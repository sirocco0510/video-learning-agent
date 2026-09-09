# Asset Pipeline Producer-Consumer 重构设计 (2026-09-09) — Option B

> **Status:** Draft for user review.
> **Scope:** 把 `text_provider` 拆成两条独立链路 — 输入链(产字幕文件或音频文件) + 处理链(转写 + 质量 + 优化 + 落盘 + 清理);scan_today_dir 转写从 strategy 内部挪到处理链;纳入 F2-10 ready 检测;清理历史 `Cmd+Shift+R` 类残留注释;**新增内部学习平台(`b-learning.bill-jc.com`)作为视频源**(复用 Chrome CDP SSO cookie → 3 个 yunxuetang API → m3u8 URL → ffmpeg → wav,**不**开 BrowserDriver)。
> **Predecessor:** 讨论起点 = `main.py:9` 注释 "返回 audio_path 让主调度知道质量失败时音频是否需要保留" 的协议别扭,以及 spike_f26_pipeline 实测中暴露的"录制中 vla 抢半成品 webm" race。
> **Out of scope:** 截图异步化(已 user deferred,见 `backlog.md` 项 4)、FR-2.27 worker 池并发、FR-2.22 三路径清理 — 全部走 backlog 后续 PR。**InternalSiteSpider API 实现细节**(3 个 yunxuetang API 调用 + Chrome cookie 借用)— 本轮只在 `fetch_asset` 加分支接住 `SubtitleResult(metadata={"video_url": "<m3u8>"})`,Spider 实现走单独 PR;Spider 类本身也走单独 PR。

---

## 1. 背景与动机

### 1.1 当前耦合点

`main.py::_process_one` 一条串行线串起 7 个不同性质的步骤:

```
Phase A 截图
  → text_provider (字幕三级 + 抽音 if needed)
  → quality check (LLM 同步)
  → refiner (LLM 同步)
  → save
  → unlink audio (if passed)
  → Phase C 截图
```

具体问题:
1. **抽音 / 转写 / 质量耦合在 `text_provider` 一次调用里** — 调用方不知道"拿到的是 text 还是 audio_path"
2. **audio_path 二态协议泄露 audio 生命周期** — `text_provider` 返回 `(text, source, audio_path)`,`audio_path` 实际只有 `None`(无音频,如 API/Browser 命中)或 wav(VideoSourceFactory 兜底抽出的 wav)两种状态,但调用方 `main.py` 仍要按"质量 pass 删 / fail 留"分支处理 — 把"音频是否要清理"这个实现细节泄露成了协议的一部分
3. **抽音埋在 `transcriber.transcribe` 里** — 抽音和转写写在同一个方法:`transcriber.transcribe(video_path)` 内部既调 ffmpeg 抽 mp4/webm → wav,又跑 faster-whisper,还顺手 unlink 视频源(FR-3.3)。这让"抽音是辅助功能"的设计原则失语 — 调用方没法独立使用抽音,也没法独立使用转写
4. **scan_today_dir 隐藏转写在 strategy 内部** — `strategy._try_browser` 弹窗 enabled 分支**已经**调 `transcriber.transcribe(webm)` 内部完成,RealTextProvider 只看到 text。这意味着:
   - 处理链(`process_asset`)无法参与"是否要转写"的决策
   - F2-10 ready 检测(避免录制中抢半成品 webm)没有合适的注入点 — 在 strategy 内部加,处理链看不到;挪到 RealTextProvider,又得在 transcribe 之后再挪回去,绕路
   - audio 生命周期"用户产物不删 / 我方产物删"的语义被 strategy 内部吞掉,RealTextProvider 拿不到 webm 路径
5. **main.py 处理 audio 生命周期分支** — 跟"主调度该干嘛"的关注点不对齐;转写与质量本应是一条消费链,现在拆在 `_process_one` 里跟"截图 + history + quota"混在一起
6. **历史架构残留注释** — 大量 `Cmd+Shift+R` / `Tab Audio Recorder` / `tab_recorder.start_recording` 类的注释分布在 src/ scripts/ tests/,有些已与 F2-10 后的代码不符(描述旧 F2-8 自动化路径),有些仍有效(用户操作指引),需逐个核对清理

### 1.2 期望效果

- **职责清晰**:输入链只产文件(无论 text 还是 audio),处理链只消费文件
- **类型即文档**:`Asset` 数据类的字段直接说明"有 text 还是有 audio_path / 是不是用户产物",不再靠 `audio_path is None` 反推
- **scan_today_dir 透明化**:strategy 只返回 webm 路径(`SubtitleResult.audio_path`),
转写和 sidecar 标记由处理链统一负责(fetch_asset 抽 webm → wav,process_asset 转写 + touch sidecar)
- **main.py 瘦身**:不再处理 audio_path 多态,只负责调度
- **可演进**:Asset 是数据类,后续接入 worker 池(FR-2.27)只需把 Asset 装进 queue,不改契约

### 1.3 设计原则(用户明确点)

> **抽音频是为了抽取字幕的辅助功能**。如果能拿到字幕文件,就不需要抽音;反过来,所有抽出来的音频都是为转写字幕服务的,没有别的目的。

推论:
- 输入链只产两样东西:**字幕文件**(API/Browser 命中时直接拿到 text)或**音频文件**(抽出来等转写)
- 处理链负责消费音频文件:**转写 → 质量门控 → 优化(Refine) → 落盘 → 清理**
- 输入链中**不调 transcriber**;处理链中**不抽音**
- **抽音责任归输入链(`fetch_asset`)**:无论来源是 webm(scan)还是 mp4(VideoSourceFactory 兜底),`fetch_asset` 都用 ffmpeg 抽成 wav 再交出去
- **`transcriber.transcribe` 变纯**:只做 Whisper 转写,不做 ffmpeg 抽音
- 这样 `Asset.audio_path` 永远是 wav,统一可喂;`transcriber` 单一职责

---

## 2. 架构总览

### 2.1 两链边界 = 文件落地

```
main.run() [串行循环,本轮不改并发]
  │
  for task in pending:
    │
    ├─ Phase A 截图(side effect,主失败 → 跳视频,继续同步)
    │
    ├─【输入链】fetch_asset(task) → Asset | None
    │      ├─ 字幕策略(API / Browser / scan_today_dir / internal_spider)
    │      │     · API/Browser 命中 → 直接返回 Asset(text=..., audio_path=None)
    │      │     · scan_today_dir 命中 → 抽出 webm → wav
    │      │       返回 Asset(text=None, audio_path=wav, source="whisper_scan",
    │      │                    deletable=True)
    │      │     · internal_spider 命中(yunxuetang 3 个 API + Chrome SSO cookie 拿 m3u8 URL)
    │      │       → ffmpeg 抽 m3u8 → wav
    │      │       返回 Asset(text=None, audio_path=wav, source="whisper_internal_download",
    │      │                    deletable=True)
    │      ├─ 全 miss → VideoSourceFactory 下载 MP4
    │      │     · 抽出 MP4 → wav,unlink MP4(FR-3.3)
    │      │     · 返回 Asset(text=None, audio_path=wav, source="whisper_download",
    │      │                    deletable=True)
    │      └─ 全失败 → 返回 None
    │
    ├─【处理链】process_asset(asset, task) → ProcessResult | None
    │      ├─ if asset.text: text = asset.text         [API/Browser]
    │      │  else:
    │      │     · transcriber.transcribe(asset.audio_path) → text  [纯 Whisper]
    │      │     · touch sidecar (scan 路径)
    │      ├─ checker.check(text) → qr
    │      ├─ if qr.passed:
    │      │     ├─ refiner.refine(text)         [可选]
    │      │     ├─ save_transcribed(...)
    │      │     └─ unlink(audio_path)            [if asset.deletable]
    │      └─ if not passed: log fail, return None
    │
    └─ Phase C 截图(side effect,继续同步)
```

**关键契约**:`Asset.audio_path` 永远是 wav(已抽好音),`transcriber` 不再接触 mp4/webm。

**抽音责任**:`fetch_asset` 内部调 `extract_audio(input, output)`(ffmpeg helper),
具体调用点:
- scan 路径:webm → `today_dir/<bvid>.wav`(webm 留作用户产物,wav 作我方 temp)
- MP4 路径:mp4 → `save_dir/audio_raw/<bvid>.wav`,然后 unlink MP4

### 2.2 与现有 FR 的关系

| FR | 现状 | 拆法后 | 备注 |
|---|---|---|---|
| FR-2.5~2.10 字幕三级 | `strategy.get_subtitle` | 不变,封装到 `fetch_asset`;但 ② enabled 分支不再内部 transcribe | 弹窗逻辑保留 |
| FR-2.14 策略 ③ 二级降级 | `audio_factory.extract` (yt-dlp -x) | 不变,封装到 `fetch_asset`;RealTextProvider 兜底仍走 VideoSourceFactory(本轮不切) | 工厂切换属 backlog §5 |
| FR-2.16d webm ready 检测 | ❌ 无 | **本轮不做**,backlog §1 | 用户决定先不做 |
| FR-3.1~3.9 转写 | `transcriber.transcribe(video_path)` 抽音 + 转写合一 | `transcriber.transcribe(audio_path)` 签名变纯(只 Whisper);抽音迁到 `fetch_asset` 内部调 `extract_audio()` | 抽音抽走(transcribe 不再含 ffmpeg) |
| FR-3.3 音频就绪后立即删视频源 | 在 `transcriber.transcribe` 内部 | 迁到 `fetch_asset` 兜底分支(MP4 路径),抽完即 unlink MP4 | transcriber 不再接触 mp4/webm |
| FR-3.7 音频清理 | `main.py` 二态分支 | 迁到 `process_asset` 内部,按 `Asset.deletable` 决策 | **本轮焦点** |
| FR-3.8/3.9 Level 1/4 字幕清理 | `transcriber.transcribe` 内部 + main.py 调 refiner | 不变,挪到 `process_asset` | |
| FR-4.5/4.6 质量门控通过/失败 | `main.py` 分支 | 迁到 `process_asset` 内部 | |
| FR-7.7 transcribed 落盘 | `log.save_transcribed` | 不变,挪到 `process_asset` | |
| FR-2.28 Phase A/C 截图 | `main.py` await | **本轮不动,继续同步** | user deferred |

---

## 3. 数据契约

### 3.1 `Asset` 数据类(新,替换 audio_path 三元组)

```python
# src/vla/models.py
@dataclass(frozen=True)
class Asset:
    """输入链产出:字幕(text)或音频(audio_path),二选一。

    字段语义:
    - text: 字幕已就绪(API/Browser 命中)
    - audio_path: **wav**(统一已抽好音);scan 路径来自 fetch_asset 抽 webm,wav 是我方 temp(可删),webm 留原位(用户产物);download 路径来自 fetch_asset 抽 MP4,MP4 已 unlink(FR-3.3)
    - source: 区分来源,影响 main.run 写 history / 配额统计
    - deletable: 质量 pass 后是否 unlink 该 wav
    """
    text: str | None
    source: str              # "api" | "browser" | "whisper_scan" | "whisper_download"
    audio_path: Path | None  # 永远是 wav(由 fetch_asset 抽好)
    deletable: bool = False

    @property
    def needs_transcribe(self) -> bool:
        return self.audio_path is not None
```

| 路径 | text | audio_path | source | deletable | needs_transcribe |
|---|---|---|---|---|---|
| ① API 命中 | `"..."` | `None` | `"api"` | n/a | False |
| ② Browser 命中(纯字幕) | `"..."` | `None` | `"browser"` | n/a | False |
| ② scan_today_dir(F2-10) | `None` | `<today>/<bvid>.wav`(`fetch_asset` 抽自 webm) | `"whisper_scan"` | **True**(wav 是我方 temp;webm 留原位) | True |
| ③ VideoSourceFactory 兜底 | `None` | `<tmp>/<bvid>.wav`(`fetch_asset` 抽自 MP4,后 unlink MP4) | `"whisper_download"` | **True**(我方产物) | True |
| ④ **内部源 spider(2026-09-09 新)** | `None` | `<save_dir>/<id>.wav`(`fetch_asset` 抽自 m3u8) | `"whisper_internal_download"` | **True**(我方产物) | True |
| 全失败 | (返回 None,不算 Asset) | | | | |

**`Asset.audio_path` 永远是 wav**:无论 scan webm 还是 MP4 兜底,`fetch_asset` 内部都用 `extract_audio()` helper 把原始音频抽成 wav 再返回。
这样 `transcriber.transcribe(wav)` 签名变纯(单一职责:Whisper),处理链不用关心 ffmpeg。

**source 拆分为 `whisper_scan` / `whisper_download` / `whisper_internal_download`** 的目的:让 `main.run` 写 history 和 quota 时能区分"用户手动录制"vs"我方兜底抽音"vs"内部学习平台",为后续配额策略(FR-9)和 history 审计(FR-9.6)留口子。

### 3.2 `SubtitleResult` 数据类(改 1 个字段)

`vla/models.py::SubtitleResult` 当前是:
```python
class SubtitleResult(BaseModel):
    text: str
    source: str
    metadata: dict | None = None
```

**本轮改**:增加 `audio_path: Path | None = None` 字段(默认 None 兼容既有调用方)。

strategy 弹窗 enabled 分支返回时填 audio_path=webm,其他分支不填。这样:
- `strategy.get_subtitle` 可以携带 webm 路径给上层
- fetch_asset 从 SubtitleResult 构造 Asset
- strategy 自身**不调 transcriber**

### 3.3 `ProcessResult` 数据类(新,替换 source 字符串返回)

```python
@dataclass(frozen=True)
class ProcessResult:
    """处理链结果。"""
    text: str           # 最终文本(refined 或 cleaned 或原文)
    qr: QualityResult
    source: str         # 跟 Asset.source 一致
    duration_sec: int
```

`main.py::_process_one` 返回 `str | None`(原返回 source 给 main.run 写 history),新设计返回 `ProcessResult | None`。`main.run` 拿 `result.source` 写 history。

### 3.4 错误信号(继续用 None)

| 失败位置 | 返回 | 内部已 log |
|---|---|---|
| `fetch_asset` 全失败 | `None` | `log_transcribe_fail(stage="fetch_asset", error="all paths exhausted")` |
| `process_asset` 转写失败 | `None` | `log_transcribe_fail(stage="transcribe", error=...)` |
| `process_asset` 质量失败 | `None` | `log_quality_fail(...)` |
| 成功 | `ProcessResult` | `save_transcribed` 内部 |

---

## 4. 组件改动

### 4.1 `src/vla/models.py`(+ Asset, ProcessResult;改 SubtitleResult)

```python
class SubtitleResult(BaseModel):
    text: str | None = None       # scan 路径可空
    source: str
    audio_path: Path | None = None  # 新增
    metadata: dict | None = None

@dataclass(frozen=True)
class Asset:
    text: str | None
    source: str
    audio_path: Path | None
    deletable: bool = False
    @property
    def needs_transcribe(self) -> bool: ...

@dataclass(frozen=True)
class ProcessResult:
    text: str
    qr: QualityResult
    source: str
    duration_sec: int
```

### 4.2 `src/vla/main_provider.py`(拆 RealTextProvider + build_text_provider)

**改前**:
- `RealTextProvider.__call__(task) -> (text, source, audio_path)`
- `build_text_provider(...) -> Callable[[VideoTask], tuple[str, str, Path | None]]`

**改后**:
- `FetchAssetFn = Callable[[VideoTask], Awaitable[Asset | None]]`
- `ProcessAssetFn = Callable[[Asset, VideoTask], Awaitable[ProcessResult | None]]`
- `RealTextProvider` 拆成两个内部方法 + 一个 wrapper:
  - `async def fetch_asset(self, task) -> Asset | None` — 字幕策略 + 抽音
  - `async def process_asset(self, asset, task) -> ProcessResult | None` — 转写 + 质量 + refine + save + cleanup
  - `__call__` 可选,转发到 fetch_asset + process_asset(保持向后兼容,给老测试用)
- `build_text_provider(...) -> tuple[FetchAssetFn, ProcessAssetFn]`

**`fetch_asset` 关键代码**:
```python
async def fetch_asset(self, task: VideoTask) -> Asset | None:
    url = str(task.url)
    duration_sec = task.expected_duration

    # 1. 字幕三级策略(含 FR-2.5/2.6 popup 流程)
    try:
        result = await self.strategy.get_subtitle(url, duration_sec)
    except Exception as e:
        logger.warning("策略调用异常,降级到 source_factory: %s", e)
        result = None

    if result is not None and result.text is not None:
        # 字幕命中(API / Browser) → 直接 text
        return Asset(
            text=result.text,
            source=result.source,
            audio_path=None,
            deletable=False,
        )

    if result is not None and result.audio_path is not None:
        # scan 命中(webm 用户产物) → ffmpeg 抽成 wav
        webm_path = result.audio_path
        wav_path = webm_path.with_suffix(".wav")
        try:
            extract_audio(webm_path, wav_path)
        except Exception as e:
            logger.warning("scan webm 抽音失败 %s: %s", webm_path, e)
            return None
        return Asset(
            text=None,
            source="whisper_scan",
            audio_path=wav_path,
            deletable=True,  # wav 是我方 temp;webm 留原位不删
        )

    # 1b. 内部学习平台 spider(2026-09-09 新增)→ m3u8 URL → ffmpeg → wav
    # 数据流:Chrome CDP 借 SSO cookie → 3 个 yunxuetang API(tree/pagelist/kngPlay)
    # → kngPlay 返回 playDetails[].url 即 m3u8 URL
    # InternalSiteSpider.spider(task) 返回 SubtitleResult(source="internal_spider",
    # metadata={"video_url": "<m3u8>", "resolution": "720p"})
    if result is not None and (result.source or "").startswith("internal"):
        video_url = (result.metadata or {}).get("video_url")
        if not video_url:
            logger.warning("internal spider 返回无 video_url,跳过: %s", result)
            return None
        wav_path = self._save_dir / "audio_raw" / f"{task.id}.wav"
        try:
            # ffmpeg 直处理 m3u8 playlist(自动下载 + 合并 + 解码)
            # m3u8 是公开 CDN URL,无需注入 cookie(签名 token 在 URL 里)
            extract_audio(video_url, wav_path)
        except Exception as e:
            logger.warning("internal spider m3u8 抽音失败 %s: %s", video_url, e)
            return None
        return Asset(
            text=None,
            source="whisper_internal_download",
            audio_path=wav_path,
            deletable=True,
        )

    # 2. 全失败 → VideoSourceFactory 兜底(下载 MP4 → 抽 wav → 删 MP4)
    # 工厂切换(→ AudioSourceFactory)属 backlog §5,本轮保持现状
    try:
        source = self.source_factory.get(url, task.id, duration_sec)
    except Exception as e:
        logger.warning("source_factory.get 失败: %s", e)
        return None

    video_path = source.path
    wav_path = video_path.with_suffix(".wav")
    try:
        extract_audio(video_path, wav_path)
    except Exception as e:
        logger.warning("MP4 抽音失败 %s: %s", video_path, e)
        return None
    # FR-3.3:抽完即删视频源
    try:
        video_path.unlink()
    except Exception as e:
        logger.warning("unlink MP4 失败 %s,继续:%s", video_path, e)

    return Asset(
        text=None,
        source="whisper_download",
        audio_path=wav_path,
        deletable=True,
    )
```

**抽音 helper**(从 `transcribe/streaming.py::_extract_audio` 拆出):
```python
# src/vla/transcribe/extract.py (新)
def extract_audio(input_path: Path, output_path: Path) -> None:
    """ffmpeg 抽 input → wav(output_path)。

    Args:
        input_path: 任意 ffmpeg 支持的格式(mp4/webm/m3u8/...)
        output_path: 目标 wav 路径(需 .wav 后缀)

    Raises:
        RuntimeError: ffmpeg 返回非 0(细看 stderr)
        FileNotFoundError: ffmpeg 二进制不存在或 input 不存在

    失败语义: 半截 wav **会被 finally 删掉**,避免 3 小时视频抽到一半崩了
    留 345MB 残文件占磁盘。
    """
    cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-ar", "16000", "-ac", "1", "-f", "wav", str(output_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"extract_audio failed: {input_path} → {output_path}: {proc.stderr}"
            )
    except Exception:
        # 删半截 wav(若有)— 不要累积垃圾
        if output_path.exists():
            try:
                output_path.unlink()
            except OSError:
                pass  # best-effort
        raise
```

**`process_asset` 关键代码**:
```python
async def process_asset(self, asset: Asset, task: VideoTask) -> ProcessResult | None:
    # Step 1: 转写(如果需要)
    if asset.needs_transcribe:
        try:
            text = await asyncio.to_thread(
                self.transcriber.transcribe, asset.audio_path
            )
        except Exception as e:
            self.log.log_transcribe_fail(
                task.id, task.title, str(task.url),
                stage="transcribe", error=str(e),
            )
            return None
        # F2-10:scan 路径转写成功后 touch sidecar,避免下次重复扫
        if asset.source == "whisper_scan" and asset.audio_path is not None:
            try:
                asset.audio_path.with_suffix(".transcribed.txt").touch()
                self.log.info("已标记已转写: %s", asset.audio_path)
            except Exception as e:
                self.log.warning("touch sidecar 失败 %s: %s", asset.audio_path, e)
    else:
        text = asset.text  # API/Browser 命中,直接用

    # Step 2: 质量门控
    qr = self.checker.check(
        text=text,
        title=task.title,
        duration_sec=task.expected_duration,
        model_size=self.cfg.whisper.model,
    )

    # Step 3: 失败分支
    if not qr.passed:
        self.log.log_quality_fail(task.id, task.title, str(task.url), qr, text)
        # FR-2.11:browser 字幕质量不过关 → 标 unavailable
        if asset.source == "browser":
            self.plugin_status.mark_unavailable(reason="plugin_quality_fail")
            logger.warning("⚠️ 插件字幕质量不过关,降级到 Whisper")
        # v3.2 (FR-3.9):quality_fail → 不删 audio,保留供 retry
        return None

    # Step 4: Refine(FR-3.9,仅 passed 文本)
    if self.cfg.quality_check.refine_enabled and self.refiner is not None:
        try:
            refinement = self.refiner.refine(text, title=task.title)
            if refinement.cleaned_text:
                text = refinement.cleaned_text
                logger.info("✨ Refine 完成:%d corrections", len(refinement.corrections))
        except Exception as e:
            logger.warning("⚠️ Refine 失败,使用原文继续:%s", e)

    # Step 5: save_transcribed(FR-4.5 + FR-7.7)
    self.log.save_transcribed(
        video_id=task.id,
        title=task.title,
        text=text,
        quality=qr,
        source=asset.source,
        duration_sec=task.expected_duration,
    )

    # Step 6: 清理音频(FR-3.7 + FR-4.5)
    # 仅 deletable=True 时删;scan 路径(用户产物)不删
    if asset.deletable and asset.audio_path is not None and asset.audio_path.exists():
        try:
            asset.audio_path.unlink()
            logger.info("🗑️ 清理音频: %s", asset.audio_path)
        except Exception as e:
            logger.warning("⚠️ 删音频失败 %s,主流程继续:%s", asset.audio_path, e)

    return ProcessResult(
        text=text,
        qr=qr,
        source=asset.source,
        duration_sec=task.expected_duration,
    )
```

### 4.3 `src/vla/subtitle/strategy.py`(_try_browser 弹窗 enabled 分支:不调 transcribe)

**改前**:
```python
# 5. "enabled" 分支
audio_path = scan_untranscribed_audio(today_dir)
if audio_path is None: return None
text = self.transcriber.transcribe(audio_path, out_dir=today_dir)  # ← 内部 transcribe
# touch sidecar
audio_path.with_suffix(".transcribed.txt").touch()
return text, {"via": "tab_audio_recorder", "method": "scan_today_dir", "audio_path": str(audio_path), ...}
```

**改后**:
```python
# 5. "enabled" 分支 — 只扫描,不做 transcribe
audio_path = scan_untranscribed_audio(today_dir)
if audio_path is None: return None
# 把 webm 路径交给 fetch_asset,由 fetch_asset 抽音成 wav
return SubtitleResult(text=None, source="whisper_scan", audio_path=audio_path)
```

`_try_browser` 的返回签名需扩展。当前返回 `tuple[str, dict] | None`,改为:
```python
def _try_browser(self, adapter, url, duration_sec) -> SubtitleResult | None:
    """返回 SubtitleResult(由 strategy.get_subtitle 包);或 None(全 miss/降级 ③)。"""
```

这样 `strategy.get_subtitle` 也只返回 `SubtitleResult | None`,不再返回 `(text, meta)` 元组。
strategy 自身**不调 transcriber**;**不调 extract_audio**;只做"扫 webm 路径"。

### 4.4 `src/vla/main.py`(_process_one 改薄)

**改后**:
```python
async def _process_one(self, task) -> str | None:
    # Phase A 截图(围在外层,主失败 → 跳视频)
    if self._screenshot is not None and self._chrome_session_ready:
        try:
            await self._screenshot.phase_a_start(...)
        except Exception as e:
            logger.warning("📸 Phase A 截图失败,跳过视频 %s:%s", task.title, e)
            self.failure_alert.check_after_write()
            return None

    # 输入链
    asset = await self.fetch_asset(task)
    if asset is None:
        self.log.log_transcribe_fail(
            task.id, task.title, str(task.url),
            stage="fetch_asset", error="all paths exhausted",
        )
        self.failure_alert.check_after_write()
        return None

    # 处理链
    result = await self.process_asset(asset, task)
    if result is None:
        # 已 log_quality_fail / log_transcribe_fail 内部
        self.failure_alert.check_after_write()
        return None

    # 通知
    self.notifier.info("✓ 质量通过", f"{task.title}({result.qr.score}分),已加入总结队列")

    # Phase C 截图(围在外层,失败 log warning 不阻塞)
    if self._screenshot is not None and self._chrome_session_ready:
        try:
            await self._screenshot.phase_c_end(...)
        except Exception as e:
            logger.warning("📸 Phase C 截图失败:%s", e)

    return result.source  # main.run() 写 history 用
```

`VideoLearningAgent.__init__` 注入签名:
- 改前:`text_provider: TextProvider`
- 改后:`fetch_asset: FetchAssetFn, process_asset: ProcessAssetFn`

### 4.5 ~~F2-10 ready 检测~~ → **本轮不做**

user 决定(2026-09-09):ready 检测先不做,后续单独 PR。代码维持现状 `audio_scan.py::scan_untranscribed_audio` 不加 mtime 宽限。详见 `backlog.md` §1。

### 4.6 `src/vla/transcribe/streaming.py`(`transcribe` 签名变纯)

**改前**:`transcribe(video_path, out_dir=None) -> str`
- 行为: 抽 mp4/webm → wav → faster-whisper → 返回 text;内部还 unlink 视频源(FR-3.3)

**改后**:`transcribe(audio_path: Path) -> str`
- 行为: **纯 Whisper**,不做 ffmpeg 抽音,不顺手删视频源(FR-3.3 改归 `fetch_asset`)
- audio_path 假定为 wav(由 `fetch_asset` 抽好);若传 mp4/webm 直接报错(契约)

**改动要点**:
- 删 `_extract_audio` 方法(逻辑迁到 `transcribe/extract.py::extract_audio`)
- 删内部 MP4/webm unlink 逻辑(FR-3.3,改归 `fetch_asset`)
- 删 `out_dir` 参数(无意义了 — 抽音已迁走,产物 wav 路径由调用方定)

**与 spec §1.3 设计原则一致**:transcriber 单一职责(Whisper),
不再泄漏 ffmpeg / 文件生命周期细节。

### 4.7 `src/vla/subtitle/internal_site_spider.py`(新,API-driven spider)

**职责**:通过 3 个 yunxuetang API 把"b-learning.bill-jc.com 的视频 task"变成"m3u8 URL"。

**核心数据流**:
```
task(kngId)
   │
   ├─ POST /kng/kngCatalog/student/tree     # 拿 catalog 树
   │     body: {"pmType": "0", "collegeId": "<cid>"}
   │     → 用于按叶子 catalog id 拿视频列表(本接口本 spider 不调 pagelist,
   │       实际是从 strategy 已扫到的 task 里拿 kngId,直接跳到 kngPlay)
   │
   ├─ POST /kng/knowledge/pagelist           # 拿子目录下视频列表(可选,本 spider 不直接调)
   │     body: {"collegeId": "<cid>", "catalogId": "<leaf_id>", ...}
   │     → 实际集成时由 strategy 在 yield task 前调一次
   │
   └─ POST /kng/study/kngPlay                # 拿 m3u8 URL(关键 API)
         body: {"kngId": "<kngId>", "courseId": "", "fullname": "",
                "lang": "", "studyParam": {"originOrgId": "", "previewType": 0},
                "targetCode": "kng", "targetId": "",
                "targetParam": {"taskId": "", "projectId": "", "flipId": "", "batchId": ""},
                "customFunctionCode": ""}
         → 返回 playDetails[].url = m3u8 URL(720p/480p/360p 三档)
```

**认证**:复用用户 Chrome CDP session 的 SSO cookie(不重新登录)。
实现思路:连 `localhost:9222` → `context.cookies()` → 拿 yunxuetang.cn / bill-jc.com 域 cookie → 转 Cookie header 注入 requests 调用。

**类签名(预留,实现走单独 PR)**:
```python
class InternalSiteSpider:
    def __init__(self, cdp_url: str, college_id: str, resolution: str = "720p"):
        ...
    async def list_tasks(self) -> list[VideoTask]:
        """走 tree + pagelist,递归拉所有叶子 catalog 下的视频,生成 VideoTask 列表。"""
    async def fetch_m3u8(self, kng_id: str) -> str:
        """调 kngPlay,按 resolution 选 playDetails[].url 返回。"""
```

**fetch_asset 集成点**:`strategy.get_subtitle` 调 `InternalSiteSpider.list_tasks()` 之前已经
确定这是 internal 源的 task。返回的每个 task 应该有 `url = "internal://<kngId>"` 之类的占位。
实际拿 m3u8 的时机:fetch_asset 拿到 SubtitleResult(source="internal_spider") 时,才调 `fetch_m3u8`。
(本轮不实装 InternalSiteSpider,只扩 fetch_asset 接住 metadata contract。)

**为什么不开 BrowserDriver**(经探勘 probe_bill_jc_play.py 验证):
- kngPlay 返回完整 m3u8 URL,无需解析页面 DOM
- 浏览器只用来借 cookie,不参与抓取逻辑
- 探勘实测:m3u8 URL 是公开 CDN(`video.bill-jc.com/conversion/...`),签名 token 在 URL 里,无需 cookie 拉取
- 减少 chrome page 开销 + 不抢用户焦点

**探勘脚本(本轮一并落盘)**:
- `scripts/probe_bill_jc.py` — 抓详情页 network,确认 HLS/m3u8 模式
- `scripts/probe_bill_jc_api.py` — 抓 catalog tree + pagelist API 真实请求格式
- `scripts/probe_bill_jc_play.py` — 抓 play 页 kngPlay API 真实请求格式

三个 probe 都已在本轮跑通,raw 落到 `logs/probe_bill_jc_*.json`。

### 4.8 注释清理(本轮一并做)

按"用户操作指引"(保留)/"代码逻辑描述"(更新)/"已删除功能的历史注释"(清理)三档:

**清理类(描述已删除的 F2-8 自动化)**:
- `src/vla/main_provider.py:147` — 注释说"Tab Audio Recorder"在策略 ③,实际 F2-10 已迁
- `src/vla/capture/screenshot_phase_controller.py:3` — "Tab Audio Recorder fallback"已不对
- `src/vla/audio/source_factory.py:33, 64` — "path ② TabAudioRecorder"已不对
- `src/vla/audio/queue.py:18` — "Tab Audio Recorder 路径用 extension-assigned id"已不对
- `src/vla/subtitle/strategy.py:272` — "录制路径(Screen Recorder / Tab Audio Recorder)"Screen Recorder 已删
- `src/vla/subtitle/internal_site_adapter.py:30, 48` — `tab_recorder` import / 参数已 unused(F2-10 删除)
- `src/vla/subtitle/strategy.py:43, 195` — type hint `tab_recorder: TabAudioRecorder`已 unused

**更新类(行为对,但描述冗长或模糊)**:
- `src/vla/subtitle/strategy.py:379, 452` — "用户按 Cmd+Shift+R 启停"精简为"用户手动录屏"
- `src/vla/subtitle/strategy.py:289` — "Tab Audio Recorder 路径迁到..."精简为"scan 路径迁到..."
- `src/vla/subtitle/bilibili_adapter.py:17` — "Tab Audio Recorder 改"用户手动下载 → 代码扫今天目录",路径迁到..."精简

**保留类(用户操作指引或 F2-10 仍有效)**:
- `src/vla/ui/macos_notify.py:170` — popup 提示文案"Cmd+Shift+R"用户要看
- `src/vla/subtitle/tab_audio_recorder.py` — 类本身(还有 `probe_status` 用) + 用户操作文案
- `src/vla/subtitle/audio_scan.py:6` — 注释"用户把 ... 手动下载到今天..."仍准
- `src/vla/config.py:109` — 注释"用户把 ... 拖到 ..."仍准
- `src/vla/subtitle/platform_adapter.py:10, 39, 74-75` — F2-10 历史变更说明(可保留作 audit)
- `tests/test_e2e.py:51, 694, 696` — 扩展名 "Free Tab Audio Recorder" 是实际插件名,valid
- `tests/test_macos_notify.py:289` — 测试用插件名,valid
- `tests/test_tab_audio_recorder.py` — 测试 recorder 类本身,valid
- `scripts/spike_f26_pipeline.py:11, 27, 32, 240, 246` — 涉及全名匹配 F2-6.2 变更,valid
- `scripts/spike_f25_full_pipeline.py:8, 22, 128, 198` — 历史 spike,描述已过时但保留作 audit

具体每个文件的改动在 §6 文件清单中列出。

---

## 5. 测试策略

### 5.1 单元测试

| 测试文件 | 覆盖 |
|---|---|
| `tests/test_extract_audio.py`(新) | `extract_audio(input, output)` 5 分支:正常 ffmpeg 调用 / ffmpeg 返回非 0 抛 RuntimeError **+ output 被删** / 输入不存在抛 FileNotFoundError **+ output 不存在时不动** / 输出已存在时 `-y` 覆盖 / ffmpeg 二进制缺失抛 FileNotFoundError |
| `tests/test_fetch_asset.py`(新) | `fetch_asset` 6 个分支:API 命中 / Browser 命中 / scan 命中(fetch_asset 内抽 webm→wav, asset.audio_path=wav) / internal_spider 命中(抽 m3u8→wav, asset.source="whisper_internal_download") / VideoSourceFactory 兜底(抽 mp4→wav, asset.audio_path=wav) / 全失败 |
| `tests/test_process_asset.py`(新) | `process_asset` 6 分支:无 audio 直接 text / wav transcribe / 质量 pass + unlink audio / 质量 fail + 不 unlink / transcribe fail / scan 路径 touch sidecar |
| `tests/test_subtitle_strategy.py`(改) | `_try_browser` 弹窗 enabled 分支返回 `SubtitleResult(text=None, source="whisper_scan", audio_path=webm)`,**不**调 transcribe,**不**抽音 |
| `tests/test_main.py`(扩) | `_process_one` 集成:接 fetch_asset + process_asset stub,验证 Phase A/C 截图 + 通知 + failure_alert 触发 |
| `tests/test_video_learning_agent.py`(改) | stub 接口从 `(text, source, audio_path)` 改成 `(Asset | None, ProcessResult | None)` |
| `tests/test_transcribe_streaming.py`(改) | `transcribe(audio_path)` 签名变更 — 删 out_dir 参数;断言不调 extract_audio,不顺手 unlink 视频源 |
| `tests/test_e2e.py` | **本轮不修**(已知 broken,见 `backlog.md` 和历史 memory) |

### 5.2 验收

沿用 `implementation-plan.md` 每个 Phase 末尾的"验收代码"块惯例。本轮作为新一节 `## Phase 9.5: Asset Pipeline 重构(Option B)`,验收:

```bash
# 1. 单元测试
uv run pytest tests/test_extract_audio.py tests/test_fetch_asset.py \
  tests/test_process_asset.py tests/test_subtitle_strategy.py \
  tests/test_main.py tests/test_video_learning_agent.py \
  tests/test_transcribe_streaming.py -v

# 2. spike 跑通(完整 F2-10 流程)
uv run python scripts/spike_f26_pipeline.py \
  --url "https://www.bilibili.com/video/BV1DUgK6cEi3" \
  --duration 60 \
  --prep-webm tmp/audio_raw/BV1DUgK6cEi3.wav \
  --force-popup \
  --auto-response enabled

# 3. 验证 audio_path 协议消失
grep -rn "audio_path" src/vla/main.py  # 应该没有 (除注释外)

# 4. 验证 transcriber 签名变纯
grep -n "_extract_audio\|out_dir" src/vla/transcribe/streaming.py  # 都不应有

# 5. 验证 Cmd+Shift+R 残留清理
grep -rn "Cmd+Shift+R\|tab_recorder\.start_recording" src/ scripts/ tests/ \
  | grep -v "macos_notify\|tab_audio_recorder\.py\|test_e2e\|test_macos_notify\|test_tab_audio_recorder\|spike_f26\|spike_f25" \
  # 应只剩保留类
```

---

## 6. 文件清单(总)

| 文件 | 改动类型 | 说明 |
|---|---|---|
| `src/vla/models.py` | 改 | + `Asset` / `ProcessResult` dataclass;`SubtitleResult` + `audio_path` 字段 |
| `src/vla/main_provider.py` | 改 | `RealTextProvider` 拆成 `fetch_asset` + `process_asset`;`build_text_provider` 返回二元组;清理 line 147 Tab Audio Recorder 注释 |
| `src/vla/main.py` | 改 | `_process_one` 改薄;注入 `fetch_asset` + `process_asset`;audio_path 处理迁出 |
| `src/vla/subtitle/strategy.py` | 改 | `_try_browser` 弹窗 enabled 分支返回 `SubtitleResult(text=None, audio_path=webm)`,不调 transcribe;清理 Tab Audio Recorder 历史注释(line 272, 289, 379, 452) |
| `src/vla/transcribe/extract.py` | **新** | `extract_audio(input_path, output_path)` 模块级函数(从 streaming._extract_audio 拆出) |
| `src/vla/transcribe/streaming.py` | 改 | `transcribe(audio_path: Path) -> str` 签名变纯;删 `_extract_audio` 方法 + 内部 MP4 unlink(FR-3.3 改归 fetch_asset) |
| `src/vla/subtitle/audio_scan.py` | 不改 | 维持现状(ready 检测走 backlog) |
| `src/vla/subtitle/internal_site_spider.py` | **新**(本轮只占位 stub) | API-driven spider:`InternalSiteSpider(cdp_url, college_id, resolution)`,方法 `list_tasks()` + `fetch_m3u8(kng_id)`;实现走单独 PR |
| `src/vla/capture/screenshot_phase_controller.py` | 改 | 清理 line 3 "Tab Audio Recorder fallback" 注释 |
| `src/vla/audio/source_factory.py` | 改 | 清理 line 33, 64 "path ② TabAudioRecorder" 注释 |
| `src/vla/audio/queue.py` | 改 | 清理 line 18 "Tab Audio Recorder 路径" 注释 |
| `src/vla/subtitle/internal_site_adapter.py` | 改 | 删 unused `tab_recorder` import / 参数(空类保留作 legacy) |
| `src/vla/subtitle/bilibili_adapter.py` | 改 | 精简 line 17 "Tab Audio Recorder 改..." 注释 |
| `src/vla/subtitle/strategy.py`(tab_recorder type hint) | 改 | 删 unused `tab_recorder: TabAudioRecorder` type hint |
| `scripts/spike_f26_pipeline.py` | 改 | 装配改用 `build_text_provider` 新返回 |
| `scripts/probe_bill_jc.py` | 新(已存在,探勘用) | 连 Chrome CDP 抓 b-learning.bill-jc.com 详情页网络,确定 m3u8 模式 |
| `scripts/probe_bill_jc_api.py` | 新(已存在,探勘用) | 抓 catalog tree + pagelist 真实请求格式(Chrome 内 XHR 拦截) |
| `scripts/probe_bill_jc_play.py` | 新(已存在,探勘用) | 抓 play 页 kngPlay API 真实请求格式(route 拦截响应) |
| `tests/test_extract_audio.py` | 新 | 4 分支覆盖(ffmpeg helper,含 m3u8 路径 stub) |
| `tests/test_fetch_asset.py` | 新 | 6 分支覆盖(fetch_asset 6 路径,含 internal_spider m3u8) |
| `tests/test_process_asset.py` | 新 | 6 分支覆盖(process_asset) |
| `tests/test_subtitle_strategy.py` | 改 | 弹窗 enabled 分支返回 SubtitleResult, 不调 transcribe,不抽音 |
| `tests/test_transcribe_streaming.py` | 改 | `transcribe(audio_path)` 新签名;断言不调 ffmpeg,不删视频源 |
| `tests/test_main.py` | 改 | 扩 stub 接口 |
| `tests/test_video_learning_agent.py` | 改 | stub 接口对齐 |
| `docs/superpowers/specs/2026-09-09-asset-pipeline-design.md` | 新 | 本文档 |
| `docs/superpowers/backlog.md` | 改 | F2-10 ready 检测维持 backlog §1(本轮不做) |
| `implementation-plan.md` | 改 | + Phase 9.5 节,验收代码如 §5.2 |

---

## 7. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| `RealTextProvider` 拆完后,e2e 测试全 broken | 阻塞 CI | 本轮 e2e 不动,单元测试覆盖新接口 |
| scan_today_dir 透明化后,e2e/老测试期望 strategy 返回 text | 行为差异 | 单测覆盖:_try_browser 弹窗 enabled 分支只返 SubtitleResult(audio_path=webm);老 e2e 不动 |
| audio_path 二态删除后,scan wav 我方产物被 unlink,但 webm 用户产物保留 | 设计意图:wav 是我方 temp 删,webm 是用户产物留;spec §3.1 已明确 deletable=True for scan wav | 单测覆盖:process_asset 只删 wav 不删 webm |
| build_text_provider 返回类型从 Callable 改 tuple | 现有调用方需改 | 列出所有调用方,在 spike_f26_pipeline.py / tests 改完 |
| AudioSourceFactory 切换属 backlog §5,本轮不切 | RealTextProvider 兜底仍用 VideoSourceFactory(MP4 下载) | spec 已标注;测试覆盖兜底路径行为不变 |
| internal_spider 实现未落地,本轮只占位 | fetch_asset 的 internal 分支等不到 SubtitleResult(source="internal_spider") 喂入 → 路径永远走不到 | spec 已标注"实现走单独 PR";fetch_asset 分支用 fixture 覆盖测试;真路径留 e2e/手动验证 |
| ~~m3u8 拉取需 SSO cookie~~ — **已探勘确认 m3u8 是公开 CDN URL,签名 token 在 URL 里,无需 cookie 拉取** | 无 | 探勘记录见 `logs/probe_bill_jc_play_*.json` |
| InternalSiteSpider 现有空类,本轮不实装(走单独 PR) | fetch_asset 内部新分支无 integration test 验证 | 单测覆盖 fetch_asset 接住 SubtitleResult(metadata={"video_url": "..."}) 的行为;Spider 实装走单独 PR + e2e |
| yunxuetang API 调用需 Chrome SSO cookie(yunxuetang.cn / bill-jc.com 域) | 没 cookie API 直接 401 | InternalSiteSpider 连 `localhost:9222` 借 Chrome cookie;用户未启 Chrome debug 时 fail-fast,提示"请启 Chrome debug" |
| yunxuetang API 字段 / URL 变更(平台重构) | Spider 集成测试 break | 探勘脚本(probe_bill_jc_api.py / probe_bill_jc_play.py)留作回归工具;Spider 实装 PR 必跑 probe 验证 API 仍 200 |
| 注释清理可能误删 valid 引用 | 维护者困惑 | §4.8 列出三档分类;review 时逐个核对;保留类的 grep 校验如 §5.2 验收 #5 |

---

## 8. 后续(均走 `backlog.md`,本轮不在 scope)

| 项 | 优先级 | 关联 |
|---|---|---|
| FR-2.27 worker 池并发 | P1 | backlog §2 |
| FR-2.22 三路径音频清理 | P1 | backlog §3 |
| 截图异步化 | P2(已 user deferred) | backlog §4 |
| video_source / audio_source 工厂统一 | P3 | backlog §5 |
| **F2-10 scan ready 检测**(回到 backlog) | P0(用户明示) | backlog §1 |
