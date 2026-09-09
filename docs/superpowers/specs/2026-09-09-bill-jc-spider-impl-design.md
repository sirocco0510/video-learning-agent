# Phase 9.6 — InternalSiteSpider 实装 + end-to-end Spike(b-learning.bill-jc.com)(2026-09-09)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this spec task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `InternalSiteSpider` 从 stub 升级到真实 bill-jc 3 个 yunxuetang API(`preinit` + `kngPlay` + m3u8)实现,写一个 end-to-end spike 脚本 `scripts/spike_bill_jc_full.py`,真账号跑通:**爬取公司视频 → ffmpeg 流式抽音 → faster-whisper 转写 → 质量门控**。

**Architecture:**

```
                          scripts/spike_bill_jc_full.py
                                  │
                                  ▼
                          VideoLearningAgent._process_one
                                  │
            ┌─────────────────────┼─────────────────────┐
            ▼                     ▼                     ▼
       fetch_asset          process_asset           notifier / 截图
            │
   ┌────────┴────────┐
   │ ① strategy text │ ② InternalSiteSpider m3u8   ③ factory MP4    ④ scan_today_dir
   │                │
   │ (本 spike 强制走 ②,因公司站无字幕)
   │                ▼
   extract_m3u8_audio(m3u8_url, wav_path)  ← ffmpeg -vn -ar 16000 -ac 1 -f wav
   → Asset(text=None, audio_path=wav, source="whisper_internal_download", deletable=True)
```

**Tech Stack:** Python 3.12, httpx(async), pydantic v2, ffmpeg subprocess, faster-whisper, Playwright(仅借 cookie,不打开浏览器渲染)。

**Spec:** 本文件;续 spec §4.7(`docs/superpowers/specs/2026-09-09-asset-pipeline-design.md`)InternalSiteSpider stub → 升级到真实现。

---

## Global Constraints(所有 task 必读)

- Python 3.12, uv, src layout (`src/vla/`), lockfile 已提交
- pydantic v2 BaseModel
- pytest-asyncio, `asyncio_mode = "auto"`
- **字幕永远本地**(只用 faster-whisper / B站 CC / VideoTrans, **禁止云端转写**)
- 云端 LLM 限定两件事:① 字幕质量检查(读 `pass` + `score)② 6h 批量总结
- **磁盘友好**:256 GB 机器,峰值 < 1 GB;**本轮新增铁律:m3u8 不缓存为 mp4,直接流式抽 wav**(~50MB /3h)
- macOS 权限:屏幕录制(录屏路径), 通知(B 级), 辅助功能(A 级弹窗)首次需用户授权
- 涉及外部 HTTP 调用统一用 **httpx.AsyncClient**(项目内已有 pattern,见 `scripts/probe_bill_jc_*.py`)
- 不引入 selenium / playwright 打开 b-learning.bill-jc.com;cookie 借取走 CDP `connect_over_cdp` + `context.cookies()`
- 涉及外网 API 失败时 **fail-fast + 给用户清晰 hint**(cookie 过期提示重启 Chrome debug;m3u8 过期提示重 fetch)
- TDD: 写实现前先写失败测试,跑红 → 写最小实现 → 跑绿 → commit
- import 顺序: stdlib → third-party → local; type import ` `from __future__ import annotations` + `TYPE_CHECKING`
- logging: ` `logger = logging.getLogger(__name__), 不要 print
- 路径: `pathlib.Path`
- no dead code
- commit 规范: ` ` <scope>: <imperative summary>` `, 本仓库现有 conventional 风格

---

## 1. 背景与动机

### 1.1 现状

- Phase 9.5 已落地 producer/consumer 拆分, `fetch_asset` 4 路径兜底链就绪(① API/Browser text → ② internal_spider m3u8 → ③ VideoSourceFactory MP4 → ④ scan_today_dir webm)
- ` `InternalSiteSpider` ` 是 **占位 stub**,所有方法抛 `NotImplementedError`(per spec §4.7 "实装走单独 PR")
- 公司视频源 b-learning.bill-jc.com 全 API-driven(per ` `project-bill-jc-spider-probe.md` `):3 个 yunxuetang API 拿 m3u8,Chrome 仅借 SSO cookie
- 走 path ② 的 ` `fetch_asset` ` 代码已存在(` `src/vla/main_provider.py` `),但内部调 ` `extract_audio(Path(m3u8_url), wav_path)` ` 不通(extract_audio 假定输入是本地文件,ffmpeg m3u8 流式场景未覆盖)

### 1.2 用户痛点

当前用户在公司站看到好课程,只能:
1. 手动 Chrome 下载插件录制 → 拖到 ` `cfg.audio.downloads_dir/YYYY-MM-DD/` `(走 path ④ scan)
2. 手动用 yunxuetang API 拿 m3u8 → ffmpeg 命令手动抽音 → 手动放到 wav 路径
3. ` `vla process`  转写

整个流程 5 个手动步骤,容易出错。**目标:一条命令从 kng_id 到 wav 文件到转写到质量门控通过,1 个真实 video 验证 end-to-end 跑通**。

### 1.3 设计原则(用户已明确点)

- **纯 API,不开浏览器渲染**(已 2026-09-09 探勘验证)
- **不需要视频,只抽音频**(ffmpeg 流式 m3u8 → wav,不缓存 mp4)
- **cookie 借 Chrome debug,自动探测 CDP**(用户手动启 ` `chrome --remote-debugging-port=9222` `)
- **fail-fast 错误提示**:cookie 过期 / m3u8 过期 / 网络失败都给用户清晰 hint,不让异常默默 swallow

---

## 2. 架构总览

### 2.1 组件关系

```new InternalSiteSpider
        │ __init__(cdp_url, college_id, resolution, cookie_ttl_sec=1800)
        │ list_tasks(root_label=None, limit=10)  ← 实装(tree + pagelist)
        │ fetch_m3u8(kng_id) -> str  ← 实装(preinit + kngPlay + cookie 借取)
        ▼
fetch_asset path ②(改动)
        │ 拿到 result.metadata["video_url"]  ← m3u8 URL
        │ 调 extract_m3u8_audio(m3u8_url, wav_path)  ← 新函数
        │ → Asset(text=None, audio_path=wav, source="whisper_internal_download", deletable=True)
        ▼
process_asset
        │ transcriber.transcribe(wav) → text
        │ quality_checker.check(text) → QualityResult
        ▼
保存 *.cleaned.txt + cleanup(unlink wav if deletable)
```

### 2.2 与现有组件的关系

| 组件 | 状态 | 改动 |
|---|---|---|
| ` `InternalSiteSpider` ` (src/vla/subtitle/) | stub | **完全重写**(本轮) |
| ` `extract_audio` ` (src/vla/transcribe/extract.py) | 已实现 | 不改 |
| ` `extract_m3u8_audio` ` (src/vla/transcribe/extract.py) | 不存在 | **新增**(本轮) |
| ` `RealTextProvider.fetch_asset` ` path ② (src/vla/main_provider.py) | stub extract_audio(m3u8) | **改调 extract_m3u8_audio** |
| ` `tests/test_internal_site_spider.py` ` | 4 placeholder | **改写 + 加 httpx mock 测试** |
| ` `scripts/` ` (探索脚本) | probe_bill_jc_*.py | 保留作为 audit |

---

## 3. 数据契约

### 3.1 ` `InternalSiteSpider` ` 实装接口

```python
# src/vla/subtitle/internal_site_spider.py
class InternalSiteSpider:
    def __init__(
        self,
        cdp_url: str,            # 默认 "http://localhost:9222"
        college_id: str,
        resolution: str = "720p",  # "720p" | "480p" | "360p"
        cookie_ttl_sec: int = 1800,  # cookie 过期阈值,默认 30 min
    ) -> None: ...

    async def fetch_m3u8(self, kng_id: str) -> str:
        """返回可达 m3u8 URL。"""
        # 1. 借 cookie(CDP connect_over_cdp)
        # 2. POST /kng/study/submit/preinit(kng_id, ...)
        # 3. POST /kng/study/kngPlay(kng_id, ..., resolution 选 playDetails[i].desc == resolution)
        # 4. 返回 playDetails[i].url

    async def list_tasks(
        self,
        *,
        root_label: str | None = None,
        limit: int = 10,
    ) -> list[VideoTask]:
        """爬目录树 + 视频列表 → VideoTask list。

        流程:
        1. 借 cookie(_borrow_cookies)
        2. POST /kng/kngCatalog/student/tree → 拿 catalog 树
        3. 递归找 root_label 子树(或全树)
        4. 对每个 kngCount > 0 的叶子 catalog:
           POST /kng/knowledge/pagelist(catalogId=<leaf_id>) → 拿视频列表
        5. 累积到 limit 条 → 转 VideoTask(id=kng_id, title=..., url=...)
        6. 返回 list[VideoTask]
        """
```

### 3.2 ` `extract_m3u8_audio` ` 新接口

```python
# src/vla/transcribe/extract.py
def extract_m3u8_audio(m3u8_url: str, output_path: Path) -> None:
    """ffmpeg 流式抽 m3u8 音轨 → wav,不缓存视频。"""
    # subprocess.run([
    #     "ffmpeg", "-y", "-loglevel", "error",
    #     "-vn",  # 跳过视频轨
    #     "-i", m3u8_url,
    #     "-ac", "1", "-ar", "16000",  # mono 16kHz(ASR 标准)
    #     "-f", "wav",
    #     str(output_path),
    # ])
    # 失败 raise RuntimeError
    # output_path.parent.mkdir(parents=True, exist_ok=True)  已存在则无操作
```

### 3.3 ` `fetch_asset` ` path ② 改动

```python
# src/vla/main_provider.py (T7 已写, 改动这一段)
if result is not None and (result.source or "").startswith("internal"):
    video_url = (result.metadata or {}).get("video_url")
    if not video_url:
        logger.warning(...)
        return None
    wav_path = self._save_dir / "audio_raw" / f"{task.id}.wav"
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # OLD: extract_audio(Path(video_url), wav_path)  ← 不通
        # NEW:
        extract_m3u8_audio(video_url, wav_path)
    except Exception as e:
        logger.warning("internal spider m3u8 抽音失败 %s: %s", video_url, e)
        return None
    return Asset(text=None, source="whisper_internal_download", audio_path=wav_path, deletable=True)
```

---

## 4. 组件改动(按 task 顺序)

### 4.1 ` `src/vla/subtitle/internal_site_spider.py` `(完全重写,~ 150 行)

**Imports:**
- ` `from __future__ import annotations` `
- ` `import logging, time` `
- ` `import httpx` `
- ` `from playwright.async_api import async_playwright` `(仅借 cookie,不打开浏览器渲染)
- ` `from vla.models import VideoTask` `(list_tasks stub 仍 return type)

**` `__init__` `** 接收 4 个参数,存为 ` `self.*` `。

**` `fetch_m3u8(self, kng_id: str) -> str:` `**
1. cookies = await ` `_borrow_cookies(self.cdp_url)` `(内部 helper)
2. headers = ` `{"Origin": "https://b-learning.bill-jc.com", "Referer": "https://b-learning.bill-jc.com/", ...cookies}` `
3. async with httpx.AsyncClient(timeout=30) as client:
    - preinit_resp = await client.post("https://api-phx-ali.yunxuetang.cn/kng/study/submit/preinit", json={...}, headers=headers)
    - 200 → continue; else raise RuntimeError(f"preinit 失败:{preinit_resp.status_code}")
    - kngplay_resp = await client.post("https://api-phx-ali.yunxuetang.cn/kng/study/kngPlay", json={...}, headers=headers)
    - play_details = kngplay_resp.json().get("playDetails", [])
    - 选择 desc == ` `self.resolution` ` 的 url;fallback 到第一个
    - 返回 url
4. 任何 HTTP 异常 → raise RuntimeError with hint("检查 Chrome debug 是否启动:chrome --remote-debugging-port=9222")

**` `_borrow_cookies(self, cdp_url: str) -> list[dict]` `** (private helper)
1. async with async_playwright() as p:
    - browser = await p.chromium.connect_over_cdp(cdp_url)
    - context = browser.contexts[0]
    - cookies = await context.cookies()
2. 过滤:仅保留 yunxuetang.cn / bill-jc.com 域的 cookies
3. 返回 Playwright cookies dict list

**` `list_tasks(self, *, root_label=None, limit=10) -> list[VideoTask]` `** 全实装:
1. cookies = await ` `_borrow_cookies(self.cdp_url)` `
2. headers = ` `{"Origin": "https://b-learning.bill-jc.com", "Referer": "https://b-learning.bill-jc.com/", ...cookies}` `
3. async with httpx.AsyncClient(timeout=30) as client:
    - tree_resp = await client.post("https://api-phx-ali.yunxuetang.cn/kng/kngCatalog/student/tree", json={"pmType": "0", "collegeId": self.college_id}, headers=headers)
    - tree = tree_resp.json()(nested catalog 数组,每项 `{id, parentId, label, kngCount, children, ...}`)
    - leaves = ` `_flatten_leaves(tree, root_label=root_label)` `(内部 helper,返回 ` `kngCount > 0` ` 的叶子)
    - 对每个 leaf: pagelist_resp = await client.post("https://api-phx-ali.yunxuetang.cn/kng/knowledge/pagelist?limit=16&offset=0&orderType=desc&orderBy=createTime", json={"collegeId": self.college_id, "catalogId": leaf["id"], "title": "", "type": "", "allTag": 1, "tagIds": []}, headers=headers)
    - datas = pagelist_resp.json()["datas"]
    - 每条 `{id: kngId, title, coverUrl, ...}` ` → VideoTask(id=kngId, title=title, url=f"https://b-learning.bill-jc.com/learn/{kngId}", expected_duration=3600)
    - 累积到 limit 条 break
4. 返回 list[VideoTask]

**` `_flatten_leaves(tree, root_label)` `** private helper:递归遍历树,过滤 ` `kngCount > 0` ` 节点,若 root_label 非空只返回 label 匹配子树内的叶子。**冲突裁决:root_label 多子树同名 → 取 DFS 第一个匹配子树**(确定性);若无匹配 → raise ValueError("root_label not found: ..."),让用户重选而非悄悄返空。

**` `_borrow_cookies(self, cdp_url)` `** private helper:Playwright ` `connect_over_cdp` ` + ` `context.cookies()` `,过滤 yunxuetang.cn / bill-jc.com 域 cookie。

### 4.2 ` `src/vla/transcribe/extract.py` ` (新增 ~ 30 行)

添加 ` `extract_m3u8_audio(m3u8_url, output_path) -> None` ` 函数(同 §3.2)。

### 4.3 ` `src/vla/main_provider.py` ` (改 ~ 15 行)

fetch_asset path ② 改调 ` `extract_m3u8_audio(video_url, wav_path)` `(同 §3.3)。

### 4.4 ` `scripts/spike_bill_jc_full.py` ` (新 ~ 250 行)

CLI typer app,端到端跑通,**3 种模式**:

```python
@app.command()
def run(
    kng_id: str | None = typer.Option(None, "--kng-id", help="单视频模式: 直接跑这个 kng_id"),
    list_only: bool = typer.Option(False, "--list-only", help="只跑 list_tasks 打印 catalog + 视频列表,不爬音频"),
    root_label: str | None = typer.Option(None, "--root-label", help="list 模式: 限定根目录(如 '技术分享')"),
    limit: int = typer.Option(10, "--limit", help="list 模式: 最多返回几条"),
    college_id: str = typer.Option(..., "--college-id"),
    cdp_url: str = typer.Option("http://localhost:9222", "--cdp-url"),
    resolution: str = typer.Option("720p", "--resolution"),
    config_path: Path = typer.Option(CONFIG_FILE, "--config"),
    verbose: bool = typer.Option(False, "--verbose"),
):
    cfg = load_config(config_path)
    spider = InternalSiteSpider(cdp_url=cdp_url, college_id=college_id, resolution=resolution)
    fetch_asset, process_asset = build_text_provider(cfg)

    if list_only:
        # 模式 1: 只爬目录树 + 视频列表,打印出来
        tasks = asyncio.run(spider.list_tasks(root_label=root_label, limit=limit))
        for t in tasks:
            print(f"[LIST] {t.id} | {t.title} | {t.url}")
        return

    if kng_id is None:
        print("--kng-id 或 --list-only 至少一个")
        raise typer.Exit(1)

    # 模式 2: 完整端到端跑单视频
    task = VideoTask(
        id=kng_id, title=f"bill-jc-{kng_id}",
        url=f"https://b-learning.bill-jc.com/learn/{kng_id}",
        expected_duration=3600,
    )
    asset = asyncio.run(fetch_asset(task))
    if asset is None:
        print("[FAIL] fetch_asset 返回 None"); raise typer.Exit(1)
    print(f"[OK] Asset: source={asset.source} wav={asset.audio_path} size={asset.audio_path.stat().st_size / 1e6:.1f}MB")
    result = asyncio.run(process_asset(asset, task))
    if result is None:
        print("[FAIL] process_asset 返回 None"); raise typer.Exit(1)
    print(f"[OK] Quality: score={result.qr.score} passed={result.qr.passed}")
    print(f"[OK] Duration: {result.duration_sec}s")
```

**3 种典型用法:**
1. ` `uv run python scripts/spike_bill_jc_full.py --college-id <cid> --list-only --root-label "技术分享" --limit 5` ` → 列出 5 条视频供挑选
2. ` `uv run python scripts/spike_bill_jc_full.py --college-id <cid> --kng-id <id>` ` → 端到端跑单视频(用户主路径)
3. ` `uv run python scripts/spike_bill_jc_full.py --college-id <cid> --kng-id <id> --resolution 360p` ` → 走 360p 档带宽优先

### 4.5 ` `tests/test_internal_site_spider.py` ` (改 ~ 120 行)

7 个 placeholder 测试改写 + 5 个新测试:
- ` `test_fetch_m3u8_calls_preinit_then_kngplay` `(mock httpx 两次 POST)
- ` `test_fetch_m3u8_selects_requested_resolution` `(mock kngPlay 返回 3 档,assert 选 720p)
- ` `test_fetch_m3u8_falls_back_to_first_if_resolution_missing` `(720p 不存在时 fallback)
- ` `test_fetch_m3u8_raises_runtimeerror_on_401_with_cookie_hint` `(mock 401,assert RuntimeError 包含 "Chrome debug")
- **` `test_list_tasks_walks_tree_then_paginates` `(mock tree 返回 nested 树 + 2 个 leaf, pagelist mock 返回 videos; assert 调 pagelist 的 catalogId 对得上)**
- **` `test_list_tasks_filters_by_root_label` `(mock tree 含多个子树,root_label="技术分享" 只走该子树)**
- **` `test_list_tasks_respects_limit` `(mock pagelist 返回 5 条,limit=3,assert VideoTask 数量 = 3)**
- **` `test_list_tasks_skips_empty_leaves` `(mock tree 含 kngCount=0 节点,assert 不调 pagelist)**
- **` `test_list_tasks_returns_video_task_with_bill_jc_url` `(assert VideoTask.url 格式 = https://b-learning.bill-jc.com/learn/<kng_id>)**
- ` `test_extract_m3u8_audio_success` `(subprocess mock / 真 ffmpeg fixture)
- ` `test_extract_m3u8_audio_fails_on_ffmpeg_nonzero` `(assert RuntimeError)
- ` `test_fetch_asset_internal_spider_uses_extract_m3u8_audio` `(集成测试:mock strategy 返回 SubtitleResult, mock extract_m3u8_audio, assert called with m3u8 url)

---

## 5. 测试策略

### 5.1 单元测试(每个 task 至少 1 happy + 1 fail)

- ` `tests/test_internal_site_spider.py` `: 7 个新测试(实装 §4.5)
- ` `tests/test_extract_audio.py` `: 2 个新测试(` `test_extract_m3u8_audio_*` `)
- ` `tests/test_fetch_asset.py` `: 1 个新测试(` `test_fetch_asset_internal_spider_uses_extract_m3u8_audio` `)

### 5.2 验收(spec §5.2 扩展 Phase 9.6)

1. **单元测试**:` `uv run pytest tests/test_internal_site_spider.py tests/test_extract_audio.py tests/test_fetch_asset.py -v` ` 全绿
2. **spike list 模式**:` `uv run python scripts/spike_bill_jc_full.py --college-id <cid> --list-only --root-label "技术分享" --limit 5` ` → 打印 ≥ 1 条 kng_id + title
4. **spike 端到端**(用户手动启 Chrome debug + 填真实 kng_id):
   ```
   uv run python scripts/spike_bill_jc_full.py --kng-id "<real_kng_id>" --college-id "<cid>" --verbose
   ```
   预期输出:
   - `[OK] Asset: source=whisper_internal_download wav=/tmp/.../<id>.wav size=N.NMB`
   - `[OK] Quality: score=NN passed=True|False`
   - wav 文件存在,文件大小符合预期(720p / 3h 视频 → ~150MB wav,720p / 1h → ~50MB wav)
   - 字幕 *.cleaned.txt 落盘 ` `cfg.storage.transcribed_dir` `
5. **磁盘检查**:` `df -h /tmp` ` spike 完成后 tmp_dir 不应有遗留 mp4 文件(只有 wav + sidecar)
6. **Cookie 过期 fail-fast**:手动 ` `killall Chrome` ` 后再跑 spike,应看到清晰 hint "Chrome debug 未启动 / cookie 已失效"

---

## 6. 文件清单

**新增 (3 个):**
- ` `scripts/spike_bill_jc_full.py` `(200 行)
- ` `docs/superpowers/specs/2026-09-09-bill-jc-spider-impl-design.md` `(本文件)
- 单元测试 / 集成测试(§4.5)

**修改 (3 个):**
- ` `src/vla/subtitle/internal_site_spider.py` `(stub → 实装, 40 行 → 150 行)
- ` `src/vla/transcribe/extract.py` `(+ 30 行 ` `extract_m3u8_audio` `)
- ` `src/vla/main_provider.py` `(fetch_asset path ② 改 5 行)
- ` `tests/test_internal_site_spider.py` `(4 placeholder → 7 真测试)
- ` `docs/superpowers/backlog.md` `(§1 InternalSiteSpider 标 done / 部分 done)

---

## 7. 风险与缓解

| 风险 | 概率 | 缓解 |
|---|---|---|
| m3u8 URL token 时效短(几小时) | 高 | ` `fetch_m3u8` ` 每次都重新调,不缓存 |
| cookie 过期 | 中 | ` `cookie_ttl_sec` ` 默认 30 min,过期时清晰 hint |
| yunxuetang API 改版 | 低 | spike 提供 `--fake-server` 模式回退测试 |
| ffmpeg 流式抽音比本地文件慢 | 中 | 实测 1h 视频 ~30s 抽音,可接受 |
| Chrome debug 端口冲突 | 低 | 默认 9222,可 `--cdp-url` 覆盖 |
| httpx 在公司网络代理问题 | 中 | httpx 默认 trust_env=True,沿用现有 proxy 设置 |

---

## 8. 后续(均走 ` `backlog.md` `,本轮不在 scope)

- ` `InternalSiteSpider.list_tasks` ` (tree + pagelist) → backlog §1,本轮留 stub
- start / near-end 截图 → backlog §6
- F2-10 scan ready 检测 → backlog §1
- video_source / audio_source 工厂统一 → backlog §5
- 截图异步化 → backlog §4

---

## 9. Task 分解(预计 6 个)

按 SDD 执行,subagent-driven:

1. **spec 落地**(已完成,本文件)
2. **InternalSiteSpider.fetch_m3u8 实装**(TDD:先 httpx mock,再 Playwright cookie helper)
3. **InternalSiteSpider.list_tasks 实装**(TDD:mock tree + pagelist,验证 _flatten_leaves + 累积到 limit)
4. **extract_m3u8_audio + fetch_asset path ② 改造**(TDD)
5. **spike 脚本**(spike_bill_jc_full.py 3 模式 + 装配)
6. **end-to-end 验证 + 文档收尾**(backlog §1 done / implementation-plan.md Phase 9.6)

每个 task 一个 implementer subagent + 一个 task reviewer + final whole-branch review(opus)。