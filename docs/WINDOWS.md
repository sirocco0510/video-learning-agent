# video-learning-agent — Windows 部署与运行指南

> **状态**:2026-09-10 轻量化后首次支持 Windows。Tab Audio Recorder 浏览器扩展 + macOS 系统通知已删除,Windows 走 `NullNotifier`(静默,自动跳过弹窗)。
>
> **目标**:在 Windows 上跑通 `scripts/spike_bill_jc_full.py`(bill-jc 内网学习平台转写端到端)。

---

## 一、Windows 相对 mac 的差异

| 维度 | macOS | Windows |
|---|---|---|
| **系统通知** | `osascript` + `terminal-notifier`(可点 Finder) | **静默**(`NullNotifier`,不弹通知不打断) |
| **弹窗询问(FR-2.5/2.6)** | A 级 AppleScript 阻塞弹窗 | **不弹** — `NullNotifier.ask_open_browser` 直接返 `"skip"` |
| **Tab Audio Recorder 扩展** | 已删除(2026-09-10 轻量化) | — |
| **Chrome CDP 9222** | 必需(cookie 借取) | 必需(同左) |
| **faster-whisper** | ✅ | ✅(同代码,跨平台) |
| **yt-dlp** | ✅ | ✅(同代码,跨平台) |
| **ffmpeg** | ✅ | ✅(需单独装) |
| **Playwright** | ✅(已用 sync + to_thread) | ✅(同代码) |

**结论**:Windows 不需要任何额外 Python 包。同一份 `uv` lockfile 直接装,代码改动仅在 macOS 走真通知路径,Windows 走静默 fallback。

---

## 二、Windows 环境准备(一次)

### 2.1 系统前置

| 软件 | 版本 | 安装方式 |
|---|---|---|
| **Windows** | Win 10/11 64-bit | — |
| **Python** | 3.12.x | [python.org](https://www.python.org/downloads/windows/) — 安装时勾选 "Add to PATH" |
| **uv** | ≥ 0.4 | `pip install uv` 或 `winget install astral-sh.uv` |
| **ffmpeg** | ≥ 6.0 | `winget install ffmpeg` 或 [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) — 解压后 `bin/` 加 PATH |
| **Chrome** | 任意版本 | 默认安装路径 `C:\Program Files\Google\Chrome\Application\chrome.exe` |

### 2.2 验证前置

```powershell
python --version          # Python 3.12.x
uv --version              # uv 0.4+
ffmpeg -version           # ffmpeg 6.0+ with --enable-libfdk-aac(默认带)
chrome --version          # Google Chrome
```

### 2.3 装项目依赖

```powershell
cd D:\path\to\video-learning-agent
uv sync                   # 装 pyproject.toml + uv.lock 全套 Python 包
uv run python -c "import vla.cli; print('OK')"   # 验证主模块可导入
```

---

## 三、Chrome CDP Debug 模式(cookie 借取用)

bill-jc / B站等需登录的站点,我们借 Chrome 已登录的 cookie(`InternalSiteSpider._borrow_auth`)。

### 3.1 启 Chrome debug 9222

**PowerShell(管理员或普通都行,Chrome 单实例模式)**:

```powershell
# 步骤 1:先关所有 Chrome(CDP 单实例锁)
Stop-Process -Name chrome -Force -ErrorAction SilentlyContinue

# 步骤 2:启 Chrome debug,user-data-dir 用独立目录(避免污染正常 profile)
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir="D:\tmp\chrome-debug-vla" `
  --no-first-run `
  --no-default-browser-check
```

### 3.2 验证 Chrome 可达

```powershell
curl http://localhost:9222/json/version
# 应返回 {"Browser": "Chrome/...", ...}
```

### 3.3 登录目标站点

在刚弹出的 Chrome 里手动登录 `https://b-learning.bill-jc.com`,保持窗口开着。spike 跑的时候不要关。

---

## 四、跑 bill-jc spike(Windows 端到端)

### 4.1 拿 college_id 和 kng_id

两种方式任选:

**A. 自动发现(推荐)**:

```powershell
uv run python scripts/spike_bill_jc_full.py `
  --college-id <从 bill-jc URL 拿> `
  --list-only
```

示例:

```powershell
uv run python scripts/spike_bill_jc_full.py `
  --college-id 7c80b070-28ac-4c1a-b54b-35b327b870eb `
  --list-only --root-label "技术分享" --limit 5
```

输出:`<kng_id> | <title> | <url>` 一行一条。

**B. 手动拿 URL**:
- 打开 `https://b-learning.bill-jc.com`
- 浏览目录,点进某个视频 → URL 形如 `https://b-learning.bill-jc.com/learn/<kng_id>`

### 4.2 跑单视频端到端

```powershell
uv run python scripts/spike_bill_jc_full.py `
  --college-id 7c80b070-28ac-4c1a-b54b-35b327b870eb `
  --kng-id 51dcbb20-848b-4d94-8a5b-313e5bda24ec `
  --cdp-url http://localhost:9222
```

流程:
1. spider 调 yunxuetang 4 API(tree / pagelist / preinit / kngPlay)拿 m3u8
2. ffmpeg 直接抽 m3u8 音轨为 wav(若失败 fallback 到浏览器内 MediaRecorder)
3. faster-whisper 转写
4. 质量门控(LLM 评分 ≥ 50,语速 ≥ 0.5 cps)
5. Refine(L2 语义清理)
6. 落盘 `<logs>/transcribed/<kng_id>_<title>.txt` + `<id>.summary.txt`

### 4.3 输出文件位置

| 文件 | 路径 | 说明 |
|---|---|---|
| **wav 临时文件** | `<tmp>/audio_raw/<id>.wav` | 中间产物,转写完后自动删 |
| **cleaned.txt** | `<logs>/transcribed/<id>_<title>.txt` | 主字幕文件(Refine 后) |
| **summary.txt** | `<logs>/transcribed/<id>_<title>.summary.txt` | 长视频 200-300 字摘要(可选) |

### 4.4 常见错误

| 错误 | 原因 | 解决 |
|---|---|---|
| `Connection refused localhost:9222` | Chrome debug 没启 | 按 §3 启动 Chrome |
| `kngPlay 401 Unauthorized` | Cookie 未借到 | 在 Chrome 里手动登录 bill-jc 后重试 |
| `ffprobe not found` | ffmpeg 没装或不在 PATH | `winget install ffmpeg` 后重开 PowerShell |
| `out of memory` | faster-whisper model 太大 | 改 `config/vla.yaml` 的 `whisper.model: tiny` 或 `base` |
| `transcribe 一直 0%` | ffmpeg 解密失败(DRM) | 走 `extract_browser_audio` 兜底(已自动) |

---

## 五、跑 `vla process` CLI(主流程)

```powershell
# B站(无需 Chrome debug,API 命中即可)
uv run vla process `
  --url "https://www.bilibili.com/video/BV1xxx" `
  --title "视频标题" `
  --duration 1800 `
  --real-provider

# bill-jc(需 Chrome debug)
uv run vla process `
  --url "https://b-learning.bill-jc.com/learn/<kng_id>" `
  --title "内训课程" `
  --duration 3600 `
  --real-provider
```

`vla doctor` 在 Windows 上正常跑,仅 terminal-notifier / 屏幕录制相关检查显示 WARN(无影响):

```powershell
uv run vla doctor
# 期望:[OK] Python >= 3.11 / [OK] ffmpeg / [OK] faster_whisper / 等
#       [WARN] terminal-notifier: 未安装(mac only)
#       [WARN] screenshot_tcc: 未启用
```

---

## 六、与 mac 的差异速查(代码层)

| 模块 | macOS 行为 | Windows 行为 |
|---|---|---|
| `vla.ui.notifier.create_notifier()` | 返 `MacOSNotifier`(osascript + terminal-notifier) | 返 `NullNotifier`(静默) |
| `notifier.ask_open_browser()` | A 级 AppleScript 阻塞弹窗 30s | 立即返 `"skip"` |
| `notifier.alert_blocking()` | A 级弹窗 | no-op |
| `notifier.info()` / `warning()` | B 级通知 | no-op |
| `vla.subtitle.strategy._try_browser` | 弹窗 → 用户响应 → 标记 plugin_status | 弹窗 skip → plugin_status unavailable → 走兜底 |
| `MacOSNotifier` 类本身 | ✅ 仍存在(macOS-only) | 不会 import(条件 import) |

**为什么不需要 mock**:Windows 上 `create_notifier()` 走 `sys.platform != "darwin"` 分支,根本不导入 `MacOSNotifier`,所以 pyobjc / osascript 调用不会触发。

---

## 七、性能 / 磁盘注意

| 项 | 值 |
|---|---|
| **wav 中间文件** | 3 小时视频 ≈ 200MB,转写完后自动删 |
| **faster-whisper 模型** | `small` ≈ 460MB,`base` ≈ 140MB,`tiny` ≈ 75MB(首次跑会下载) |
| **模型缓存位置** | `%LOCALAPPDATA%\huggingface\hub\`(自动) |
| **磁盘峰值** | < 1 GB(wav + 模型 + 转写缓存) |

---

## 八、进阶:PyInstaller 单文件 exe(可选)

> **状态**:未在主线启用(2026-09-10)。如需要单文件 exe,后续可加 `pyinstaller.spec` 走 spec hiddenimports 模式。

目前 Windows 推荐 `uv run` 启动,避免 PyInstaller 打包 playwright / faster-whisper / yt-dlp 时漏 hiddenimports 的麻烦。

---

## 九、故障排查 checklist

- [ ] Chrome debug 9222 已启?`curl http://localhost:9222/json/version` 有响应?
- [ ] Chrome 里已登录 bill-jc / B站?cookie 没过期?
- [ ] ffmpeg 在 PATH?`ffmpeg -version` 打印版本?
- [ ] `uv sync` 跑过?`uv.lock` 在仓库根?
- [ ] Python 3.12?`python --version`?
- [ ] `.env` 有 `OPENAI_API_KEY`?QualityChecker / Refiner 要用
- [ ] `config/vla.yaml` `platforms.internal_site.enabled: true`?

---

## 十、相关文档

- `requirements.md` — 需求 SSOT(FR-1 ~ FR-10)
- `README.md` — 项目门户
- `docs/superpowers/specs/2026-09-09-bill-jc-spider-impl-design.md` — bill-jc spider 设计 spec
