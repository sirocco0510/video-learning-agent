---
title: 视频挂机学习 Agent — 实施计划
created: 2026-08-31
updated: 2026-08-31
type: project
status: active
tags:
  - type/project
  - topic/video-learning
---

# 视频挂机学习 Agent — 实施计划

> 本文档为 VS Code AI 助手实施时的**执行手册**。VS Code AI 助手按 Phase 顺序推进,**前一 Phase 验收通过**才能进入下一 Phase。

---

## 0. 总览

| 阶段 | 主题 | 预计工时 | 关键产出 |
|------|------|----------|----------|
| Phase 0 | 项目骨架 | 0.5h | uv 项目 + 目录结构 |
| Phase 1 | 配置 + 数据模型 | 1h | `config.py` + `models.py` |
| Phase 2 | 视频源工厂 | 3h | 下载 + 录屏 |
| Phase 3 | 字幕三级策略(含插件状态机) | 4h | 官方 + 插件(一次启动)+ 调度 |
| Phase 4 | Whisper 流式转写 | 2h | 边转边删 |
| Phase 5 | 质量门控 | 2h | LLM 检查 + 启发式 |
| Phase 6 | macOS 通知 + 日志 | 1.5h | 弹窗 + CSV |
| Phase 7 | LLM 批量总结 | 2h | 6h 配额触发 + 跨视频合并 |
| Phase 8 | 主调度 + 配额/去重 + CLI | 3h | 累计时长 + history + 串联 + typer |
| Phase 9 | 端到端测试 | 3h | 真实 B站验证 |

**总计:约 24 小时**(3 个工作日)

**新增跨阶段模块**:
- `state/quota.py` — 配额管理器(累计时长、归零、阈值)
- `state/history.py` — 历史记录(去重、URL key)
- `state/plugin_status.py` — 插件状态机(unknown/available/unavailable)

---

## Phase 0:项目骨架(0.5h)

### 操作步骤
```bash
# 1. 创建项目
uv init video-learning-agent
cd video-learning-agent

# 2. 创建目录结构
mkdir -p src/vla/{source,subtitle,transcribe,quality,summary,ui,log}
mkdir -p config tests/fixtures logs
touch src/vla/__init__.py
for d in source subtitle transcribe quality summary ui log; do
  touch src/vla/$d/__init__.py
done

# 3. 添加依赖
uv add faster-whisper yt-dlp httpx openai pyobjc-framework-Quartz \
       pysrt webvtt-py pyyaml pydantic typer rich
uv add --dev pytest pytest-cov pytest-asyncio

# 4. 安装系统依赖(macOS)
brew install ffmpeg
```

### 交付物
- `pyproject.toml` 含全部依赖
- 完整目录结构(见下)
- `README.md`(项目说明 + 安装步骤)

### 验收
```bash
uv run python -c "import faster_whisper, yt_dlp, httpx, typer; print('OK')"
which ffmpeg && ffmpeg -version | head -1
```

---

## Phase 1:配置 + 数据模型(1h)

### 必读
[[requirements#六、模块详细规格]] — 严格按 6.1 节契约实现

### 文件清单
- `src/vla/config.py` — 配置加载
- `src/vla/models.py` — pydantic 数据模型
- `config/vla.yaml` — 默认配置(从 [[requirements#八、配置文件]] 复制)

### `config.py` 实现要点
- 使用 `pydantic-settings` 风格(纯 pydantic BaseModel 也可)
- `VLAConfig` 嵌套所有子配置类(`StorageConfig`, `WhisperConfig` 等)
- `from_yaml(path) -> VLAConfig` 类方法
- 环境变量覆盖:`api_key_env` 字段指定的 env 变量自动读取
- `model_validator` 校验:target_words_min < max

### `models.py` 实现要点
严格按 [[requirements#6.1 models.py]] 实现 4 个模型:
- `VideoTask`
- `SubtitleResult`
- `QualityResult`
- `VideoSource`

每个字段加类型注解和 `Field(description=...)`。

### 验收
```python
# 测试代码
from vla.config import VLAConfig
from vla.models import VideoTask

cfg = VLAConfig.from_yaml("./config/vla.yaml")
task = VideoTask(
    id="test",
    title="测试",
    url="https://www.bilibili.com/video/BV1X54y1p7Dd?spm_id_from=333.788.videopod.episodes&vd_source=60b3697df165803cf7edaa996f5e0cb9",
    expected_duration=1800,
)
assert task.id == "test"
print(cfg.whisper.model)  # "small"
```

---

## Phase 2:音频源工厂(2h,2026-09-03 重构)

> **重大重构**:从"视频源工厂(下载+录屏)"改为"音频源工厂",Whisper 永不接收视频信号。

### 必读
- [[requirements#FR-1 视频源管理]] — 仍保留 `_is_downloadable`(yt-dlp simulate),用于路径 ① 判定
- [[requirements#FR-2.14 策略 ③ 音频三级降级总览]]
- [[requirements#FR-2.16a yt-dlp -x 抽音频]]
- [[requirements#FR-2.16b Puppeteer 抓音频流]]

### 文件清单
- `src/vla/audio/source_factory.py` **(NEW)** — 统一音频源工厂,封装三条路径
- `src/vla/source/video_source.py` — **简化**,删除 `_record_screen`,只保留 `_is_downloadable` 给路径 ① 用

### 实现要点

#### `AudioSourceFactory.extract(url, video_id, duration_sec, save_dir) -> AudioSource`
- 返回 `AudioSource(path, source="ytdlp|puppeteer|tab_audio_recorder", audio_id=None)`
- 决策树(FR-2.14):
  ```
  yt-dlp simulate 可下载?
    ├─ YES → _extract_via_ytdlp(url, video_id, save_dir)  # 路径 ①
    │         └─ yt-dlp -x --audio-format wav ...
    └─ NO  → _extract_via_puppeteer(driver, url, duration_sec)  # 路径 ②
              └─ page.evaluate MediaRecorder + ArrayBuffer 回传
  ```

#### 路径 ①:`_extract_via_ytdlp(url, video_id, save_dir) -> AudioSource`
```python
def _extract_via_ytdlp(self, url: str, video_id: str, save_dir: Path) -> AudioSource:
    """yt-dlp -x 直接抽音频 wav,16kHz 单声道,Whisper 直吃(FR-2.16a)。"""
    save_dir.mkdir(parents=True, exist_ok=True)
    out = save_dir / f"{video_id}.wav"
    r = subprocess.run(
        ["yt-dlp", "-x", "--audio-format", "wav",
         "--postprocessor-args", "-ac 1 -ar 16000",
         "-o", str(out), url],
        capture_output=True, timeout=300,
    )
    if r.returncode != 0 or not out.exists():
        raise YtdlpExtractError(f"yt-dlp 抽音失败: {r.stderr.decode()}")
    return AudioSource(path=out, source="ytdlp")
```

#### 路径 ②:`_extract_via_puppeteer(driver, url, duration_sec, save_dir) -> AudioSource`
- `ctx.new_page().goto(url)` 后台标签页
- `page.evaluate(getUserMedia + MediaRecorder)` 流式录制 `duration_sec` 秒
- ArrayBuffer → Python → `save_dir / f"{bvid}_{timestamp}.webm"`
- **关键**:Tab Audio Recorder 路径失败才 fallback 到这里;正常情况 Puppeteer 音频流质量差(浏览器音频 mix)
- 详见 [[requirements#FR-2.16b]]

#### 路径 ③:Tab Audio Recorder(FR-2.16c,Phase 3 `tab_audio_recorder.py` 实现)
- AudioSourceFactory **不直接实现路径 ③**,只在路径 ①② fail 时向上抛 `AudioSourceUnavailable`
- `SubtitleStrategy` 捕获后调 `TabAudioRecorder.start_recording` 走路径 ③

### 验收
```bash
# 测试 1:yt-dlp 抽音频(B站可下载视频)
uv run python -c "
from pathlib import Path
from vla.audio.source_factory import AudioSourceFactory
factory = AudioSourceFactory(Path('./logs/audio_raw'))
src = factory.extract('https://www.bilibili.com/video/BV1xxxxxxx', 'test1')
assert src.path.exists() and src.path.stat().st_size > 1000
assert src.source == 'ytdlp'
print(f'source={src.source}, size={src.path.stat().st_size}')
src.path.unlink()  # 测试完清理
"

# 测试 2:Puppeteer 路径(需要 Chrome --remote-debugging-port=9222)
# 见 Phase 3 Step 3 tab_audio_recorder.py 的验收段
```

---

## Phase 3:字幕平台无关三级策略(6h)

> **重大重写**:从"B站限定"升级为"平台无关"。2026-09-01 spike 已验证 Puppeteer + page.evaluate + context.request 通道。

### 必读
- [[requirements#FR-2 字幕提取(平台无关三级策略)]]
- [[requirements#6.1 subtitle/*]]

### 文件清单
- `src/vla/subtitle/platform_adapter.py` **(NEW)** — Protocol + Registry
- `src/vla/subtitle/bilibili_adapter.py` **(NEW)** — B站 adapter
- `src/vla/subtitle/internal_site_adapter.py` **(NEW)** — 内部网站 adapter stub
- `src/vla/subtitle/browser_driver.py` **(NEW)** — Puppeteer + 4 种 JS 探测
- `src/vla/subtitle/tab_audio_recorder.py` **(NEW)** — Tab Audio Recorder 触发 + 下载 + 异步队列(FR-2.24/2.25/2.26/2.27)
- `src/vla/audio/queue.py` **(NEW)** — AudioQueue(asyncio.Queue,容量 10)
- `src/vla/audio/worker_pool.py` **(NEW)** — WhisperWorkerPool(默认 2 worker)
- `src/vla/subtitle/bilibili_official.py` — 保留,作为 `BilibiliAdapter.fetch_api_subtitle` 内部实现
- `src/vla/subtitle/browser_plugin.py` — **废弃**,仅保留 `parse()` 方法
- `src/vla/subtitle/strategy.py` — **重写**,从"扫描 + 弹窗"改为"adapter 三级降级"
- `config/vla.yaml` — 新增 `puppeteer` 配置块 + `platforms` 段

### 已验证 spike(2026-09-01)

`scripts/spike_browser_subtitle.py` 验证结果:

| 项 | 结果 |
|---|---|
| `connect_over_cdp("http://localhost:9222")` | ✅ 通 |
| `page.goto(B站 URL)` 后台标签页 | ✅ 不抢焦点 |
| `page.evaluate(fetch player/v2)` | ✅ 拿到 `subtitles count=1` |
| `context.request.get(subtitle_url)` | ✅ status 200,跨 origin 通过 |
| body[] | 1143 条中文 AI 字幕(`ai-zh`) |
| dump 到 `.srt` | ✅ 72947 bytes / 4571 行 |

### 实现顺序(强依赖)

#### Step 1:`platform_adapter.py`(0.5h)

**Protocol 定义**:

```python
from typing import Protocol
from ..models import SubtitleResult

class PlatformAdapter(Protocol):
    @classmethod
    def match(cls, url: str) -> bool: ...

    def fetch_api_subtitle(self, url: str) -> tuple[str, dict] | None:
        """策略 ①:平台公开 API(httpx)。"""

    def fetch_browser_subtitle(self, driver, url: str) -> tuple[str, dict] | None:
        """策略 ②:Puppeteer 通用 JS 探测。"""

    def fetch_via_recording(self, driver, url: str, duration_sec: int) -> tuple[str, dict] | None:
        """策略 ③:Puppeteer 录屏扩展 + Whisper。"""
```

**Registry**:

```python
class PlatformAdapterRegistry:
    def __init__(self):
        self._adapters: list[type[PlatformAdapter]] = []

    def register(self, adapter_cls: type[PlatformAdapter]) -> None: ...

    def get_for_url(self, url: str) -> PlatformAdapter | None:
        for cls in self._adapters:
            if cls.match(url):
                return cls(driver=self._driver)
        return None
```

**TDD**:测试 register / get_for_url / 域名匹配 / 找不到返回 None。

#### Step 2:`browser_driver.py`(2h,核心模块)

**职责**:Puppeteer 通用驱动 + 4 种 JS 探测 + 跨域处理。

```python
class BrowserDriver:
    def __init__(self, config: VLAConfig):
        self.config = config
        self._browser: playwright.sync_api.Browser | None = None

    def connect(self) -> playwright.sync_api.Browser:
        """connect_over_cdp 到用户 Chrome。"""
        url = f"http://localhost:{self.config.puppeteer.debugging_port}"
        self._browser = playwright.sync_api.sync_playwright().start().chromium.connect_over_cdp(url)
        return self._browser

    def new_background_page(self, browser=None):
        """context.new_page(),后台标签页,不抢用户焦点。

        FR-2.23:创建前调用 cleanup_stale_extension_pages 关闭旧的扩展 popup,
        只保留最新的 1 个,避免 Chrome 标签栏堆积(用户多次按 Cmd+Shift+R)。
        """
        ctx = browser.contexts[0] if browser else self._browser.contexts[0]
        self.cleanup_stale_extension_pages(ctx, keep_latest=1)
        return ctx.new_page()

    @staticmethod
    def cleanup_stale_extension_pages(ctx, keep_latest: int = 1) -> int:
        """FR-2.23:关闭除最新 keep_latest 个外的所有 chrome-extension:// 页面。"""
        pages = list(getattr(ctx, "pages", []))
        ext = [p for p in pages if (getattr(p, "url", "") or "").startswith("chrome-extension://")]
        if len(ext) <= keep_latest:
            return 0
        to_close = ext[:-keep_latest] if keep_latest > 0 else ext
        closed = 0
        for p in to_close:
            try:
                p.close()
                closed += 1
            except Exception:
                pass
        return closed

    def fetch_subtitle_via_browser(
        self, page, url: str
    ) -> tuple[str, dict] | None:
        """4 种 JS 探测,首个命中即返回。"""
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)  # 等播放器初始化

        # 1. <track> 标签
        track = page.evaluate("""() => {
            const t = document.querySelector('video track[kind="subtitles"], video track[kind="captions"]');
            return t ? {src: t.src, lang: t.srclang} : null;
        }""")
        if track:
            text = self._fetch_subtitle_text(page, track["src"])
            if text:
                return text, {"source": "browser", "method": "track", "lang": track["lang"]}

        # 2. __INITIAL_STATE__ 找字幕 URL
        url_found = page.evaluate("""() => {
            const init = window.__INITIAL_STATE__ || window.__INITIAL_DATA__;
            if (!init) return null;
            const urls = [];
            const walk = (obj) => {
                if (typeof obj === 'string' && /^https?:\/\//.test(obj) && /subtitle|caption/i.test(obj)) {
                    urls.push(obj);
                } else if (typeof obj === 'object' && obj !== null) {
                    for (const v of Object.values(obj)) walk(v);
                }
            };
            walk(init);
            return urls[0] || null;
        }""")
        if url_found:
            text = self._fetch_subtitle_text(page, url_found)
            if text:
                return text, {"source": "browser", "method": "initial_state"}

        # 3. window.player.getSubtitle()
        player_sub = page.evaluate("""async () => {
            if (window.player?.getSubtitle) {
                return await window.player.getSubtitle();
            }
            if (window.player?.subtitle) {
                return window.player.subtitle;
            }
            return null;
        }""")
        if player_sub:
            text = self._extract_text_from_player_subtitle(player_sub)
            if text:
                return text, {"source": "browser", "method": "player_object"}

        # 4. DOM 选择器扫描
        dom_text = page.evaluate("""() => {
            const sels = ['[class*="subtitle"]', '[class*="caption"]', '[id*="subtitle"]'];
            for (const sel of sels) {
                const el = document.querySelector(sel);
                if (el && el.textContent.trim()) return el.textContent.trim();
            }
            return null;
        }""")
        if dom_text:
            return dom_text, {"source": "browser", "method": "dom_selector"}

        return None

    def _fetch_subtitle_text(self, page, url: str) -> str | None:
        """跨 origin 用 context.request;同 origin 用 page.evaluate fetch。"""
        if url.startswith("//"):
            url = "https:" + url
        ctx = page.context
        try:
            resp = ctx.request.get(url)
            if resp.status == 200:
                # 根据 content-type / url 后缀判断格式
                return self._parse_response(resp, url)
        except Exception:
            pass
        return None
```

**TDD**:
- mock `playwright.sync_api.Browser`,模拟每种探测方法的 page.evaluate 返回
- 测试 track / initial_state / player / DOM 各路径
- 测试跨 origin context.request 调用
- 测试 4 种都 miss 时返回 None

#### Step 3:`tab_audio_recorder.py`(2h,2026-09-03 重构)

**职责**:Tab Audio Recorder 触发(FR-2.24)+ 编辑器页下载(FR-2.25)+ audio_id 管理 + 异步入队,**不录屏**。

**关键设计(2026-09-03)**:

| 组件 | 职责 |
|------|------|
| `TabAudioRecorder.start_recording(driver, url, duration_sec) -> str` | 在 bg page 上 evaluate 启动录制,轮询 editor.html URL 拿到 audio_id(FR-2.24) |
| `DownloadButtonClicker.click_download(driver, audio_id, save_dir, timeout_sec=180) -> Path` | goto editor.html → 注册 download 监听 → 点下载按钮 → save_as(FR-2.25) |
| `TabAudioRecorderPluginStatus`(单例) | unknown/asked/available/skipped 四态,整 session 只弹一次(FR-2.21) |
| `AudioQueue.push(audio_id, audio_path, video_meta)` | 入队到 Whisper worker 池(FR-2.27) |

**核心代码骨架**:

```python
class TabAudioRecorder:
    EXT_ID = "hanfcigjijjcbdbfoplddndcblmlfiio"
    EDITOR_URL = f"chrome-extension://{EXT_ID}/editor.html"

    def __init__(self, driver: BrowserDriver, config: VLAConfig):
        self.driver = driver
        self.config = config
        self.timeout_sec = config.extension.tab_audio_recorder.timeout_sec  # 180

    async def start_recording(self, url: str, duration_sec: int) -> str:
        """bg page evaluate 启动录制,返回 audio_id。"""
        bg_page = await self._get_bg_page()
        # 在 bg page 上 evaluate 启动(扩展内部暴露的全局函数名)
        await bg_page.evaluate("startTabRecording()")
        # 轮询 editor.html URL 拿到 audio_id
        deadline = asyncio.get_event_loop().time() + duration_sec + 60
        while asyncio.get_event_loop().time() < deadline:
            current_url = bg_page.url
            m = re.search(r"editor\.html\?id=(\d+)", current_url)
            if m:
                return m.group(1)
            await asyncio.sleep(1)
        raise RecorderTriggerError("Tab Audio Recorder 未在 timeout 内跳转到 editor.html")

    async def _get_bg_page(self) -> Page:
        for ctx in self.driver.browser.contexts:
            for p in ctx.pages:
                if p.url.startswith(f"chrome-extension://{self.EXT_ID}/"):
                    return p
        # 未打开则导航到 background page
        ctx = self.driver.browser.contexts[0]
        page = await ctx.new_page()
        await page.goto(f"chrome-extension://{self.EXT_ID}/_generated_background_page.html")
        return page


class DownloadButtonClicker:
    EXT_ID = "hanfcigjijjcbdbfoplddndcblmlfiio"

    def __init__(self, driver: BrowserDriver, save_dir: Path, timeout_sec: int = 180):
        self.driver = driver
        self.save_dir = save_dir
        self.timeout_sec = timeout_sec

    async def click_download(self, audio_id: str) -> Path:
        """goto editor.html + 点下载 → 拿到 audio_id.webm。"""
        ctx = self.driver.browser.contexts[0]
        page = await ctx.new_page()
        # 先注册 download 监听(必须在点按钮前)
        async with ctx.expect_download(timeout=self.timeout_sec * 1000) as dl_info:
            await page.goto(f"chrome-extension://{self.EXT_ID}/editor.html?id={audio_id}")
            # 点下载按钮(候选 selector)
            for sel in [
                'button:has-text("Download")',
                'button:has-text("保存")',
                '#download-btn',
                '[data-action="download"]',
            ]:
                try:
                    await page.click(sel, timeout=2000)
                    break
                except PlaywrightTimeoutError:
                    continue
        download = await dl_info.value
        target = self.save_dir / f"{audio_id}.webm"
        await download.save_as(target)
        await page.close()
        return target


class TabAudioRecorderPluginStatus:
    """整 session 单例,四态机(unknown/asked/available/skipped)。"""
    _instance = None

    def __init__(self):
        self.state = "unknown"  # unknown → asked → available | skipped

    @classmethod
    def get(cls) -> "TabAudioRecorderPluginStatus":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
```

**关键约束(2026-09-03)**:

- 扩展 ID 固定为 `hanfcigjijjcbdbfoplddndcblmlfiio`,用户在 `chrome://extensions` 复制粘贴到 `config/vla.yaml`
- **不依赖 hotkey**:Tab Audio Recorder 无 `chrome.commands`,macOS TCC 拦截 `Input.dispatchKeyEvent`
- **editor.html = 录制完成标志**:扩展自己 stop + 跳转,Agent 端只轮询 URL,避免抢焦
- **download 必须先注册再点按钮**:`ctx.expect_download` 上下文管理器保证事件不丢
- **audio_id 是纯数字字符串**,作为本地文件名 + 转写队列 key(FR-2.26)
- **audio_path 命名**:`logs/audio_raw/<audio_id>.webm`,失败文件进 `logs/audio_failed/`


#### Step 4:`bilibili_adapter.py`(1h)

**实现** `PlatformAdapter`,内部用 spike 已验证的 Puppeteer 流程。

```python
class BilibiliAdapter:
    def __init__(self, driver: BrowserDriver):
        self.driver = driver
        self.official = BilibiliOfficialSubtitle()  # 复用现有 httpx 实现

    @classmethod
    def match(cls, url: str) -> bool:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        return host.endswith("bilibili.com") or host == "b23.tv"

    def fetch_api_subtitle(self, url: str) -> tuple[str, dict] | None:
        """策略 ①:复用现有 B站官方 httpx 调用。"""
        return self.official.get_subtitle(url)

    def fetch_browser_subtitle(self, driver, url: str) -> tuple[str, dict] | None:
        """策略 ②:Puppeteer + page.evaluate fetch player/v2(已 spike 验证)。"""
        page = driver.new_background_page()
        try:
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            bvid = self.official.extract_bv_id(url)

            # 同 origin fetch player/v2
            player_data = page.evaluate(f"""
                async () => {{
                    const cidResp = await fetch('https://api.bilibili.com/x/web-interface/view?bvid={bvid}', {{credentials: 'include'}});
                    const cidData = await cidResp.json();
                    const cid = cidData?.data?.cid;
                    if (!cid) return null;
                    const subResp = await fetch(`https://api.bilibili.com/x/player/v2?bvid={bvid}&cid=${{cid}}`, {{credentials: 'include'}});
                    return await subResp.json();
                }}
            """)
            if not player_data:
                return None
            subs = (player_data.get("data") or {}).get("subtitle", {}).get("subtitles", [])
            if not subs:
                return None

            # 按语言优先级选
            chosen = next((s for s in subs if s.get("lan") in ("zh-Hans", "zh-CN")), subs[0])
            sub_url = chosen["subtitle_url"]
            if sub_url.startswith("//"):
                sub_url = "https:" + sub_url

            # 跨 origin:context.request
            ctx = page.context
            resp = ctx.request.get(sub_url)
            body_data = resp.json()
            text = "\n".join(item["content"] for item in body_data.get("body", []))
            return text, {
                "source": "browser",
                "method": "bilibili_player_v2",
                "language": chosen.get("lan"),
                "ai_status": chosen.get("ai_status"),
            }
        finally:
            page.close()

    def fetch_via_recording(self, driver, url: str, duration_sec: int) -> tuple[str, dict] | None:
        """策略 ③:复用 BrowserDriver + BrowserRecorder。"""
        # 由 Phase 8 主调度直接调用 BrowserRecorder,adapter 这里只标 stub
        return None
```

**TDD**:
- `match`:测试 bilibili.com / b23.tv / 其他域名
- `fetch_api_subtitle`:Mock BilibiliOfficialSubtitle
- `fetch_browser_subtitle`:Mock driver + page.evaluate + context.request,验证完整流程
- `fetch_via_recording`:返回 None(由主调度直接调 recorder)

#### Step 5:`internal_site_adapter.py`(0.25h)

```python
class InternalSiteAdapter:
    @classmethod
    def match(cls, url: str) -> bool:
        return False  # 等公司下发账号后实现

    def fetch_api_subtitle(self, url: str) -> tuple[str, dict] | None:
        raise NotImplementedError("等公司下发账号")

    def fetch_browser_subtitle(self, driver, url: str) -> tuple[str, dict] | None:
        raise NotImplementedError("等公司下发账号")

    def fetch_via_recording(self, driver, url: str, duration_sec: int) -> tuple[str, dict] | None:
        raise NotImplementedError("等公司下发账号")
```

**TDD**:测试 `match=False` + 三个方法抛 `NotImplementedError`。

#### Step 6:`strategy.py` 重写(1h)

旧实现:扫描 VideoTrans 目录 + 弹窗。新实现:三级降级 + adapter 注册。

```python
class SubtitleStrategy:
    def __init__(self, registry: PlatformAdapterRegistry, driver: BrowserDriver, recorder: BrowserRecorder, log):
        self.registry = registry
        self.driver = driver
        self.recorder = recorder
        self.log = log

    def get_subtitle(
        self, url: str, duration_sec: int = 600
    ) -> SubtitleResult | None:
        """
        三级降级(平台无关):
          ① adapter.fetch_api_subtitle(url)
          ② adapter.fetch_browser_subtitle(driver, url)
          ③ recorder.record_and_transcribe(driver, url, duration_sec)
        任一失败降级到下一级;仅 ③ 失败返回 None。
        """
        adapter = self.registry.get_for_url(url)
        if adapter is None:
            self.log.warning(f"无匹配 adapter,跳 ①;直接走 ② + ③:{url}")
            adapter = FallbackAdapter(self.driver, self.recorder)

        # ①
        try:
            result = adapter.fetch_api_subtitle(url)
            if result:
                text, meta = result
                self.log.info(f"✓ 策略 ① 命中(API)")
                return SubtitleResult(text=text, source="api", metadata=meta)
        except Exception as e:
            self.log.warning(f"策略 ① 失败:{e}")

        # ②
        try:
            browser = self.driver.connect()
            result = adapter.fetch_browser_subtitle(browser, url)
            if result:
                text, meta = result
                self.log.info(f"✓ 策略 ② 命中(browser:{meta.get('method')})")
                return SubtitleResult(text=text, source="browser", metadata=meta)
        except Exception as e:
            self.log.warning(f"策略 ② 失败:{e}")

        # ③
        try:
            browser = self.driver.connect()
            result = adapter.fetch_via_recording(browser, url, duration_sec)
            if result:
                text, meta = result
                self.log.info(f"✓ 策略 ③ 命中(whisper)")
                return SubtitleResult(text=text, source="whisper", metadata=meta)
        except Exception as e:
            self.log.error(f"策略 ③ 失败(计入 transcribe_fail):{e}")

        return None
```

**TDD**:
- Mock adapter,3 个 fetch 方法分别成功 → 测试对应 source
- ① 失败 / ② 命中 → 验证不会调 ③
- ① ② 失败 / ③ 命中 → 验证 ③ 调用
- 全部失败 → 返回 None
- 无匹配 adapter → FallbackAdapter

#### Step 7:`config/vla.yaml` 新增配置(0.25h)

```yaml
puppeteer:
  debugging_port: 9222
  recording_output_dir: "~/Downloads"
  recording_hotkey: "Control+Shift+R"

platforms:
  bilibili:
    enabled: true
    match_hosts:
      - "bilibili.com"
      - "b23.tv"
  internal_site:
    enabled: false  # 等账号下发
    match_hosts: []
```

#### Step 8:`browser_plugin.py` 保留 `parse()` 即可(0.25h)

删 `find_subtitle` / `wait_for_subtitle`,只保留 `parse(path) → str`。`BrowserDriver._fetch_subtitle_text` 用它解析 Puppeteer 取回的字幕文件。

#### Step 9:旧代码清理(0.25h)

- `subtitle/strategy.py` 旧逻辑 + `state/plugin_status.py` 状态机 + `ui/macos_notify.py` 的 `ask_open_browser` → **全部删除**
- `tests/test_subtitle_strategy.py` 旧测试 → **删除**
- `tests/test_plugin_status.py` → **删除**
- `tests/test_macos_notify.py` → 保留 info/warning/alert 测试,删 `ask_open_browser` 测试

### 验收

```python
# 测试 1:Phase 3 单元测试全绿
uv run pytest tests/test_platform_adapter.py tests/test_browser_driver.py                 tests/test_browser_record.py tests/test_bilibili_adapter.py                 tests/test_internal_site_adapter.py tests/test_subtitle_strategy.py                 tests/test_browser_plugin.py -v
# 期望:全部 pass(约 35-40 个测试)

# 测试 2:跑现有 spike 脚本,验证 B站端到端通道仍通
uv run python scripts/spike_browser_subtitle.py
# 期望:✓ 新建后台标签页 → view API → player/v2 → context.request → .srt dump

# 测试 3:BilibiliAdapter.fetch_browser_subtitle 端到端(mock driver)
# - 模拟 page.evaluate 返回 player/v2 响应
# - 模拟 context.request 返回 body JSON
# - 期望:返回 (text, metadata) 其中 source="browser", method="bilibili_player_v2"

# 测试 4:SubtitleStrategy 三级降级
# - Mock adapter,① 成功 → 不调 ②③
# - ① 失败 / ② 成功 → 不调 ③
# - 全部失败 → 返回 None
# - ③ 失败 → 不抛异常,返回 None
```

## Phase 4:流式转写(2h)

### 必读
- [[requirements#FR-3 流式转写与磁盘管理]]

### 文件清单
- `src/vla/transcribe/streaming.py`

### 实现要点

#### `transcribe_video(video_path, duration_sec) -> str`

```python
def transcribe_video(self, video_path, duration_sec):
    # 1. ffmpeg 切音轨(单声道,16kHz)
    audio_path = video_path.with_suffix(".wav")
    subprocess.run([
        "ffmpeg", "-y", "-i", str(video_path),
        "-ar", "16000", "-ac", "1",
        "-c:a", "pcm_s16le",
        str(audio_path),
    ], check=True)

    # 2. 立即删视频源(无论后续如何)
    if video_path.exists():
        video_path.unlink()
        self.log.info(f"🗑️ 删除视频源:{video_path}")

    # 3. faster-whisper 转写
    segments, info = self.model.transcribe(
        str(audio_path),
        language=self.config.whisper.language,
        beam_size=5,
        vad_filter=True,
    )
    text = "\n".join(seg.text for seg in segments)
    return text
```

#### 关键约束
- **必须**音频就绪后立即删视频源(不能等转写完才删)
- `vad_filter=True` 必须开(过滤静音段,提速)
- 模型**懒加载**:首次调用时才加载

#### `cleanup(*paths)`
- 简单 `unlink()` 循环,加 try/except
- 用于质量检查通过后清理音频

### 验收
```python
# 测试:1 分钟样本视频
from faster_whisper import WhisperModel
# 先用 faster-whisper 自带的 sample 测试
# 或用手机录一段 1 分钟中文视频

transcriber = StreamingTranscriber(
    model_size="tiny",  # 测试用 tiny,快
    log=TranscriptionLog(Path("./logs"))
)
text = transcriber.transcribe_video(Path("./sample.mp4"), 60)
assert len(text) > 50
assert not Path("./sample.mp4").exists()  # 视频源已删
assert Path("./sample.wav").exists()  # 音频保留
```

---

## Phase 5:质量门控(2h)

### 必读
- [[requirements#FR-4 质量门控]]
- [[requirements#6.1 quality/checker.py]]

### 文件清单
- `src/vla/quality/checker.py`
- `src/vla/llm/client.py`(独立 LLM 客户端,Phase 1 也可提前)

### 实现要点

#### `QualityChecker.check(...)` 流程

```python
def check(self, text, title, duration_sec, model_size):
    char_count = len(text)
    cps = char_count / max(duration_sec, 1)

    # 启发式 1:语速异常
    if cps < self.config.quality_check.min_char_per_second:
        return QualityResult(passed=False, score=20, ...)
    if cps > self.config.quality_check.max_char_per_second:
        return QualityResult(passed=False, score=30, ...)

    # 启发式 2:重复异常
    sentences = re.split(r"[。!?]", text)
    most_common = Counter(sentences).most_common(1)[0]
    if most_common[1] >= 3 and len(most_common[0]) > 5:
        return QualityResult(passed=False, score=10, issues=["重复异常"], ...)

    # LLM 检查
    prompt = self.PROMPT.format(...)
    resp = self.llm.complete(prompt, max_tokens=500)
    data = self._parse_json(resp)

    return QualityResult(
        passed=data.get("pass", False) and data.get("score", 0) >= self.config.quality_check.min_score_to_pass,
        score=data.get("score", 0),
        issues=data.get("issues", []),
        suggestion=data.get("suggestion", ""),
        char_count=char_count,
    )
```

#### LLM Client(`src/vla/llm/client.py`)

```python
class LLMClient:
    """统一 OpenAI 兼容协议,适配 OpenAI / Qwen / DeepSeek"""

    def __init__(self, config: LLMClientConfig):
        self.client = openai.OpenAI(
            api_key=os.environ[config.api_key_env],
            base_url=os.environ.get(config.base_url_env, "https://api.openai.com/v1"),
        )
        self.model = config.model

    def complete(self, prompt: str, max_tokens: int = 1000) -> str:
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=0.3,
        )
        return resp.choices[0].message.content
```

#### Prompt(从 requirements 复制)

```python
PROMPT = """你是字幕质量审核员。请评估以下 Whisper 转写的字幕质量。

【视频标题】:{title}
【视频时长】:{duration_sec} 秒
【转写引擎】faster-whisper-{model_size}
【文本长度】{char_count} 字
【估算语速】{char_per_second:.1f} 字/秒(中文正常 4-7)

【转写文本】
{text}

【检查维度】
1. **通顺度**:有无明显乱码、无意义重复、语序混乱?
2. **完整性**:是否覆盖视频大部分内容?语速是否在正常范围?
3. **准确性**:专业术语是否正确(可基于标题推断)?
4. **重复异常**:是否出现 ≥3 次重复的同一句话(Whisper 失败的典型表现)?

【输出 JSON】
{{
  "pass": true/false,
  "score": 0-100,
  "issues": ["问题1", "问题2"],
  "suggestion": "如果 fail,具体修复建议(如重新转写/人工修正)"
}}

只输出 JSON,不要其他文字。"""
```

### 验收
```python
# 测试 1:正常文本 → pass
checker = QualityChecker(LLMClient(...))
text = "这是一些正常的中文转写文本,内容丰富,描述详细..." * 50
result = checker.check(text, "Python 教程", 600, "small")
assert result.passed == True

# 测试 2:异常语速 → 直接 fail
result = checker.check("短短", "测试", 600, "small")
assert result.passed == False
assert "语速" in result.issues[0]

# 测试 3:重复异常 → 直接 fail
result = checker.check("同样的话。同样的话。同样的话。" * 100, "测试", 600, "small")
assert result.passed == False
```

---

## Phase 6:macOS 通知 + 日志(1.5h)

### 必读
- [[requirements#FR-6 macOS 系统通知]]
- [[requirements#FR-7 日志与审计]]
- [[requirements#6.1 ui/macos_notify.py]]
- [[requirements#6.1 log/transcription_log.py]]

### 文件清单
- `src/vla/ui/macos_notify.py`
- `src/vla/log/transcription_log.py`

### 实现要点

#### `MacOSNotifier`

**`info` / `warning`**:非阻塞
```python
def info(self, title, message):
    subprocess.run(["osascript", "-e", f'display notification "{message}" with title "{title}"'])

def warning(self, title, message):
    subprocess.run(["osascript", "-e", f'display notification "{message}" with title "{title}" sound name "Basso"'])
```

**`alert`**:阻塞,返回点击的按钮
```python
def alert(self, title, message, buttons=("OK",)) -> str:
    btn_list = ", ".join(f'"{b}"' for b in buttons)
    script = f'''
    tell application "System Events"
        set theResult to button returned of (display dialog "{message}" ¬
            with title "{title}" ¬
            buttons {{{btn_list}}})
        return theResult
    end tell
    '''
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return r.stdout.strip()
```

**`ask_open_browser`**:插件启用提示(返回三类,见 [[requirements#FR-2.5/2.6]])

```python
def ask_open_browser(
    self,
    title: str,
    url: str,
    timeout_sec: int = 30,
) -> str:
    """
    返回三类值:
      "opened"   用户在超时前点击"已开启"
      "skip"     用户点击"跳过该视频"
      "timeout"  超时未响应 → 主调度降级到策略 ③

    实现思路:
      1. 用 subprocess.Popen 异步启动 osascript display dialog
      2. 主线程 sleep(timeout_sec)
      3. 若用户在 timeout 前返回 → kill osascript,返回 "opened" 或 "skip"
      4. 若 timeout 触发 → kill osascript,返回 "timeout"
    """
    msg = (
        f"需要使用 VideoTrans 插件转写字幕。\n"
        f"请在浏览器打开:{url}\n\n打开后点'已开启'"
    )

    # 异步启动 osascript
    proc = subprocess.Popen(
        ["osascript", "-e", f'''
            tell application "System Events"
                set theResult to button returned of (display dialog "{msg}" ¬
                    with title "需要启用浏览器插件" ¬
                    buttons {{"已开启", "跳过该视频"}})
                return theResult
        end tell
        '''],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        stdout, _ = proc.communicate(timeout=timeout_sec)
        result = stdout.strip()
        if result == "已开启":
            return "opened"
        if result == "跳过该视频":
            return "skip"
        return "skip"  # 其他意外情况,安全降级
    except subprocess.TimeoutExpired:
        proc.kill()
        return "timeout"
```

#### `TranscriptionLog`

**关键方法**:
```python
def log_transcribe_fail(self, video_id, title, url, stage, error):
    # 追加到 transcribe_fail.csv
    # 列:timestamp, video_id, title, url, stage, error

def log_quality_fail(self, video_id, title, url, result, text):
    # 追加到 quality_fail.csv
    # 列:timestamp, video_id, title, url, score, issues, suggestion
    # 同时把 text 存到 failed_texts/<id>_<title短>.txt

def save_transcribed(self, video_id, title, text, quality, source, duration_sec):
    """质量通过 → 原文存盘到 logs/transcribed/<id>_<title短>.txt(FR-7.7)。
    文件格式:首行 # 视频标题 + 来源 + 质量分,后续为字幕正文(便于 Phase 7 直接读取)。"""
    safe_title = _safe_title(title)
    path = self.transcribed_dir / f"{video_id}_{safe_title}.txt"
    header = f"# {title}\n来源:{source} | 质量:{quality.score}/100 | 时长:{duration_sec}s\n\n"
    path.write_text(header + text, encoding="utf-8")

def save_failed_text(self, video_id, title, text, reason):
    """质量失败 → 原文存盘到 logs/failed_texts/<id>_<title短>.txt(FR-7.3)。
    文件格式:首行 # 标题 + 失败原因,后续为字幕正文(便于人工审核)。"""
    safe_title = _safe_title(title)
    path = self.failed_texts_dir / f"{video_id}_{safe_title}.txt"
    header = f"# {title}\n失败原因:{reason}\n\n"
    path.write_text(header + text, encoding="utf-8")
```

### 验收
```python
# 测试通知(手动):在 macOS 终端运行
notifier = MacOSNotifier()
notifier.info("测试", "这是一条普通通知")  # 右上角横幅
notifier.warning("警告", "这是一条警告通知")  # 横幅 + 声音
choice = notifier.alert("确认", "看到这个就点确定")  # 模态弹窗
assert choice == "OK"

# 测试日志
log = TranscriptionLog(Path("./logs"))
log.log_transcribe_fail("v1", "测试", "http://x", "whisper", "模型未找到")
assert (Path("./logs/transcribe_fail.csv")).exists()
assert len(log.transcribe_fail_file.read_text().splitlines()) >= 2
```

---

## Phase 7:LLM 批量总结(2h)

### 必读
- [[requirements#FR-5 LLM 总结]]
- [[requirements#FR-9 累计时长与去重(配额管理)]]
- [[requirements#6.1 summary/llm_summarizer.py]]

### 文件清单
- `src/vla/summary/llm_summarizer.py`

### 关键变化(相对 v0.1 → 2026-09)

**v0.1**:每条视频单独总结 → 改为 **6h 配额触发批量总结**(in-memory window)。
**2026-09**:进一步改为 **从磁盘 `logs/transcribed/*.txt` 读**(FR-7.7)+ **统一输出 500-800 字总结**(用户需求)。
- Phase 4 触发 `save_transcribed()` 把通过的字幕写盘,Phase 8 不需要在内存维护 window
- 崩溃恢复:即使 session 中途崩,磁盘文件还在,下次启动照样能总结
- 配额触发时一次性读盘所有 `*.txt`(按 mtime 升序,即按处理顺序)+ 批量 LLM 总结

#### `LLMSummarizer.summarize_batch(transcribed_dir: Path, ...) -> str`

```python
@dataclass
class TranscribedItem:
    """从 logs/transcribed/*.txt 读出的单条字幕"""
    title: str
    source: str          # official / plugin / whisper
    quality_score: int
    duration_sec: int
    text: str
    mtime: float

SUMMARIZE_BATCH_PROMPT = """你是视频内容总结助手。以下是累计约 6 小时视频的字幕(共 {video_count} 个视频),请生成一份 {min_words}-{max_words} 字的统一总结。

【视频清单】
{video_index}

【字幕内容】
{video_sections}

【要求】
1. 从所有视频标题提取**核心知识点**,跨视频**合并去重**
2. 每个视频作为总结中的一个**子要点**(## 二级标题 + 子列表)
3. 优先保留 **可操作的方法 / 概念 / 结论**
4. 跳过偶尔提到的次要内容
5. 使用 Markdown 格式(## / ### / - / 列表)
6. 字数控制在 {min_words}-{max_words} 之间
7. 输出里**只包含这一批 6 小时的总结**,不写"以上是本次总结"等元话语

【输出】
直接输出 Markdown 内容。"""

def _load_items(self, transcribed_dir: Path) -> list[TranscribedItem]:
    """从 logs/transcribed/*.txt 加载,按 mtime 升序。"""
    items: list[TranscribedItem] = []
    for path in sorted(transcribed_dir.glob("*.txt"), key=lambda p: p.stat().st_mtime):
        content = path.read_text(encoding="utf-8")
        # 解析头部 "# title\n来源:... | 质量:N/100 | 时长:Ns\n\n<text>"
        lines = content.split("\n", 1)
        title = lines[0].lstrip("# ").strip() if lines[0].startswith("# ") else path.stem
        meta_line = lines[1].split("\n", 1)[0] if len(lines) > 1 else ""
        # 来源:official / 质量:85/100 / 时长:600s
        source = "whisper"
        quality_score = 0
        duration_sec = 0
        for token in meta_line.split("|"):
            token = token.strip()
            if token.startswith("来源:"):
                source = token.removeprefix("来源:").strip()
            elif "质量:" in token:
                m = re.search(r"质量:(\d+)", token)
                if m: quality_score = int(m.group(1))
            elif "时长:" in token:
                m = re.search(r"时长:(\d+)", token)
                if m: duration_sec = int(m.group(1))
        body = content.split("\n\n", 1)[1] if "\n\n" in content else content
        items.append(TranscribedItem(
            title=title, source=source, quality_score=quality_score,
            duration_sec=duration_sec, text=body.strip(),
            mtime=path.stat().st_mtime,
        ))
    return items

def summarize_batch(
    self,
    transcribed_dir: Path,
    group_title: str | None = None,
    clear_after: bool = True,
) -> str:
    """从 logs/transcribed/ 读所有字幕 → 批量 LLM 总结 → 返回 Markdown。
    
    clear_after=True: 总结完后删除源文件,避免下次重复总结(默认)。
    """
    items = self._load_items(transcribed_dir)
    if not items:
        return ""
    
    video_index = "\n".join(
        f"- [{i+1}] {item.title}({item.duration_sec}s, 来源:{item.source})"
        for i, item in enumerate(items)
    )
    video_sections = "\n\n".join(
        f"### 视频 {i+1}:{item.title}\n"
        f"时长:{item.duration_sec}s | 来源:{item.source} | 质量:{item.quality_score}/100\n\n"
        f"{item.text[:3000]}"
        for i, item in enumerate(items)
    )
    prompt = SUMMARIZE_BATCH_PROMPT.format(
        video_count=len(items),
        video_index=video_index,
        video_sections=video_sections,
        min_words=self.config.summary.target_words_min,
        max_words=self.config.summary.target_words_max,
    )
    summary = self.llm.complete(prompt, max_tokens=2000)
    
    if group_title:
        total_sec = sum(item.duration_sec for item in items)
        header = f"## {group_title} — 累计 {total_sec // 60} 分钟({len(items)} 个视频)\n\n"
        result = header + summary
    else:
        result = summary
    
    if clear_after:
        for item in items:
            path = next(transcribed_dir.glob(f"*.txt"), None)  # 简化,实际用 _path_for(item)
            # 实际删除应通过 mtime 反查;实现时记录 _items_with_path 返回 (item, path)
    return result
```

**调用时机**(由 Phase 8 主调度触发):

```text
_quota.add(duration_sec)
  ├─ current < threshold → 不调 summarize_batch
  └─ current ≥ threshold → 调 summarize_batch(transcribed_dir)
                              ├─ 写入 notes.md(FR-5)
                              ├─ 删除 transcribed/*.txt(避免重复)
                              └─ _quota.reset()
```

**返回纯 Markdown**(不写文件,由主调度统一追加)。

### 验收
```python
# 准备 logs/transcribed/ 目录,写入若干 fake 字幕(模拟 Phase 6 save_transcribed 输出)
transcribed_dir = Path("./logs/transcribed")
for title, secs in [("Python 列表推导式", 1800), ("Python 装饰器", 2400), ("Python 生成器", 3600)]:
    (transcribed_dir / f"v_{title}.txt").write_text(
        f"# {title}\n来源:whisper | 质量:85/100 | 时长:{secs}s\n\n字幕正文...",
        encoding="utf-8",
    )

summarizer = LLMSummarizer(LLMClient(...), Path("./notes/videos.md"))
result = summarizer.summarize_batch(transcribed_dir, group_title="Python 基础")
assert 500 <= len(result) <= 1500
assert "Python" in result
assert "列表推导式" in result
assert "装饰器" in result
assert result.startswith("## ")
# 总结完后默认清空源文件
assert list(transcribed_dir.glob("*.txt")) == []
```
```

---

## Phase 7.5:状态管理(配额 / 历史 / 插件状态)(1.5h)

### 必读
- [[requirements#FR-9 累计时长与去重(配额管理)]]
- [[requirements#FR-10 视频组概念]]
- [[requirements#FR-2.9/2.10 插件状态机]]

### 文件清单
- `src/vla/state/__init__.py`
- `src/vla/state/quota.py` — 累计时长配额
- `src/vla/state/history.py` — 去重历史
- `src/vla/state/plugin_status.py` — 插件状态机

### `QuotaManager`(FR-9)

```python
class QuotaManager:
    def __init__(self, threshold_sec: int, log):
        self.threshold = threshold_sec  # 默认 21600
        self.current = 0
        self.window: list[SubtitleWindowItem] = []  # 6h 窗口
        self.log = log

    def add(self, item: SubtitleWindowItem) -> bool:
        """
        累加并返回是否触发总结。
        - 返回 False:未触发,继续下一个视频
        - 返回 True :触发,主调度应调 summarize_batch
        """
        self.current += item.duration_sec
        self.window.append(item)
        if self.current >= self.threshold:
            return True
        return False

    def drain(self) -> list[SubtitleWindowItem]:
        """取出当前窗口并清空"""
        items = self.window
        self.window = []
        self.current = 0
        return items

    @property
    def progress(self) -> float:
        return min(self.current / self.threshold, 1.0)
```

### `HistoryManager`(FR-9.5/9.6, FR-10.2/10.6)

```python
class HistoryManager:
    """维护 transcribed_history.jsonl,负责去重"""

    def __init__(self, history_file: Path, log):
        self.file = history_file
        self.log = log
        self._urls: set[str] = set()
        self._load()

    def _load(self):
        """启动时读 history,填充 _urls 集合"""
        if not self.file.exists():
            return
        for line in self.file.read_text(encoding="utf-8").splitlines():
            try:
                data = json.loads(line)
                self._urls.add(data["url"])
            except (json.JSONDecodeError, KeyError):
                continue

    @staticmethod
    def make_url_key(group_id: str, bvid: str) -> str:
        """FR-10.2:内部 URL 表示"""
        return f"bilibili://group/{group_id}/{bvid}"

    def is_already_done(self, url_key: str) -> bool:
        return url_key in self._urls

    def record_success(
        self,
        url_key: str,
        title: str,
        duration_sec: int,
        group_id: str,
        source: str,
    ):
        """追加一行 jsonl"""
        record = {
            "url": url_key,
            "title": title,
            "duration_sec": duration_sec,
            "group_id": group_id,
            "source": source,
            "transcribed_at": datetime.now().isoformat(),
        }
        with self.file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._urls.add(url_key)
```

### `PluginStatus`(FR-2.9/2.10, session 级单例)

```python
class PluginStatus:
    """整 session 只确认一次插件可用性(FR-2.9)"""

    def __init__(self):
        self._status = "unknown"  # unknown / available / unavailable
        self._reason: str | None = None

    def get(self) -> str:
        return self._status

    def mark_available(self):
        self._status = "available"
        self._reason = None

    def mark_unavailable(self, reason: str):
        """FR-2.10:不再弹窗,整 session 后续都走策略 ③"""
        self._status = "unavailable"
        self._reason = reason

    def is_unavailable(self) -> bool:
        return self._status == "unavailable"
```

### 验收
```python
# QuotaManager
q = QuotaManager(threshold_sec=3600, log=None)
assert q.add(SubtitleWindowItem(..., duration_sec=1800)) is False
assert q.add(SubtitleWindowItem(..., duration_sec=1800)) is True  # 触发
window = q.drain()
assert len(window) == 2
assert q.current == 0

# HistoryManager
hm = HistoryManager(Path("./history.jsonl"), log=None)
url_key = HistoryManager.make_url_key("python-basics", "BV1xxx")
assert not hm.is_already_done(url_key)
hm.record_success(url_key, "...", 1800, "python-basics", "whisper")
assert hm.is_already_done(url_key)

# PluginStatus
ps = PluginStatus()
assert ps.get() == "unknown"
ps.mark_unavailable(reason="user_skip")
assert ps.is_unavailable()
ps.mark_available()
assert ps.get() == "available"
```

---

## Phase 7.6:FailureAlert(失败日志上限监控,FR-6.6)(0.5h)

### 必读
- [[requirements#FR-6 macOS 系统通知]] 中的 FR-6.5/6.6/6.7
- [[requirements#十、异常矩阵]]

### 文件清单
- `src/vla/log/failure_alert.py` — `FailureAlert` 类

### 设计目标

- **失败静默** = 转写失败 / 质量不过关都不弹通知,只写 CSV 日志(FR-6.4)
- **累计监控** = `transcribe_fail` + `quality_fail` 达到 `log_alert_threshold`(默认 50)的整数倍 → **阻塞弹窗汇总**(FR-6.6)
- **避免误判** = `last_alerted_multiple` 字段,只在跨过倍数边界时才弹,避免每条都弹

### 实现要点

#### `FailureAlert`

```python
# src/vla/log/failure_alert.py
class FailureAlert:
    """
    FR-6.6:失败日志上限弹窗
    累计失败条数(transcribe_fail + quality_fail)达到 threshold 整数倍时
    弹一次阻塞式汇总,避免每条失败都打扰用户
    """

    def __init__(
        self,
        threshold: int,
        log: "TranscriptionLog",
        notifier: "macOSNotifier",
    ):
        self.threshold = threshold
        self.log = log
        self.notifier = notifier
        # 跨过的最大倍数边界(避免每条都弹)
        self.last_alerted_multiple: int = 0

    def check_after_write(self):
        """
        在每次写 transcribe_fail / quality_fail 后调用
        - 累计失败 >= threshold * k → 弹窗(仅第一次跨过 k)
        - 0 ≤ k * threshold 区间内 → 静默
        """
        total_fail = self.log.count_total_failures()
        current_multiple = total_fail // self.threshold
        if current_multiple > self.last_alerted_multiple:
            self.last_alerted_multiple = current_multiple
            self._alert(total_fail)

    def _alert(self, total_fail: int):
        """阻塞式弹窗 + 提供查看日志入口"""
        breakdown = (
            f"转写失败 {self.log.transcribe_fail_count()} 条 + "
            f"质量失败 {self.log.quality_fail_count()} 条"
        )
        self.notifier.alert_blocking(
            title="⚠️ 失败积累过多",
            message=(
                f"已积累 {total_fail} 条失败({breakdown}),"
                f"请检查 logs/ 目录下的 CSV 与原文。"
            ),
            detail_button="查看 logs/",
            detail_action=lambda: self._reveal_logs(),
        )

    def _reveal_logs(self):
        """macOS Finder 打开 logs 目录"""
        import subprocess
        from pathlib import Path
        logs_dir = Path(self.log.config.log_dir)
        subprocess.run(["open", str(logs_dir)], check=False)
```

#### `TranscriptionLog.count_total_failures()`(扩展)

```python
# 在 src/vla/log/transcription_log.py 添加
class TranscriptionLog:
    # ... 已有方法 ...

    def count_total_failures(self) -> int:
        """FR-6.6:统计所有失败条数"""
        return self.transcribe_fail_count() + self.quality_fail_count()

    def transcribe_fail_count(self) -> int:
        return self._count_csv("transcribe_fail.csv")

    def quality_fail_count(self) -> int:
        return self._count_csv("quality_fail.csv")

    def _count_csv(self, filename: str) -> int:
        path = Path(self.config.log_dir) / filename
        if not path.exists():
            return 0
        with path.open(encoding="utf-8") as f:
            return sum(1 for _ in csv.DictReader(f))
```

#### `macOSNotifier.alert_blocking()`(扩展)

```python
# 在 src/vla/ui/macos_notify.py 添加
class macOSNotifier:
    # ... 已有方法 ...

    def alert_blocking(
        self,
        title: str,
        message: str,
        detail_button: str | None = None,
        detail_action: callable = None,
        timeout_sec: int = 60,
    ):
        """
        FR-6.6 阻塞式弹窗,失败上限时使用
        - 与 ask_open_browser 类似,阻塞 + 超时
        - 超时后调用方决定降级(此处只需弹窗,无需降级)
        """
        script = f'''
        display dialog "{message}" with title "{title}" buttons {{"OK", "{detail_button or '查看'}"}} default button "OK"
        '''
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                timeout=timeout_sec,
                check=False,
            )
            # 用户点击 detail_button → 触发 action
            if detail_button and detail_action:
                if detail_button in result.stdout.decode():
                    detail_action()
        except subprocess.TimeoutExpired:
            # 超时 = 用户没看 = 静默
            pass
```

#### 主调度器集成(FR-6.6)

```python
# VideoLearningAgent.__init__
self.failure_alert = FailureAlert(
    threshold=self.config.notifier.log_alert_threshold,
    log=self.log,
    notifier=self.notifier,
)

# 在每次 log.log_*_fail 后调用
self.failure_alert.check_after_write()
```

### 验收

```python
# 1. FailureAlert 阈值触发
log = TranscriptionLog(config)
alert = FailureAlert(threshold=10, log=log, notifier=mock_notifier)

# 模拟 9 条失败 → 不弹窗
for i in range(9):
    log.log_transcribe_fail(f"id{i}", f"title{i}", "url", "stage", "err")
    alert.check_after_write()
assert mock_notifier.alert_blocking.call_count == 0

# 第 10 条 → 弹窗(跨过 1 倍)
log.log_transcribe_fail("id10", "title10", "url", "stage", "err")
alert.check_after_write()
assert mock_notifier.alert_blocking.call_count == 1

# 11-20 条 → 不再弹
for i in range(11, 20):
    log.log_transcribe_fail(f"id{i}", f"title{i}", "url", "stage", "err")
    alert.check_after_write()
assert mock_notifier.alert_blocking.call_count == 1

# 第 20 条 → 第二次弹窗(跨过 2 倍)
log.log_transcribe_fail("id20", "title20", "url", "stage", "err")
alert.check_after_write()
assert mock_notifier.alert_blocking.call_count == 2
```

---

---

## Phase 8:主调度 + CLI(3h)

### 必读
- [[requirements#七、数据流]]
- [[requirements#九、CLI 接口]]
- [[requirements#六、模块详细规格]]

### 文件清单
- `src/vla/main.py` — `VideoLearningAgent` 主类
- `src/vla/cli.py` — typer CLI
- `src/vla/state/*.py` — Phase 7.5 产出

### 实现要点

#### `VideoLearningAgent.run(tasks: list[VideoTask])`

严格按 [[requirements#七、数据流]] 实现,新增**配额管理 + 去重 + 6h 触发总结**。

```python
def run(self, tasks: list[VideoTask]):
    """
    主流程(集成 FR-9/10 + FR-2.9-2.11 + FR-6):
      1. 过滤 history 中已转写 URL(FR-9.6)
      2. 逐个处理(字幕 + 质量门控)
      3. 累加器 >= 6h → 调 summarize_batch → 归零 → 停止(FR-9.4)
      4. **失败日志上限监控**(FR-6.6)— 写日志后检查是否触发弹窗
    """
    # 1. 去重(FR-9.6)
    pending = [t for t in tasks if not self.history.is_already_done(self._url_key(t))]
    skipped = len(tasks) - len(pending)
    if skipped:
        self.log.info(f"⏭️ 跳过 {skipped} 个已转写视频")
    if not pending:
        self.log.info("✅ 所有视频都已转写,无需处理")
        return

    for task in pending:
        # 2. 单条处理
        try:
            window_item = self._process_one(task)
        except Exception as e:
            # 真正的程序异常(FR-6.4:不弹通知,只写日志)
            self.log.log_transcribe_fail(
                task.id, task.title, str(task.url),
                "main_loop", str(e),
            )
            self._check_failure_alert()  # FR-6.6
            continue

        if window_item is None:
            # 字幕拿不到 + 转写也没成,跳过本条
            self._check_failure_alert()
            continue

        # 3. 写 history
        self.history.record_success(
            url_key=self._url_key(task),
            title=task.title,
            duration_sec=task.expected_duration,
            group_id=task.group_id,
            source=window_item.source,
        )

        # 4. 累加器判断是否触发总结
        if self.quota.add(window_item):
            self._trigger_summary(group_title=task.group_title)
            # 5. 总结后停止 session(FR-9.4)
            self.notifier.info("🎉 已累计 6 小时", "总结已生成,session 结束")
            break

def _process_one(self, task) -> SubtitleWindowItem | None:
    """
    处理单条视频,返回 SubtitleWindowItem 给 quota.add
    返回 None = 本条没成(字幕 / 转写 / 质量全失败),跳过
    """
    # ① 字幕三级策略(含插件状态机)
    subtitle = self.strategy.get_subtitle(
        str(task.url), task.id, task.title
    )

    # ② 字幕缺失 → 走下载/录屏 + Whisper(策略 ③)
    if subtitle is None:
        self.log.info(f"📼 {task.title}:走兜底,获取视频源 + Whisper")
        try:
            source = self.source_factory.get(
                str(task.url), task.id, task.expected_duration
            )
            text = self.transcriber.transcribe_video(
                source.path, task.expected_duration
            )
        except Exception as e:
            # FR-6.4:不通知,只写日志
            self.log.log_transcribe_fail(
                task.id, task.title, str(task.url),
                "source_factory", str(e),
            )
            return None
        subtitle = SubtitleResult(
            text=text,
            source="whisper",
            metadata={"video_source_mode": source.mode if source else "unknown"},
        )

    # ③ 质量检查
    qr = self.quality_checker.check(
        text=subtitle.text,
        title=task.title,
        duration_sec=task.expected_duration,
        model_size=self.config.whisper.model,
    )

    # ④ 分支
    if not qr.passed:
        # FR-6.4:失败只写日志,不弹通知
        self.log.log_quality_fail(
            task.id, task.title, str(task.url), qr, subtitle.text
        )
        # 关键(FR-2.11):插件字幕质量不过关时,标记 unavailable
        if subtitle.source == "plugin":
            self.plugin_status.mark_unavailable(reason="plugin_quality_fail")
            self.log.warning("插件字幕质量不过关,标记 unavailable,降级到策略 ③")
        return None

    # 通过:清理音频 + 进度通知(非阻塞)
    self.transcriber.cleanup(self._audio_path(task.id))
    self.notifier.info(
        "✓ 质量通过",
        f"{task.title}({qr.score}分),已加入总结窗口",
    )

    # 返回 window item(让 quota.add 累加)
    return SubtitleWindowItem(
        title=task.title,
        text=subtitle.text,
        quality=qr,
        source=subtitle.source,
        duration_sec=task.expected_duration,
    )

def _trigger_summary(self, group_title: str | None):
    """触发 6h 批量总结"""
    window = self.quota.drain()
    if not window:
        return
    summary = self.summarizer.summarize_batch(
        window, group_title=group_title
    )
    self._append_to_notes(summary)

def _check_failure_alert(self):
    """
    FR-6.6:失败日志上限弹窗
    - 累计失败条数(transcribe_fail + quality_fail)达到 threshold 整数倍时
      弹一次阻塞式汇总
    - 避免每条失败都打扰用户
    """
    total_fail = self.log.count_total_failures()
    threshold = self.config.notifier.log_alert_threshold
    current_multiple = total_fail // threshold
    if current_multiple > self.failure_alert.last_alerted_multiple:
        self.failure_alert.last_alerted_multiple = current_multiple
        breakdown = (
            f"转写失败 {self.log.transcribe_fail_count()} 条 + "
            f"质量失败 {self.log.quality_fail_count()} 条"
        )
        self.notifier.alert_blocking(
            title="⚠️ 失败积累过多",
            message=(
                f"已积累 {total_fail} 条失败({breakdown}),"
                f"请检查 logs/ 目录下的 CSV 与原文。"
            ),
            detail_button="查看 logs/",
        )

def _url_key(self, task: VideoTask) -> str:
    """FR-10.2"""
    return HistoryManager.make_url_key(task.group_id, task.bvid)
```

**关键边界(注释强提醒)**:

```python
# ① _process_one 内部不直接写 history,由 run 统一写
# ② 字幕 None → 走兜底路径,绝不写 transcribe_fail
#    (FR-2.8 字幕失败 ≠ Whisper 转写失败)
# ③ 插件字幕质量不过关 → 标记 unavailable + 写 quality_fail(FR-2.11)
# ④ 累加器 >= 6h → 触发总结 + session 停止(FR-9.4)
# ⑤ history 是 session 级单例,跨视频组共享
```

#### CLI(`cli.py`)

```python
import typer
from pathlib import Path

app = typer.Typer()

@app.command()
def process(
    url: str = typer.Option(..., "--url"),
    title: str = typer.Option(..., "--title"),
    duration: int = typer.Option(..., "--duration"),
    config_path: Path = typer.Option("./config/vla.yaml", "--config"),
):
    """处理单条视频"""
    cfg = VLAConfig.from_yaml(config_path)
    agent = VideoLearningAgent(cfg)
    task = VideoTask(id=generate_id(), title=title, url=url, expected_duration=duration)
    agent.run([task])

@app.command()
def batch(
    config_path: Path = typer.Option("./videos.yaml", "--config"),
    vla_config: Path = typer.Option("./config/vla.yaml", "--vla-config"),
):
    """批量处理"""
    cfg = VLAConfig.from_yaml(vla_config)
    tasks = load_tasks_from_yaml(config_path)
    agent = VideoLearningAgent(cfg)
    agent.run(tasks)

@app.command()
def logs(
    type_: str = typer.Option("all", "--type"),
    last: int = typer.Option(20, "--last"),
):
    """查看日志"""
    # 实现略

@app.command()
def retry(
    from_: Path = typer.Option(..., "--from"),
):
    """重试失败视频"""
    # 实现略

@app.command()
def doctor():
    """环境检测"""
    # 检查 ffmpeg / yt-dlp / 模型 / API key
```

### 验收
```bash
# 测试 CLI
uv run vla --help
uv run vla doctor

# 测试单条处理(用一个 1 分钟的本地视频)
uv run vla process --url "file:///tmp/test.mp4" --title "测试视频" --duration 60
```

---

## Phase 9:端到端测试(3h)

### 测试矩阵

| 用例 | 输入 | 预期 | 验证点 |
|------|------|------|--------|
| E2E-1 | 有 CC 的 B站视频 | official 字幕 + 总结 | 笔记追加 + 无录屏 |
| E2E-2 | 无 CC + 插件目录有文件 | plugin 字幕 + 弹窗 | 弹窗出现,点了继续 |
| E2E-2b | 无 CC + 插件目录无 + 用户点"跳过该视频" | **降级到 whisper** + 总结 | **transcribe_fail.csv 不增加** |
| E2E-2c | 无 CC + 插件目录无 + **弹窗超时未响应** | **降级到 whisper** + 总结 + **B 级 warning 通知用户** | **transcribe_fail.csv 不增加** |
| E2E-2d | **插件字幕质量不过关**(FR-2.11) | 标记 + 降级 + 写日志 | quality_fail.csv `failure_source=plugin`,后续视频直接 unavailable |
| E2E-2e | 无 CC + 浏览器探测 miss + 用户点"已启用" + Tab Audio Recorder 正常 | **Tab Audio Recorder 录制 → editor.html → 下载 → Whisper** | source="whisper",metadata["via"]="tab_audio_recorder",audio_id=数字 ID |
| E2E-2f | 无 CC + 浏览器探测 miss + 用户点"已启用" + Tab Audio Recorder 超时/失败 | **降级 quality_skip.csv** | 不记 transcribe_fail,继续下一个视频(FR-2.21 降级语义) |
| E2E-3 | 无 CC + 插件无 + 用户点"已开启"但文件不出现 | whisper 字幕 + 总结 | 降级路径 + 走下载/录屏 |
| E2E-4 | 防下载视频(YouTube 等) | 录屏 + whisper | ffmpeg 启动 |
| E2E-5 | 静音视频 | quality_fail | 视频源保留 + CSV 记录 |
| E2E-6 | 失败后重试 | retry 命令成功 | CSV 重读 + 重新处理 |
| E2E-7 | **累计时长触发总结**(FR-9) | 达到 6h → summarize_batch | 笔记含一次批量总结,session 停止 |
| E2E-8 | **去重**(FR-9.6) | history 中已存在 URL | 跳过,不观看不转写 |
| E2E-9 | **插件状态机**(FR-2.10) | 用户第一次点跳过 | 整 session 不再弹窗 |
| E2E-10 | **失败日志达上限倍数**(FR-6.6) | 累计失败 ≥ `log_alert_threshold`(默认 50) | 阻塞弹窗汇总,跨过倍数才弹,`alert_blocking.call_count` 等于跨过倍数 |

### 真实视频测试脚本

```python
# tests/e2e/test_real_bilibili.py
import pytest
from pathlib import Path

@pytest.mark.skip(reason="需要真实 B站视频,手工运行")
def test_official_subtitle():
    url = "https://www.bilibili.com/video/BV1xxxxxxx"  # 真实有字幕视频
    cfg = VLAConfig.from_yaml("./config/vla.yaml")
    agent = VideoLearningAgent(cfg)
    task = VideoTask(id="e2e-1", title="...", url=url, expected_duration=1800)
    agent.run([task])
    # 检查 notes.md 已追加
    assert Path("./notes/videos.md").exists()
    # 检查 tmp 已清理
    assert not Path("./tmp/BV1xxxxxxx.mp4").exists()
```

### 验收标准
- [ ] 12 个 E2E 用例全部通过(含降级路径 / 插件状态机 / 配额 / 去重 / 失败日志上限)
- [ ] 失败用例的 CSV 记录可读、可重试
- [ ] 磁盘 tmp 目录峰值 < 1 GB
- [ ] 单视频处理全程(从启动到总结)有明确日志

---

## 关键依赖关系图

```text
Phase 0(骨架)
   ↓
Phase 1(配置+模型) ──────┐
   ↓                     │
Phase 2(视频源) ←────────┤
   ↓                     │
Phase 3(字幕策略) ←──────┤
   ↓                     │
Phase 4(Whisper) ←───────┤
   ↓                     │
Phase 5(质量检查) ←──────┤
   ↓                     │
Phase 6(通知+日志) ←─────┤
   ↓                     │
Phase 7(LLM 总结) ←──────┤
   ↓                     │
Phase 7.5(状态管理) ←────┤
   ↓                     │
Phase 7.6(FailureAlert) ←┤
   ↓                     │
Phase 8(主调度+CLI) ←────┘
   ↓
Phase 9(E2E 测试)
```

**强约束**:
- 不能跳过中间 Phase
- 每个 Phase 验收通过才能进入下一 Phase
- Phase 5 依赖 Phase 1 的 `LLMClient`(可提前在 Phase 1 实现)
- Phase 6 / 7 可并行(但建议顺序)

---

## 进度跟踪

每完成一个 Phase,在本文件追加状态:

```markdown
## 进度

- [x] Phase 0: 项目骨架
- [x] Phase 1: 配置 + 数据模型
- [x] Phase 2: 视频源工厂
- [x] Phase 3: 字幕三级策略(2026-09-01 完成,平台无关三级 + 86 测试通过 + B站 spike 验证)
  - [x] Phase 3.2: BrowserRecorder(2026-09-01 完成,Screencastify/Screen Recorder 录屏兜底)→ **2026-09-03 重构**:`tab_audio_recorder.py`(Tab Audio Recorder + audio_id + 异步 worker 池)+ `audio/source_factory.py`(yt-dlp -x / Puppeteer 路径);原 `browser_record.py` 整段删除,详细变更见 [[requirements#FR-2.14]]
- [x] Phase 4: 流式转写(2026-09-01 完成,StreamingTranscriber + AudioTranscriber Protocol + ffmpeg 抽音轨 + faster-whisper VAD + 懒加载 + 边转写边清理 + 179 测试通过)
- [x] Phase 5: 质量门控(2026-09-01 完成,LLMClient + QualityChecker + 启发式预筛(语速 / 重复) + LLM JSON 评估 + pass/fail 阈值 + 216 测试通过)
- [x] Phase 5: 质量门控
- [x] Phase 6: 通知 + 日志
- [x] Phase 7: LLM 总结
- [x] Phase 7.5: 状态管理(QuotaManager / HistoryManager / PluginStatus)
- [x] Phase 7.6: FailureAlert(失败日志上限监控,FR-6.6)
- [x] Phase 8: 主调度 + CLI
- [x] Phase 9: 端到端测试
  - [x] Phase 9.6: FR-2.5/2.6 popup 流程接入(2026-09-02 完成,MacOSNotifier.ask_open_browser/ask_recording_done + SubtitleStrategy pause+popup + RealTextProvider 透传 notifier+plugin_status + 4 个 E2E 测试覆盖 enabled/skip/timeout/session-single-popup + 328 测试通过)
  - [x] Phase 9.6.1: popup enabled 路径修正(2026-09-02 完成,修三个 bug + 改 SSOT)
    - plugin_name "VideoTrans" → "Screen Recorder"(config + popup 文案 + 热键 Alt+Shift+R → Command+Shift+R)
    - "enabled" 路径不再 retry fetch_browser_subtitle,改为直接调 BrowserRecorder.record_and_transcribe(FR-2.14)
    - ~~弹窗超时分支加 B 级 `notifier.warning(...)`~~ **改**:**移除**超时 B 级 warning(2026-09-02 UX 收敛)—— macOS dialog 自动消失已是用户感知信号,叠加 B 级通知会和"录屏启动"/"录屏到时"挤在通知中心,反而干扰;只 `logger.info()` 记录降级原因
    - record_and_transcribe 抛错 → 不标记 unavailable,降级 ffmpeg(FR-2.20)
    - **录屏到时 = duration + post_buffer**(2026-09-02 UX 改):post_buffer 是弹性而非反应时间,warning 在 `duration + post_buffer` 后发出,见 FR-2.15
    - **pause_page_video 双触发点**(2026-09-02 UX 改):Strategy/BilibiliAdapter 在 page.goto 后**立即**也调一次,消除"录屏启动 → 视频开始播放"窗口,见 FR-2.15a'
    - **recorder 返回 transcript 路径**(2026-09-02):`record_and_transcribe` 返回 `Path` 落盘文件路径,Strategy 读一次得 `SubtitleResult.text`,见 FR-2.15b
    - audio_path 处理(2026-09-03 改):屏幕录制路径 audio_path=None(BrowserRecorder 自己清理)→ **改为 audio_id.webm 文件路径**:`logs/audio_raw/<audio_id>.webm`,由 `TabAudioRecorder.start_recording` 返回的 audio_id 命名(FR-2.26);失败文件进 `logs/audio_failed/`
    - 测试:test_subtitle_strategy 加 enabled→record 路径、test_macos_notify 加超时 warning、test_e2e 加整链路 + 真实 spike 验证
- [x] 字幕清理 Level 3 步骤 1(2026-09-02 完成,本地 postprocess):
    - 新增 `src/vla/transcribe/postprocess.py`:`merge_short_lines()` + `dedupe_repeated_segments()` + `_has_significant_overlap()`(LCS-style) + `clean_transcript()`(组装 + PostprocessStats)
    - `StreamingTranscriber.transcribe()` 末尾 `clean_transcript()` 串联(`whisper.postprocess_enabled` 控制开关,默认开)
    - 配置:`whisper.postprocess_min/max_line_chars / min_overlap_chars`(`config/vla.yaml`)
    - 测试:27 个单元测试覆盖 happy path / 边界 / spike 真实样本,full suite 380+ passed
- [x] 字幕清理 Level 4(2026-09-02 完成,云端 LLM 语义整理,可选):
    - 新增 `src/vla/quality/refiner.py`:`SubtitleRefiner.refine(text, title)` + `write_cleaned_transcript()` + `_parse_json()` helper(与 QualityChecker 同模式)
    - 新增数据模型:`Correction` + `RefinementResult`(在 `src/vla/models.py`)
    - `QualityCheckConfig` 加 `refine_enabled / refine_model / refine_max_chars` 字段,`config/vla.yaml` 同步
    - LLM 调用参数:`max_tokens=2000, temperature=0.2`(低随机,稳定输出),JSON `{cleaned_text, corrections[], notes}` schema
    - 失败 fallback:LLM 抛错 / JSON 解析失败 / 空 `cleaned_text` → 返回原始 text + notes,主流程不中断
    - 落盘:`<stem>.cleaned.txt`(与 `.transcript.txt` 同目录,不覆盖原文),头部含 `cleaned_at / model / notes / corrections` 元数据
    - 配额归类:NFR-5 第 ③ 项(字幕语义清理)
    - 测试:37 个单元测试覆盖 properties / 调用参数 / JSON 鲁棒解析 / 成功路径 / 失败 fallback / 长度超限 / 真实场景 / 落盘格式,full suite 417 passed
    - 集成 spike:`scripts/spike_refiner_integration.py` 端到端跑通(本地 clean → LLM refine → 落盘),real transcript 320 行 → 79 行 → 356 字符 + 7 条 corrections
```

---

## VS Code AI 助手使用建议

1. **不要一次性让 AI 助手实现所有 Phase**,按 Phase 拆任务,逐个推进。
2. 每个 Phase 开始前,把对应的"必读"小节和"文件清单"贴给 AI 助手(作为 chat 上下文)。
3. 每个 Phase 完成后,**跑"验收"段代码**,通过后再开下一个 Phase。
4. Phase 5 / 7 涉及 LLM,AI 助手实现时可以**留 TODO**,自己填充真实 API key。
5. Phase 9 的真实 B站测试,**AI 助手实现测试代码**,手工跑(避免封号)。

### 推荐扩展(免费 + 可接国内大模型)

| 扩展 | 特点 | 推荐理由 |
|------|------|----------|
| **Continue**(开源) | 支持 OpenAI / Anthropic / Ollama / DeepSeek / Qwen | 开源、可接本地 Ollama 做零成本补全;`@file` 上下文注入对规格友好 |
| **Cline** | Claude / GPT / Gemini / 国产模型 | 多文件修改能力强,类似 Cursor Composer |
| **Roo Code** | Cline fork,UI 更现代 | 同 Cline,带图形化工作流 |

### 上下文注入方式(替代 Cursor 的原生 vault 集成)

VS Code AI 助手不像 Cursor 原生集成整个 vault,需要手动管理上下文:

1. **粘贴规格** → chat message 里直接粘贴 [[requirements]] / 本文档的相关小节
2. **`@file` 引用** → 多数扩展支持 `@requirements` 自动注入文件内容
3. **Phase 任务模板** → 每个新 Phase 开 chat 时,先发一段:
   ```text
   任务:实现 Phase X
   必读:[粘贴对应小节]
   验收:[粘贴验收代码]
   完成后跑 [验收命令] 确认通过
   ```
