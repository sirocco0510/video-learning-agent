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

## Phase 2:视频源工厂(3h)

### 必读
- [[requirements#FR-1 视频源管理]]
- [[requirements#FR-8 录屏与音频]]
- [[requirements#6.1 source/video_source.py]]

### 文件清单
- `src/vla/source/video_source.py`

### 实现要点

#### `VideoSourceFactory._is_downloadable(url: str) -> bool`
```python
def _is_downloadable(self, url: str) -> bool:
    """调用 yt-dlp --simulate,返回码 0 即视为可下载"""
    r = subprocess.run(
        ["yt-dlp", "--simulate", "--quiet", url],
        capture_output=True, timeout=30,
    )
    return r.returncode == 0
```

#### `VideoSourceFactory._download(url, video_id) -> Path`
- 调用 `yt-dlp -f worst -o <tmp>/<id>.mp4 <url>`
- 检查 `returncode == 0`,否则抛 `DownloadError`
- 返回文件路径(必须确认文件存在)

#### `VideoSourceFactory._record_screen(url, video_id, duration_sec) -> Path`
- `subprocess.run(["open", url])` 打开浏览器
- `time.sleep(5)` 等加载
- 启动 ffmpeg **非阻塞**:`subprocess.Popen([...])`
- 完整命令:
```python
[
    "ffmpeg", "-y",
    "-f", "avfoundation",
    "-framerate", "30",
    "-i", f"{self.config.video_source.record.screen_index}:{self.config.video_source.record.audio_input.split(':')[1]}",
    "-t", str(duration_sec),
    "-c:v", "libx264",
    "-preset", "ultrafast",
    "-crf", "28",
    "-c:a", "aac",
    "-b:a", "128k",
    output_path,
]
```

#### 关键点
- 录屏命令**异步启动**,主调度器会等转写完才继续
- 失败时**必须** `proc.kill()` 清理孤儿进程
- `expected_duration` + 30s 余量后,主动 `kill` ffmpeg

### 验收
```bash
# 测试 1:可下载视频
uv run python -c "
from pathlib import Path
from vla.source.video_source import VideoSourceFactory
from vla.log.transcription_log import TranscriptionLog
log = TranscriptionLog(Path('./logs'))
factory = VideoSourceFactory(Path('./tmp'), log)
src = factory.get('https://www.bilibili.com/video/BV1xxxxxxx', 'test1', 600)
assert src.path.exists()
print(f'mode={src.mode}, size={src.path.stat().st_size}')
"

# 测试 2:不可下载视频(模拟)
# 直接调用 _record_screen,确认 ffmpeg 启动
```

---

## Phase 3:字幕三级策略(4h)

### 必读
- [[requirements#FR-2 字幕提取(三级策略)]]
- [[requirements#6.1 subtitle/*]]

### 文件清单
- `src/vla/subtitle/bilibili_official.py`
- `src/vla/subtitle/browser_plugin.py`
- `src/vla/subtitle/strategy.py`

### 实现顺序(强依赖)

#### Step 1:`bilibili_official.py`(1.5h)

**B站 API 调用顺序**(严格遵守):
```python
# Step 1: 获取 cid
GET https://api.bilibili.com/x/web-interface/view?bvid={bvid}
# Response: data.cid, data.title

# Step 2: 获取字幕列表
GET https://api.bilibili.com/x/player/v2?bvid={bvid}&cid={cid}
# Response: data.subtitle.subtitles[]

# Step 3: 下载字幕 JSON
GET https://{subtitle_url}  # subtitle_url 以 // 开头,补 https:
# Response: { body: [{ from, to, content }] }
```

**关键 header**:
```python
HEADERS = {
    "User-Agent": "Mozilla/5.0 ... Chrome/120.0.0.0",
    "Referer": "https://www.bilibili.com",
}
```

**语言优先级**:`["zh-Hans", "zh-CN", "zh-Hant", "en-US", "en"]`,命中第一个。

#### Step 2:`browser_plugin.py`(1.5h)

**目录扫描顺序**(按 `config.browser_plugin.plugin_paths`):
```python
PLUGIN_PATHS = [
    "~/Documents/VideoTrans/subtitles",
    "~/Downloads",
]
```

**匹配规则**:
1. 精确:`{title}_{bvid}.{srt|vtt|json|ass}`
2. 模糊:`*{bvid}*` 且后缀匹配
3. 都找不到 → 返回 None

**格式解析**:
| 后缀 | 解析方法 |
|------|----------|
| `.srt` | `pysrt.open(path)`,`"\n".join(s.text for s in subs)` |
| `.vtt` | `webvtt.WebVTT().read(path)`,`"\n".join(s.text for s in captions)` |
| `.json` | 递归提取字符串字段,按时间戳排序 |
| `.ass` | 解析 `Dialogue:` 行,取第 10 个逗号之后的内容 |

#### Step 3:`strategy.py`(1.5h)

按 [[requirements#6.1 subtitle/strategy.py]] 实现三级调度。

**关键设计**:弹窗是策略 ② 的一部分,**内化进 strategy**,而不是放在主调度。

**降级语义**(严格遵守):
```
策略 ① 失败  ──┐
策略 ② 失败  ──┼──→ 返回 None → 主调度走策略 ③
策略 ② 超时  ──┤   (注意:不是 transcribe_fail,而是字幕获取失败,主调度接手走下载/录屏)
用户点"跳过" ──┘
```

```python
from dataclasses import dataclass
from ..state.plugin_status import PluginStatus  # FR-2.9/2.10

class SubtitleStrategy:
    def __init__(self, config, log, notifier, plugin_status: PluginStatus):
        self.config = config
        self.log = log
        self.notifier = notifier
        self.official = BilibiliOfficialSubtitle()
        self.plugin = BrowserPluginSubtitle(config)
        self.plugin_status = plugin_status  # FR-2.10 注入 session 级单例

    def get_subtitle(self, url, bvid, title) -> SubtitleResult | None:
        """
        三级调度(含插件状态机,FR-2.9/2.10/2.11):
          ① B站官方 CC → SubtitleResult(source="official")
          ② 浏览器插件(状态机控制)
              - unavailable → 直接降级,不再尝试
              - unknown/available → 扫描 + 弹窗
          ③ 返回 None,主调度走下载/录屏 + Whisper
        """
        # ===== 策略 ① B站官方 CC =====
        try:
            result = self.official.get_subtitle(url)
            if result:
                text, metadata = result
                self.log.info(f"✓ 策略 ① 命中:B站官方字幕 ({metadata.get('language')})")
                return SubtitleResult(
                    text=text, source="official", metadata=metadata
                )
        except Exception as e:
            self.log.warning(f"策略 ① 失败:{e}")

        # ===== 策略 ② 浏览器插件(FR-2.10 状态机)=====
        # 2.1.0 先看 session 级状态
        status = self.plugin_status.get()
        if status == "unavailable":
            self.log.info("⏭️ 插件状态=unavailable,跳过整个策略 ②")
            return None

        # 2.1.1 状态 = unknown 或 available → 先扫描目录
        path = self.plugin.find_subtitle(bvid, title)
        if path:
            text = self.plugin.parse(path)
            self.log.info(f"✓ 策略 ② 命中(已有文件):{path}")
            self.plugin_status.mark_available()
            return SubtitleResult(
                text=text, source="plugin",
                metadata={"file": str(path), "trigger": "scan_hit"},
            )

        # 2.1.2 扫描无 → 状态机判断:unknown 才弹窗,available 直接降级
        if status == "available":
            self.log.info("📭 插件可用但本次扫描无文件,降级到策略 ③")
            return None

        # 2.2 status=unknown → 弹窗询问用户(FR-2.9 一次启动)
        timeout = self.config.browser_plugin.remind_timeout_sec
        self.notifier.info("需要浏览器插件", f"准备转写:{title}")
        user_choice = self.notifier.ask_open_browser(
            f"启用 VideoTrans:{title}", url, timeout_sec=timeout
        )

        if user_choice == "timeout":
            self.log.info("⌛ 弹窗超时,降级 + 标记 unavailable")
            self.plugin_status.mark_unavailable(reason="dialog_timeout")
            return None

        if user_choice == "skip":
            self.log.info("⏭️ 用户跳过,降级 + 标记 unavailable")
            self.plugin_status.mark_unavailable(reason="user_skip")
            return None

        # 2.3 用户"已开启" → 等文件出现
        path = self.plugin.wait_for_subtitle(bvid, title, timeout=timeout)
        if path:
            text = self.plugin.parse(path)
            self.plugin_status.mark_available()
            self.log.info(f"✓ 策略 ② 命中(用户开启后):{path}")
            return SubtitleResult(
                text=text, source="plugin",
                metadata={"file": str(path), "trigger": "user_opened"},
            )

        # 2.4 等文件超时 → 标记 unavailable + 降级
        self.log.info("⌛ 等文件超时,降级 + 标记 unavailable")
        self.plugin_status.mark_unavailable(reason="wait_timeout")
        return None

        # ===== 策略 ③ 由主调度器 _process_one 接管 =====
```

**`MacOSNotifier.ask_open_browser` 接口扩展**(返回三类):

```python
def ask_open_browser(
    self,
    title: str,
    url: str,
    timeout_sec: int = 30,
) -> str:
    """
    返回:
      "opened"   用户点击"已开启"(超时前)
      "skip"     用户点击"跳过该视频"
      "timeout"  超时未响应(降级到策略 ③)
    """
    # osascript display dialog 不直接支持 timeout 异步
    # 实现思路:subprocess.Popen 异步启动,主线程 sleep(timeout_sec)
    # 若用户在 timeout 前返回, kill osascript
    # 否则 kill 并返回 "timeout"
```

### 验收
```python
# 测试 1:B站官方字幕(找一个有 CC 的视频)
url = "https://www.bilibili.com/video/BV1xxxxxxx"
strategy = SubtitleStrategy(cfg, log, notifier)
result = strategy.get_subtitle(url, "BV1xxxxxxx", "测试标题")
assert result.source == "official"

# 测试 2:无字幕视频 + 用户点"跳过" → 返回 None(由主调度走兜底)
result = strategy.get_subtitle(no_sub_url, bvid, title)
assert result is None  # 不是 transcribe_fail

# 测试 3:插件目录有文件 → 直接用 plugin
Path("~/Documents/VideoTrans/subtitles/test_BV1yyy.srt").write_text(srt_content)
result = strategy.get_subtitle(any_url, "BV1yyy", "test")
assert result.source == "plugin"
assert result.metadata["trigger"] == "scan_hit"

# 测试 4:弹窗超时 → 返回 None(主调度走兜底)
mock_notifier = Mock(ask_open_browser=Mock(return_value="timeout"))
strategy = SubtitleStrategy(cfg, log, mock_notifier)
result = strategy.get_subtitle(url, bvid, title)
assert result is None
assert not log.has_transcribe_fail()  # 关键:不该记为转写失败
```

---

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

### 关键变化(相对 v0.1)

**之前**:每条视频单独总结 → 现在改为 **6h 配额触发批量总结**。
- 主调度不再每条调用 `summarize(title, text)`,改为在累加器达 6h 时调用 `summarize_batch(window)`。

#### `LLMSummarizer.summarize_batch(window: list[SubtitleWindowItem]) -> str`

```python
@dataclass
class SubtitleWindowItem:
    """6h 窗口内的单个视频"""
    title: str
    text: str
    quality: QualityResult
    source: str  # official / plugin / whisper
    duration_sec: int

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

def summarize_batch(
    self,
    window: list[SubtitleWindowItem],
    group_title: str | None = None,
) -> str:
    """
    生成 6h 窗口的批量总结。
    返回 Markdown 内容(不含 notes.md 头)。
    调用方负责追加/写入文件。
    """
    # 1. 构造 video_index(标题 + 来源)
    video_index = "\n".join(
        f"- [{i+1}] {item.title}({item.duration_sec}s, 来源:{item.source})"
        for i, item in enumerate(window)
    )

    # 2. 构造 video_sections(每个视频一个 section)
    video_sections = "\n\n".join(
        f"### 视频 {i+1}:{item.title}\n"
        f"时长:{item.duration_sec}s | 来源:{item.source} | 质量:{item.quality.score}/100\n\n"
        f"{item.text[:3000]}"
        for i, item in enumerate(window)
    )

    # 3. 调用 LLM
    prompt = SUMMARIZE_BATCH_PROMPT.format(
        video_count=len(window),
        video_index=video_index,
        video_sections=video_sections,
        min_words=self.config.summary.target_words_min,
        max_words=self.config.summary.target_words_max,
    )
    summary = self.llm.complete(prompt, max_tokens=2000)

    # 4. 加笔记头部(可选,group_title 时加)
    if group_title:
        total_sec = sum(item.duration_sec for item in window)
        header = f"## {group_title} — 累计 {total_sec // 60} 分钟({len(window)} 个视频)\n\n"
        return header + summary
    return summary
```

**调用时机**(由 Phase 8 主调度触发):

```text
_quota.add(duration_sec)
  ├─ current < threshold → 不调 summarize_batch
  └─ current ≥ threshold → 调 summarize_batch(window)
                              ├─ 写入 notes.md
                              ├─ 清空 window
                              └─ _quota.reset()
```

**返回纯 Markdown**(不写文件,由主调度统一追加)。

### 验收
```python
summarizer = LLMSummarizer(LLMClient(...), Path("./notes/videos.md"))
window = [
    SubtitleWindowItem(title="Python 列表推导式", text="...", quality=Q, source="whisper", duration_sec=1800),
    SubtitleWindowItem(title="Python 装饰器", text="...", quality=Q, source="whisper", duration_sec=2400),
    # ... 共 6h
]
result = summarizer.summarize_batch(window, group_title="Python 基础")
assert 500 <= len(result) <= 1500
assert "Python" in result
assert "列表推导式" in result
assert "装饰器" in result
assert result.startswith("## ")
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
| E2E-2c | 无 CC + 插件目录无 + **弹窗超时未响应** | **降级到 whisper** + 总结 | **transcribe_fail.csv 不增加** |
| E2E-2d | **插件字幕质量不过关**(FR-2.11) | 标记 + 降级 + 写日志 | quality_fail.csv `failure_source=plugin`,后续视频直接 unavailable |
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
- [ ] Phase 1: 配置 + 数据模型
- [ ] Phase 2: 视频源工厂
- [ ] Phase 3: 字幕三级策略
- [ ] Phase 4: 流式转写
- [ ] Phase 5: 质量门控
- [ ] Phase 6: 通知 + 日志
- [ ] Phase 7: LLM 总结
- [ ] Phase 7.5: 状态管理(QuotaManager / HistoryManager / PluginStatus)
- [ ] Phase 7.6: FailureAlert(失败日志上限监控,FR-6.6)
- [ ] Phase 8: 主调度 + CLI
- [ ] Phase 9: 端到端测试
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