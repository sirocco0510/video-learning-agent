# video-learning-agent — Windows 部署与运行指南

> **状态**:2026-09-10 轻量化后首次支持 Windows。Tab Audio Recorder 浏览器扩展 + macOS 系统通知已删除,Windows 走 `NullNotifier`(静默,自动跳过弹窗)。
>
> **目标**:在 Windows 上跑通 bill-jc 内网学习平台转写的**两条路径** ——
> 单视频(`scripts/spike_bill_jc_full.py`,§四)和课程目录批量(`uv run vla learn`,§五)。

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
- 浏览目录,点进某个视频 → URL 形如 `https://b-learning.bill-jc.com/kng/#/video/play?kngId=<kng_id>&...`
  (`kngId` 在**查询参数**里;`/learn/<kng_id>` 这种 path 形式不存在)

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
3. faster-whisper 转写(`initial_prompt` 强制简体,FR-3.10)
4. **Level 4 Refine**(云端 LLM 清错别字 / 统一繁简;`refine_enabled` 时)
5. **质量门控**(LLM 评分 ≥ 50,语速 ≥ 0.5 cps)
6. 长视频单视频摘要(FR-2.15d,200-300 字)
7. 落盘 `<logs>/transcribed/<YYYY-MM-DD>/transcripts/<id>_<title>.txt`
   + `<logs>/transcribed/<YYYY-MM-DD>/summaries/<id>_<title>.summary.txt`

> ⚠️ **Refine 在门控之前**(步骤 4 在 5 前),这是有意为之:未精修的文本进
> 门控会拿低分(2026-09-10 实测同一视频 未注入 Refiner 45 分 vs 注入后 88 分)。
> 两处云端调用**都算「字幕质量检查」配额**,不额外开新用途。

> ⚠️ 抽音受 `audio.max_extract_sec` 限制(默认 **1800 秒 = 30 分钟**),超出部分
> **直接丢弃** —— 超长视频只会转出前 30 分钟,这不是 bug。

### 4.3 输出文件位置

| 文件 | 路径 | 说明 |
|---|---|---|
| **wav 临时文件** | `<tmp>/audio_raw/<id>.wav` | 中间产物,转写成功后**立即删**(FR-3.7,不等门控) |
| **transcript.txt** | `<logs>/transcribed/<YYYY-MM-DD>/transcripts/<stem>.transcript.txt` | Whisper 原始输出(总写) |
| **refined.txt** | `<logs>/transcribed/<YYYY-MM-DD>/transcripts/<stem>.refined.txt` | Refine 后(**仅未通过的条目残留** —— 成功条目在 Step 6 被删,见下) |
| **主字幕** | `<logs>/transcribed/<YYYY-MM-DD>/transcripts/<id>_<title>.txt` | 正式产物 |
| **summary.txt** | `<logs>/transcribed/<YYYY-MM-DD>/summaries/<id>_<title>.summary.txt` | 长视频 200-300 字摘要(可选) |

> **成功条目上找不到 `.transcript.txt` / `.refined.txt` 是正常的**:`process_asset`
> 成功路径的 Step 6(`main_provider.py:317`)会调 `discard_transcribe_intermediates(stem)`
> 把它们清掉;只有**失败**条目会在 Step 3 提前返回、把中间产物留在盘上。
> 所以精修有没有降级(`# notes:` 尾巴)事后**只能从运行时的 stdout 看**,
> 别用 `| tail -1` 之类的管道把中间输出截掉。

> 顺带:`<stem>` 与 `<id>_<title>` 是**两套命名**,都落同一个 `transcripts/`
> 目录。核对产物时别数目录里的文件个数,按 `通过 N 条 → N 份 .txt + N 份 .summary.txt` 数。

### 4.4 常见错误

| 错误 | 原因 | 解决 |
|---|---|---|
| `Connection refused localhost:9222` | Chrome debug 没启 | 按 §3 启动 Chrome |
| `kngPlay 401 Unauthorized` | Cookie 未借到 | 在 Chrome 里手动登录 bill-jc 后重试 |
| `ffprobe not found` | ffmpeg 没装或不在 PATH | `winget install ffmpeg` 后重开 PowerShell |
| `out of memory` | faster-whisper model 太大 | 改 `config/vla.yaml` 的 `whisper.model: tiny` 或 `base` |
| `transcribe 一直 0%` | ffmpeg 解密失败(DRM) | 走 `extract_browser_audio` 兜底(已自动) |

---

## 五、跑课程目录批量(`vla learn`)

**适用**:手里是**课程目录页** URL(含 `catalogId` + `cid`),想按目录翻页整门跑完。

> **和 §四 的关系**:两条路径**平级**,不是同一条的两个参数。
> `vla learn` 是 `src/vla/cli.py:501` 的 typer 命令 + `src/vla/learn.py`,
> **不 import 也不调用** `scripts/spike_bill_jc_full.py` —— 单视频才用 spike。
> 两者共享的只是库模块 `vla.subtitle.internal_site_spider.InternalSiteSpider`。

### 5.1 怎么拿到 URL 和那两个参数

**实操**:Chrome 里打开要跑的那门课的**课程目录页**(能看到整门课章节列表的那一页)→ **地址栏整条复制**。URL 长这样:

```text
https://b-learning.bill-jc.com/kng/#/list?catalogId=<X>&cid=<Y>&order=0&sort=0&type=
                                         ^^^^^^^^^^^^      ^^^^^
                                         --catalog-id      --college-id
```

`cid` **就是 collegeId**(不是"课程 ID" —— 名字有误导性)。两个都必须是 UUID,顺序任意。

**真实示例**(可直接替换成你自己的):

```text
https://b-learning.bill-jc.com/kng/#/list?catalogId=3514be39-ee3f-4ad0-a276-d529474c6662&cid=7c80b070-28ac-4c1a-b54b-35b327b870eb&order=0&sort=0&type=
```

→

```powershell
--catalog-id 3514be39-ee3f-4ad0-a276-d529474c6662
--college-id 7c80b070-28ac-4c1a-b54b-35b327b870eb
```

> ⚠️ **别把整条 URL 当参数传**。CLI 收的是 `--catalog-id` / `--college-id` 两个独立
> flag,**没有** `--url`。而且 PowerShell 里 `&` 是**调用运算符**,未加引号的 URL 会被
> 解析成"执行 `https://...` 这条命令"而报错 —— 这也正是为什么要把两个 UUID 拆出来。

> ⚠️ 两个 UUID **都要带**。只给 `catalogId` 缺 `cid` → `vla learn` 会因缺少必填
> `--college-id` 直接报 typer 用法错误(`Missing option`)。

### 5.2 先 `--dry-run` 列清单

```powershell
uv run vla learn `
  --college-id 7c80b070-28ac-4c1a-b54b-35b327b870eb `
  --catalog-id 3514be39-ee3f-4ad0-a276-d529474c6662 `
  --limit 10 `
  --dry-run
```

(两个 UUID 换成 §5.1 从你自己那条 URL 里抄出来的)

`--dry-run` **不装配** transcriber / refiner / LLM(零凭据零副作用),只翻页列条目并标注哪些已转写:

```text
📋 dry-run:目录共 <total> 条 / 已转写 <done> 条 → 本次将处理 <total-done> 条
```

> `--limit` 既是**每页条数**也是**翻页步长**。

### 5.3 真跑

```powershell
uv run vla learn `
  --college-id 7c80b070-28ac-4c1a-b54b-35b327b870eb `
  --catalog-id 3514be39-ee3f-4ad0-a276-d529474c6662 `
  --limit 10
```

把 `--dry-run` 去掉即可,其余参数与 §5.2 完全一致。

- **不需要** `--real-provider`(与 §六 的 `vla process` 不同 —— `vla learn` 内部自己装配真实链路)
- 可选 `--cdp-url`(默认 `http://localhost:9222`)/ `--resolution`(默认 `720p`)/ `--config`
- **全程不弹窗**(FR-11.12):内部站走 m3u8 直抽,不碰浏览器插件路径。Windows 上更是
  走 `NullNotifier`,即使命中降级分支也是静默 → 不会卡在无人应答的弹窗上

### 5.4 输出与停止

```text
📊 课程批量结果:翻页 <pages> 页 / 处理 <processed> / 通过 <passed> / 失败 <failed> / 跳过(已转写)<skipped> / 触发总结 <summarized>
```

| 停止方式 | 日志 | 含义 |
|---|---|---|
| 目录翻完 | `🏁 翻页结束:offset=N 无更多视频` | 全跑完 |
| 配额到 | `🛑 累计配额已到(X.Xh)且 on_exhausted=stop_session → 停止翻页` | 累计 6h,本 session 结束(已跑完的照常落盘) |

> ⚠️ **去重只记成功**。`logs/transcribed_history.jsonl` 由 `agent.run` 在**成功后**写入;
> 失败条目**不写** → 下次跑同一门课会**重跑**。`--dry-run` 的「已转写」也只数成功。
> 另外 spike 从不写这个文件,所以**用 spike 单跑过的视频,批量会再跑一遍**。

> ⚠️ **逐条明细在 stdout 的 `logger.info` 行里**(每条打 score / 时长 / 落盘路径)。
> 别把 `vla learn` 接到 `| tail -1` 之类的管道上 —— 那会把明细和 `# notes:`
> 降级信号一起截掉,只留最后那行统计。

### 5.5 常见错误

| 错误 | 原因 | 解决 |
|---|---|---|
| `📋 dry-run:目录共 0 条` | `catalogId` 给错 / 目录为空 | 回 §5.1 核对 URL,**别**靠调大 `--limit` 硬试 |
| 看到"是否已开启字幕插件"弹窗 | kngId 解析 miss,走错路径了 | 停下核对 URL 形式(§4.1 B),**不要**点"跳过"硬跑 |
| 翻页停不下来 | `--limit` 给了过大值且目录异常 | 先 Ctrl-C,再用 `--dry-run` 确认总条数 |

---

## 六、跑 `vla process` CLI(主流程)

```powershell
# B站(无需 Chrome debug,API 命中即可)
uv run vla process `
  --url "https://www.bilibili.com/video/BV1xxx" `
  --title "视频标题" `
  --duration 1800 `
  --real-provider

# bill-jc(需 Chrome debug)
uv run vla process `
  --url "https://b-learning.bill-jc.com/kng/#/video/play?kngId=<kng_id>" `
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

## 七、与 mac 的差异速查(代码层)

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

## 八、性能 / 磁盘注意

| 项 | 值 |
|---|---|
| **wav 中间文件** | 3 小时视频 ≈ 200MB,转写完后自动删 |
| **faster-whisper 模型** | `small` ≈ 460MB,`base` ≈ 140MB,`tiny` ≈ 75MB(首次跑会下载) |
| **模型缓存位置** | `%LOCALAPPDATA%\huggingface\hub\`(自动) |
| **磁盘峰值** | < 1 GB(wav + 模型 + 转写缓存) |

---

## 九、进阶:PyInstaller 单文件 exe(可选)

> **状态**:未在主线启用(2026-09-10)。如需要单文件 exe,后续可加 `pyinstaller.spec` 走 spec hiddenimports 模式。

目前 Windows 推荐 `uv run` 启动,避免 PyInstaller 打包 playwright / faster-whisper / yt-dlp 时漏 hiddenimports 的麻烦。

---

## 十、故障排查 checklist

- [ ] Chrome debug 9222 已启?`curl http://localhost:9222/json/version` 有响应?
- [ ] Chrome 里已登录 bill-jc / B站?cookie 没过期?
- [ ] ffmpeg 在 PATH?`ffmpeg -version` 打印版本?
- [ ] `uv sync` 跑过?`uv.lock` 在仓库根?
- [ ] Python 3.12?`python --version`?
- [ ] `.env` 有 `OPENAI_API_KEY`?QualityChecker / Refiner 要用
- [ ] `config/vla.yaml` `platforms.internal_site.enabled: true`?

---

## 十一、相关文档

- `requirements.md` — 需求 SSOT(FR-1 ~ FR-11)
- `README.md` — 项目门户
- `docs/superpowers/specs/2026-09-09-bill-jc-spider-impl-design.md` — bill-jc spider 设计 spec
- `.claude/skills/vla-learn-bill-jc/SKILL.md` — 两条路径的交互式引导(URL 校验 + 分流)
