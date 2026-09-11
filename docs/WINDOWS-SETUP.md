# Windows 平台部署 & 应用安装参考

> **来源**:本文件从 `requirements.md` § 十五 抽离(2026-09-10 拆分),与 macOS 的 `README.md` 安装片段对偶。
> **范围**:Windows 平台的环境配置、应用安装、命令对比与踩坑清单 — 不包含业务逻辑(看 `requirements.md`)。

---


> **范围**:本节讲 Windows 平台的项目环境配置(系统要求 → Python → Obsidian → ffmpeg → Chrome 扩展 → doctor 验证 → Windows 特定注意事项)。macOS 用户的步骤见 README 简述 + 本文档 § 四 技术栈 + § 八 配置。
>

## 一、范围与跨平台一致性

>**跨平台一致性**:Windows 与 macOS 共享同一份代码 + 同一份 `pyproject.toml` + 同一份 `vla.yaml`;差异只在 ① 系统截图 API(macOS `screencapture` vs Windows PowerShell System.Drawing)② 系统通知 API(macOS `osascript` vs Windows BurntToast / Win11 toast)。其余行为(Whisper 转写、Tab Audio Recorder 扩展、字幕三级策略、6h 配额、history 去重)完全一致。

### 1.1 系统要求

| 项 | 最低 | 推荐 |
|---|---|---|
| Windows | 10 22H2 (Build 19045) | Windows 11 23H2 |
| RAM | 8 GB | 16 GB+(跑 faster-whisper `large-v3`) |
| 可用磁盘 | 50 GB | 256 GB+(与 macOS 相同的"256 GB 友好"约束) |
| 管理员权限 | 装 uv / ffmpeg 时需要 | — |
| .NET | .NET Framework 4.8(Win10/11 默认装) | — |
| PowerShell | 5.1(系统自带,**不够**) | **PowerShell 7.x**(必须,见 2.2) |
| Git | — | **Git for Windows 2.40+**(必需,见 5.6) |
| Node.js(可选) | — | 20 LTS+(仅装 Claude Code 需要,见 6.7) |

### 2.2 安装 PowerShell 7

> **为什么不用系统自带的 PowerShell 5.1**:5.1 是 Windows 7 时代的遗产,与 uv / .NET 8 / 现代 PowerShell 模块兼容性差,且 `irm`(Invoke-RestMethod)对 GitHub raw URL 处理在 5.1 上有 bug。
>
> **版本澄清**:`Microsoft.PowerShell` winget 包 = PowerShell **7.x**(目前最新 7.4.x,基于 .NET 8,跨平台)。**不是** PowerShell 1.0(那是 2006 年 Windows PowerShell 古老版本,与 PS7 不兼容)。本项目要求的"PowerShell 7"就是指这个包。

```powershell
# 用 winget 装 PowerShell 7(Windows 11 / Server 2022+ 默认带 winget)
winget install Microsoft.PowerShell
# 装完重开 PowerShell 窗口

# 验证(三种方式,任选其一)
pwsh -v                          # 输出 PowerShell 7.4.x(只看 Major.Minor)
$PSVersionTable.PSVersion        # 输出 Major 7 / Minor 4 / Patch x(详细)
$PSVersionTable.PSEdition         # 输出 "Core"(=PS7,而非 "Desktop" = PS5.1)
```

**版本对照表**:

| 输出 | 含义 | 本项目 |
|---|---|---|
| `PowerShell 7.4.x` + `Edition: Core` | ✅ PowerShell 7(本项目要求) | 推荐 |
| `PowerShell 5.1.x` + `Edition: Desktop` | ❌ 系统自带 PS5.1 | **不够**,需升级 |
| `pwsh not recognized` | ❌ PS7 没装或 PATH 没配 | 故障排查见下 |

#### 把 PowerShell 7 设为默认 shell

**Windows 系统默认没有 Windows Terminal** — Win10 早期 / 旧版本只有传统的 PowerShell ISE 或 PowerShell 5.1 控制台。**Windows Terminal 是可选的 UX 增强**,不是必需。下面给三种从零到 PS7 的方法,任选其一。

**方法 A — 直接用 PS7 启动**(最简单,无需任何配置)

装完 PS7 后,系统会出现两个独立入口:

| 入口 | 启动什么 | 位置 |
|---|---|---|
| **PowerShell 7** | `pwsh.exe`(PS7) | 开始菜单搜 `PowerShell 7` → 点开 |
| **Windows PowerShell** | `powershell.exe`(PS5.1) | 开始菜单搜 `Windows PowerShell` → 点开 |

> **关键**:**开始菜单搜 `PowerShell`**(不带 "Windows"),点开第一个结果,这就是 PS7。日常跑 `pwsh -v` / `uv run vla doctor` 都用这个。

**方法 B — 把 PS7 固定到任务栏**(一键启动)

1. 开始菜单 → 搜 `PowerShell 7`
2. 右键 → **"更多" → "固定到任务栏"**
3. 任务栏上点小图标 → 弹出 PS7 终端

**方法 C — Windows Terminal 用户**(Win11 / 已装 WT 的)

如果已经装 Windows Terminal(Win11 默认装,Win10 可 `winget install Microsoft.WindowsTerminal`):

1. `Ctrl + ,`(逗号)打开 WT 设置 GUI
2. 左侧 → **启动**(`Startup`)
3. 右侧 **默认配置文件**(`Default profile`)下拉 → 选 **`PowerShell`**(**不是** `Windows PowerShell`)
   - `PowerShell` = PS7(`C:\Program Files\PowerShell\7\pwsh.exe`)
   - `Windows PowerShell` = PS5.1(`C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe`)
4. 关掉设置 → 重开 Windows Terminal → tab 标题应显示 "PowerShell"

**验证**(任意方法,跑一次确认是 PS7):

```powershell
$PSVersionTable.PSEdition    # 应输出 "Core"(=PS7),不是 "Desktop"(=PS5.1)
pwsh -v                      # 应输出 PowerShell 7.4.x
```

**故障排查**:

| 现象 | 原因 + 修复 |
|---|---|
| 开始菜单搜 `PowerShell 7` 没结果 | PS7 没装 → 重跑 `winget install Microsoft.PowerShell`,**重开开始菜单**(刷新索引) |
| 任务栏固定的是 PS5.1 | 卸了重固定:右键旧图标 → 取消固定,然后方法 B 重做 |
| WT 下拉里只有 `Windows PowerShell` 没 `PowerShell` | WT 没扫到 PS7 → 关掉所有 WT 窗口,重开(WT 启动时扫描) |
| 打开 WT 设置是 JSON 不是 GUI | 高级模式,直接编辑 `defaultProfile` 字段;或在 PowerShell 7 跑 `[Console]::Title = "PS7"` 区分 |

**故障排查 — `pwsh` 命令找不到**:
- 现象:`pwsh -v` 报 `'pwsh' is not recognized as a cmdlet`
- 原因 ①:PowerShell 7 没装 → 重跑 `winget install Microsoft.PowerShell`, 重开 PowerShell 窗口
- 原因 ②:装完后 PATH 没刷新 → 重启 Windows Terminal / 资源管理器
- 原因 ③:用 Windows 自带 PowerShell 5.1 跑的(`powershell.exe` 不是 `pwsh.exe`)→ 显式用 `pwsh` 命令,或在 Windows Terminal 切换默认 profile
- 验证:`$env:PATH -split ';' | Select-String -Pattern 'PowerShell'` 应有 `C:\Program Files\PowerShell\7`

**故障排查 — 装上但显示 5.1**:
- 现象:`pwsh -v` 输出 `PowerShell 5.1.x`(但你装了 PS7)
- 原因:Windows Terminal 默认 profile 还是 PS5.1,显式 `pwsh` 才是 PS7
- 验证:在 PowerShell 7 窗口跑 `$PSVersionTable.PSEdition -eq 'Core'` 应返回 `True`
- 修复:Windows Terminal → 设置 → 默认配置文件 → 选 "PowerShell"(PS7)而不是 "Windows PowerShell"(PS5.1)

**故障排查 — 开始菜单搜不到 "PowerShell 7"**:

**先用命令定位 pwsh.exe**(任何 PS5.1 窗口跑):

```powershell
# A. 全盘搜 pwsh.exe(慢,1-3 分钟)
Get-ChildItem -Path C:\ -Filter pwsh.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -First 5 FullName

# B. 看常见安装路径(快,优先)
$paths = @(
    "$env:ProgramFiles\PowerShell\7\pwsh.exe",
    "${env:ProgramFiles(x86)}\PowerShell\7\pwsh.exe",
    "$env:LOCALAPPDATA\Programs\PowerShell\7\pwsh.exe"
)
$paths | Where-Object { Test-Path $_ }
```

**如果定位到 pwsh.exe**,手动启动它:

```powershell
# 直接跑(临时)
& "C:\Program Files\PowerShell\7\pwsh.exe"

# 或在文件管理器双击 pwsh.exe
explorer "C:\Program Files\PowerShell\7\"
```

**手动创建桌面快捷方式 + 任务栏固定**(保证以后能搜到):

```powershell
# 1. 找到 pwsh.exe 路径(假设上面 B 步骤找到了)
$pwsh = "$env:ProgramFiles\PowerShell\7\pwsh.exe"

# 2. 创建桌面快捷方式
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut("$env:USERPROFILE\Desktop\PowerShell 7.lnk")
$shortcut.TargetPath = $pwsh
$shortcut.WorkingDirectory = $env:USERPROFILE
$shortcut.IconLocation = "$pwsh,0"
$shortcut.Save()

# 3. 验证:桌面应出现 "PowerShell 7" 图标,双击 → 弹出 PS7 终端
```

**开始菜单搜不到但文件存在**的常见原因:

| 原因 | 检查 + 修复 |
|---|---|
| winget 装到非默认路径 | 看 $env:LOCALAPPDATA\Programs\PowerShell\7\ 或 `D:\apps\` 等手动指定路径 |
| winget 装的是 Preview 版 | 开始菜单搜 `PowerShell 7 (Preview)`,带 "(Preview)" 后缀 |
| Microsoft Store 装的是 7 Preview | 与 winget 稳定版共存;卸载 Store 版本只留 winget 版 |
| PATH 没生效 | 跑 `Get-Command pwsh`,看返回路径(应该是 `C:\Program Files\PowerShell\7\pwsh.exe`) |
| Windows Search 索引没刷 | 开始菜单输入框空白 → 等 30s,或重启 `explorer.exe`(`Stop-Process -Name explorer -Force` 后资源管理器自动重启) |
| **根本没装上**(最常见) | 跑 `winget list --name PowerShell`,应列出 `Microsoft.PowerShell` 包;若空 → 重跑 `winget install Microsoft.PowerShell` |

**终极验证** — 用绝对路径直接启动:

```powershell
& "$env:ProgramFiles\PowerShell\7\pwsh.exe" -NoExit -Command '$PSVersionTable.PSVersion.ToString()'
# 应弹出新窗口,标题栏 "pwsh",输出 "7.4.6"
```

### 3.3 安装 Python 3.12 + uv(本项目运行时)

> **关键决策**:Windows 上 Python 来源有坑。优先级:**uv 自带 Python > winget Python > python.org 安装包 > Microsoft Store**。

```powershell
# 1. 装 uv(单命令,uv.exe 自动加 PATH)
irm https://astral.sh/uv/install.ps1 | iex

# 2. uv 下载并管理 Python 3.12(到 ~/.local/share/uv/python/,与系统 Python 隔离)
uv python install 3.12

# 3. 验证
uv python list
# 应输出 cpython-3.12.x-macos-x86_64... 等条目
```

### 4.4 安装 Obsidian(笔记软件)

```powershell
# 官方下载(选 Windows .exe)
# https://obsidian.md/download
```

安装步骤:
1. 双击 `Obsidian.exe` 安装
3. 启动后选 **"Open folder as vault"** → 指向你的 vault 目录(例如 `D:\KnowledgeBase\`)
2. 推荐插件:Dataview(查 YAML)、Templater(模板)、Calendar(日记)、Outliner

**注意事项**:
- vault 路径**不要用 OneDrive 同步目录**(文件锁竞争 + YAML frontmatter 解析 bug)
- vault 路径**不要含中文**(部分插件 path encoding 出 bug)
- 装 Dataview 插件后,query `LIST FROM #type/project` 应能看到本项目页面

### 5.5 安装 ffmpeg(必需)

ffmpeg 用作:yt-dlp -x 抽音频后处理(转 16kHz 单声道 wav,给 Whisper 直吃)。

```powershell
winget install Gyan.FFmpeg
# 或用 chocolatey:
choco install ffmpeg

# 验证
ffmpeg -version
# 应输出版本号 + configuration 含 libmp3lame / libvorbis
```

### 5.6 安装 Git for Windows(必需)

> **为什么必需**:本项目从 GitHub 克隆 → `uv sync` 装依赖 → 后续开发都依赖 Git;Windows 默认**不带** Git(`git` 命令会报 `'git' is not recognized`)。

```powershell
# 方式 A:winget(推荐)
winget install Git.Git
# 装完重开 PowerShell 让 PATH 生效

# 方式 B:官网安装包
# https://git-scm.com/download/win → 下 64-bit Git for Windows Setup → 装

# 验证
git --version
# 应输出 git version 2.40+.windows.1
```

**关键安装选项**(Setup 向导,全部默认即可,例外如下):
- **Default editor**:选 VS Code(若已装)或 Notepad++,不要选 Vim(Windows 用户不熟)
- **PATH environment**:选 **Git from the command line and also from 3rd-party software**(推荐)
- **Line ending conversions**:选 **Checkout Windows-style, commit Unix-style**(避免提交 CRLF 冲突)
- **Terminal emulator**:选 **Use Windows Terminal**(若已装)

**SSH / GPG 配置**(可选):推 GitHub 用 SSH key 时,装完跑 `ssh-keygen -t ed25519`,把 `~/.ssh/id_ed25519.pub` 加到 GitHub → Settings → SSH keys。

### 6.6 克隆项目 + 安装依赖

```powershell
# 1. 克隆(已装 Git for Windows,见 5.6)
git clone https://github.com/<your>/video-learning-agent.git
cd video-learning-agent

# 2. uv 自动装 Python 3.12 + 同步所有依赖(无需手动 venv)
uv sync

# 3. 验证依赖(应无 ImportError)
	uv run python -c "import faster_whisper, yt_dlp, typer, pydantic; print('all ok')"
```

`uv sync` 一次完成 ① 创建虚拟环境 ② 装 Python 3.12 ③ 装 pyproject.toml 里所有依赖。无需 `python -m venv`。

### 7.7 首次运行 doctor 验证(FR-2.28.2c)

```powershell
uv run vla doctor
```

doctor 会验证(2026-09-03 升级):

| 验证项                                                 | 失败提示                                             |
| --------------------------------------------------- | ------------------------------------------------ |
| ffmpeg 在 PATH                                       | "未检测到 ffmpeg,请运行 winget install Gyan.FFmpeg"     |
| yt-dlp 可调用                                          | "未检测到 yt-dlp,请运行 uv tool install yt-dlp"         |
| faster-whisper 模型                                   | "模型 large-v3 未下载,运行 vla model download large-v3" |
| `OPENAI_API_KEY` 环境变量                               | "未设云端 API key,质量检查 + 总结不可用"                      |
| **Tab Audio Recorder 扩展 enabled**(FR-2.24a)         | "Tab Audio Recorder 未启用,音频策略 ② 不可用"              |
| **`element.requestFullscreen()` 能正常调用**(FR-2.28.2c) | "全屏 API 失败,FR-2.28 截图功能不可用"                      |
| **PowerShell 截图脚本可用**(FR-2.28,Windows only)         | "首次运行需允许 PowerShell 脚本,Set-ExecutionPolicy..."   |
### 8.8 Chrome + Tab Audio Recorder 扩展

跨平台一致 — Chrome Windows 与 macOS 行为完全一致:
1. Chrome 装扩展(Windows 直接 Chrome Web Store 搜 "Tab Audio Recorder" → Add to Chrome)
2. `chrome://extensions/` 看到扩展 + 开关是 on
3. 首次使用需 user gesture 激活(浏览器右上角点扩展图标)

Windows **不需要** TCC 屏幕录制权限(macOS 需要,Windows PowerShell 截图是用户级 API)。

### 9.9 macOS / Windows 命令对比表

| 动作                         | macOS                                               | Windows(PowerShell 7)                                    |
| -------------------------- | --------------------------------------------------- | -------------------------------------------------------- |
| 装 Python 3.12              | `brew install python@3.12` 或 uv 自带                  | **`uv python install 3.12`**(推荐,见 3.3)                   |
| 装 uv                       | `curl -LsSf https://astral.sh/uv/install.sh \| sh`  | `irm https://astral.sh/uv/install.ps1 \| iex`            |
| 装 ffmpeg                   | `brew install ffmpeg`                               | `winget install Gyan.FFmpeg`                             |
| 装 Obsidian                 | 官方 .dmg 拖进 Applications                             | 官方 .exe 安装包                                              |
| 默认 shell                   | zsh                                                 | **PowerShell 7**(winget 装)                               |
| 系统截图 API                   | `screencapture -x -t png <path>`(~0.3-0.5s)         | PowerShell + `System.Drawing.CopyFromScreen`(~1.5-3s 首次) |
| 截菜单栏/任务栏时间                 | ✅ macOS 菜单栏自带                                       | ✅ Windows 任务栏自带                                          |
| **屏幕录制 TCC 权限**            | **需要**(系统设置 → 隐私与安全性 → 屏幕录制)                        | **不需要**(PowerShell 用户级 API)                              |
| 系统通知 API                   | `osascript -e 'display notification ...'`           | BurntToast module 或 Win11 原生 toast                       |
| `requestFullscreen()` 首次确认 | 浏览器弹"按 Esc 退出"提示                                    | 浏览器弹"按 Esc 退出"提示(同)                                      |
| 路径分隔符                      | `/`(POSIX)                                          | `\` 或 `/`(Python 都接受)                                    |
| 行尾                         | LF                                                  | CRLF(`git config core.autocrlf true`)                    |
| 磁盘峰值占用                     | < 1 GB                                              | < 1 GB(同)                                                |
| Whisper 加速                 | Apple Silicon MLX / CoreML                          | CUDA(NVIDIA) / DirectML(AMD) / CPU                       |
| 项目依赖管理                     | `uv sync`                                           | `uv sync`(同)                                             |
| 配置文件路径                     | `~/Library/Application Support/...` 或项目内 `vla.yaml` | `%APPDATA%\...` 或项目内 `vla.yaml`(推荐项目内,跨平台一致)             |
| History / 转写日志             | `logs/` 项目目录                                        | `logs/` 项目目录(同,推荐 `.gitignore`)                          |
| Chrome user-data-dir       | `/tmp/vla-chrome-debug`(已验证)                        | `%TEMP%\vla-chrome-debug\`                               |

### 10.10 Windows 特定注意事项(踩坑清单)

1. **PowerShell 执行策略首次拦截**:
   首次跑 PowerShell 脚本(FR-2.28 的截图脚本)会被拦截,提示"无法加载,因为在此系统上禁止运行脚本"。
   ```powershell
   Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
   ```
   选 **`Y`**(是)。这只是解除用户级限制,不破坏 Windows 安全。

2. **PowerShell 截图脚本首次 JIT 编译慢**:
   首次跑 `System.Drawing` 截图 ~3s(CLR JIT 编译 .NET 程序集),后续 ~0.5s。无需预热,首次截图多等 2s 可接受。

3. **Windows Defender 误判**:
   Defender 实时保护可能误判 `screencapture` 类命令 → 把项目目录加白名单:`Defender → 病毒和威胁防护 → 排除项 → 添加排除文件夹`。

4. **路径转义**:
   PowerShell 里 `\` 是转义符,**推荐路径统一用 `/`**(Python 跨平台)或用 here-string `@"..."@`:
   ```powershell
   # 错(可能误转义):
   $path = "D:\KnowledgeBase\videos"
   # 对(Python 风格):
   $path = "D:/KnowledgeBase/videos"
   # 或 here-string:
   $path = @"
   D:\KnowledgeBase\videos
   "@
   ```

5. **CRLF vs LF**:
   PowerShell 默认输出 CRLF;git autocrlf 建议:
   ```powershell
   git config --global core.autocrlf true    # Windows:提交时 CRLF→LF
   ```
   避免污染 macOS / Linux 同事的 diff。

6. **多显示器**:
   `Screen.PrimaryScreen.Bounds` 返回主显示器。多显示器时,**确认主显示器是哪个**(Windows 设置 → 显示 → 主显示器)。

7. **中文路径**:
   Obsidian vault 路径含中文没问题,但 Windows Terminal 偶尔编码错乱 → **用 Windows Terminal + UTF-8 编码**(默认就是)。

8. **OneDrive 同步**:
   **不要把 vault / 项目目录放 OneDrive / iCloud / Google Drive 同步路径**(文件锁竞争 + YAML frontmatter 解析 bug + 历史 jsonl 写入异常)。

9. **Tab Audio Recorder Windows 首次激活**:
   Windows 版 Chrome 装扩展后,**首次使用需 user gesture**(浏览器右上角点扩展图标,或在 editor.html 里点"Allow")。

10. **yt-dlp 单独工具**(可选):
    如果想在 PowerShell 直接调 yt-dlp(不通过项目):
    ```powershell
    uv tool install yt-dlp
    yt-dlp --version
    ```

11. **PATH 顺序**:
    PowerShell `$env:PATH` 里 uv 自带 Python(`~/.local/bin`)必须排在系统 Python 之前,否则 `python` 命令指到错版本。`uv sync` 自动处理,但手动装时要小心。

12. **WSL2(可选,)**:
    若用户偏好 Linux 命令行:装 WSL2 Ubuntu,在 WSL 里跑项目(Windows 上只用浏览器/截图)。所有命令跟 macOS 一致。WSL 装 Python + uv:
    ```bash
    curl -LsSf https://astral.sh/uv/install.sh | sh
    ```
    **不推荐用于本项目**(性能略差 + 截图权限 WSL 转发复杂)。

### 11.11 验收(AC-12 Windows 部署)

- [ ] Windows 11 / Windows 10 22H2 上 `uv run vla doctor` 全 OK(所有检查项 pass)
- [ ] `uv python install 3.12` 成功,`uv python list` 含 cpython-3.12
- [ ] `uv sync` 装齐 faster-whisper / yt-dlp / pydantic / typer / pyobjc-framework-Quartz(仅 macOS)等依赖
- [ ] ffmpeg 装好,`ffmpeg -version` 输出含 libmp3lame
- [ ] Obsidian 安装,vault 路径选好(非 OneDrive),Dataview 插件装上
- [ ] PowerShell 7 设默认 shell,`pwsh -v` 输出 7.x
- [ ] `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned` 已设
- [ ] Chrome 装 Tab Audio Recorder 扩展 + `chrome://extensions/` 看到 enabled
- [ ] 处理一条 B 站无字幕视频(走策略 ③ + Tab Audio Recorder),Whisper 转写 + 截图落盘(开头 + 末尾)
- [ ] 截图 PNG 用图像查看器打开,**能看到菜单栏/任务栏时间戳**
- [ ] Windows Defender 不拦截 screencapture 类命令
- [ ] `logs/screenshots/index.jsonl` 写入索引行,`end_ts - start_ts ≈ duration_sec`(±5s)

---


## 二、环境变量 & PATH 配置

> **范围**:把所有安装的应用统一登记 PATH,跑一次验证脚本就能看出缺哪个。
>
> **黄金法则**:**winget / npm / uv 装的应用,99% 会自动加到 PATH**;只有手动下载 .msi / 解压 .zip 的应用才需要手工配置。装完任何工具后,**必须重开 PowerShell 窗口**(PATH 才会刷新)。

### 2.1 各工具 PATH 默认位置

| 工具 | 安装方式 | PATH 默认路径 | 自动加 PATH? |
|---|---|---|---|
| **PowerShell 7** | winget | `C:\Program Files\PowerShell\7\` | ✅ 是 |
| **uv** | irm 脚本 | `%USERPROFILE%\.local\bin\` | ✅ 是 |
| **ffmpeg** | winget / choco | `C:\Program Files\ffmpeg\bin\`(或 `ffmpeg\` 在 PATH) | ✅ 是 |
| **Git for Windows** | winget / Setup | `C:\Program Files\Git\cmd\` | ✅ 是(若向导选 PATH 选项) |
| **Node.js** | winget | `C:\Program Files\nodejs\` | ✅ 是 |
| **Claude Code** | npm 全局 | `%AppData%\npm\`(默认)或自定义 prefix | ✅ 是(但需重开) |
| **Obsidian** | .exe 安装 | —(GUI 应用,**不需要** PATH) | N/A |

### 2.2 一键验证 PATH 脚本

复制粘贴到 PowerShell 7 窗口跑,**看每个工具的 [OK] / [MISS]**:

```powershell
$tools = @(
    @{ name = 'PowerShell 7';  cmd = 'pwsh';                       exe = 'pwsh.exe' },
    @{ name = 'uv';             cmd = 'uv';                          exe = 'uv.exe' },
    @{ name = 'ffmpeg';         cmd = 'ffmpeg';                      exe = 'ffmpeg.exe' },
    @{ name = 'yt-dlp';         cmd = 'yt-dlp';                      exe = 'yt-dlp.exe' },
    @{ name = 'Git';            cmd = 'git';                         exe = 'git.exe' },
    @{ name = 'Node.js';        cmd = 'node';                        exe = 'node.exe' },
    @{ name = 'npm';            cmd = 'npm';                         exe = 'npm.cmd' },
    @{ name = 'Claude Code';    cmd = 'claude';                      exe = 'claude.cmd' },
    @{ name = 'Python 3.12';    cmd = 'python';                      exe = 'python.exe' },
    @{ name = 'Tab Audio Rec';  cmd = 'chrome';                      exe = 'chrome.exe' }
)

Write-Host '=== PATH 验证 ===' -ForegroundColor Cyan
foreach ($t in $tools) {
    $found = (Get-Command $t.cmd -ErrorAction SilentlyContinue) -ne $null
    if ($found) {
        $ver = & $t.cmd --version 2>$null | Select-Object -First 1
        Write-Host ("[OK]   {0,-15} {1,-10} ({2})" -f $t.name, $t.cmd, $ver) -ForegroundColor Green
    } else {
        Write-Host ("[MISS] {0,-15} {1,-10} → 装了吗?PATH 加了吗?" -f $t.name, $t.cmd) -ForegroundColor Red
    }
}
```

**期望输出**(全绿,所有工具都装好):

```
[OK]   PowerShell 7    pwsh       (PowerShell 7.4.6)
[OK]   uv              uv         (uv 0.4.x)
[OK]   ffmpeg          ffmpeg     (ffmpeg version 6.1.x)
[OK]   yt-dlp          yt-dlp     (2024.x.x)
[OK]   Git             git        (git version 2.43.x)
[OK]   Node.js         node       (v20.x.x)
[OK]   npm             npm        (10.x.x)
[OK]   Claude Code     claude     (1.x.x)
[OK]   Python 3.12     python     (Python 3.12.x)
[OK]   Tab Audio Rec   chrome     (Google Chrome 124.x)
```

### 2.3 手工加 PATH(应急)

如果验证脚本某个工具 `[MISS]`,而你已经装了,说明 PATH 没生效或没加。手动补:

```powershell
# 1. 临时加(只对当前 PowerShell 窗口生效,关掉就丢)
$env:PATH += ';C:\Program Files\ffmpeg\bin'

# 2. 永久加到用户 PATH(无需管理员,推荐)
[Environment]::SetEnvironmentVariable(
    'PATH',
    ($env:PATH + ';C:\Program Files\ffmpeg\bin'),
    'User'   # 'User' = 当前用户; 'Machine' = 全机器(需管理员)
)
# 重开 PowerShell 生效

# 3. 删除路径(误加了)
$currentPath = [Environment]::GetEnvironmentVariable('PATH', 'User')
$newPath = ($currentPath -split ';' | Where-Object { $_ -ne 'C:\Program Files\ffmpeg\bin' }) -join ';'
[Environment]::SetEnvironmentVariable('PATH', $newPath, 'User')
```

**路径注意**:
- Windows PATH 用 `;` 分隔(Linux/macOS 是 `:`)
- 加路径时**不要加末尾的 `\`**,否则会变成 `;;` 双分号
- 路径含空格(如 `C:\Program Files\`)无需引号,在 PATH 列表里自动处理

### 2.4 PATH 顺序(避坑)

**PowerShell 解析 PATH 是按顺序找**,第一个匹配的 exe 生效。**常用优先级**:

```
1. uv 自带 Python     %USERPROFILE%\.local\share\uv\python\...     ← 必须排第一
2. npm 全局 bin       %AppData%\npm                                   ← 装 Claude Code 后才生效
3. Git cmd            C:\Program Files\Git\cmd
4. ffmpeg bin         C:\Program Files\ffmpeg\bin
5. Node.js            C:\Program Files\nodejs
6. PowerShell 7       C:\Program Files\PowerShell\7
7. 系统 PATH          C:\Windows\System32\...
```

**关键避坑**:
- **uv 自带 Python 必须排第一**,否则 `python` 命令指到系统 Python 3.x(可能是 3.8/3.11,与项目要求 3.12 不符)。uv 自动管理,无需手配。
- **winget / npm 装的应用会自动按上述顺序**;手动解压 .zip 的工具(如某些 ffmpeg build)必须手排在前。

### 2.5 用户环境变量(其他重要变量)

| 变量名 | 用途 | 本项目值示例 |
|---|---|---|
| `OPENAI_API_KEY` | 云端 LLM API key(质量检查 + 总结) | `sk-ant-...`(放 .env,不入 PATH) |
| `ANTHROPIC_API_KEY` | Claude Code API 计费(若不走 OAuth) | `sk-ant-...` |
| `PYTHONUTF8` | 强制 Python UTF-8(避免 Windows GBK 解码 bug) | `1` |
| `PYTHONDONTWRITEBYTECODE` | 禁止 .pyc 缓存(vault 干净) | `1` |
| `VLA_HOME` | 项目数据目录(可选,默认仓库根) | `D:\vla-data` |

设置方式:

```powershell
# 永久设用户环境变量
[Environment]::SetEnvironmentVariable('PYTHONUTF8', '1', 'User')

# 验证(设完重开窗口)
[Environment]::GetEnvironmentVariable('PYTHONUTF8', 'User')   # 应输出 1
```

更推荐用项目根目录的 `.env` 文件(uv 自动加载),不要把所有变量塞环境变量,避免泄漏到 shell 历史。

### 2.6 批量写入所有 PATH(应急/自定义安装)

> **什么时候用**:
> - 上面的工具你都是 **手动解压 / 自定义目录** 装的(不走 winget / npm)
> - 装完后发现 `Get-Command pwsh` / `Get-Command uv` 找不到
> - 想一次性把全部 bin 目录写进用户 PATH
>
> **安全保证**:**幂等**(已经在 PATH 里的不重复加)、**容错**(路径不存在只 warn 不中断)、**用户级**(不需管理员)。

```powershell
# === 一键批量加 PATH ===
# 复制粘贴到 PowerShell 7 窗口跑一次(完事重开所有 PS 窗口)

$addPaths = @(
    @{ name = 'uv (Python mgr)';       path = "$env:USERPROFILE\.local\bin" },
    @{ name = 'PowerShell 7';          path = "$env:ProgramFiles\PowerShell\7" },
    @{ name = 'ffmpeg';                path = "$env:ProgramFiles\ffmpeg\bin" },
    @{ name = 'Git for Windows';       path = "$env:ProgramFiles\Git\cmd" },
    @{ name = 'Git for Windows (bin)'; path = "$env:ProgramFiles\Git\bin" },
    @{ name = 'Node.js';               path = "$env:ProgramFiles\nodejs" },
    @{ name = 'npm global bin';        path = "$env:APPDATA\npm" },
    @{ name = 'Obsidian (CLI)';        path = "$env:LOCALAPPDATA\Obsidian" }
)

# 取出当前用户 PATH,分割成数组
$currentPath = [Environment]::GetEnvironmentVariable('PATH', 'User') -split ';' | Where-Object { $_ }

$changes = @()
foreach ($entry in $addPaths) {
    if (-not (Test-Path $entry.path)) {
        Write-Host ("[SKIP]  {0,-22} {1} (路径不存在)" -f $entry.name, $entry.path) -ForegroundColor DarkGray
        continue
    }
    if ($currentPath -contains $entry.path) {
        Write-Host ("[KEEP]  {0,-22} {1} (已在 PATH)" -f $entry.name, $entry.path) -ForegroundColor Gray
        continue
    }
    # 加到最前面(优先级最高,特别是 uv)
    $currentPath = @($entry.path) + $currentPath
    Write-Host ("[ADD]   {0,-22} {1}" -f $entry.name, $entry.path) -ForegroundColor Green
    $changes += $entry.path
}

if ($changes.Count -eq 0) {
    Write-Host "`n所有路径都已就位,无需改动。" -ForegroundColor Cyan
} else {
    # 写回用户 PATH(无需管理员)
    $newPath = $currentPath -join ';'
    [Environment]::SetEnvironmentVariable('PATH', $newPath, 'User')
    Write-Host "`n[OK] 已更新用户 PATH,共 $($changes.Count) 个新路径。" -ForegroundColor Cyan
    Write-Host "[!]  必须重开 PowerShell 窗口才能生效。" -ForegroundColor Yellow
}

# 立即生效(只对当前会话)
$env:PATH = ([Environment]::GetEnvironmentVariable('PATH', 'User')) + ';' + ([Environment]::GetEnvironmentVariable('PATH', 'Machine'))
```

**期望输出示例**:

```
[ADD]   uv (Python mgr)        C:\Users\sirocco\.local\bin
[ADD]   PowerShell 7           C:\Program Files\PowerShell\7
[SKIP]  ffmpeg                 C:\Program Files\ffmpeg\bin (路径不存在)
[KEEP]  Git for Windows        C:\Program Files\Git\cmd (已在 PATH)
[ADD]   npm global bin         C:\Users\sirocco\AppData\Roaming\npm
...

[OK] 已更新用户 PATH,共 4 个新路径。
[!]  必须重开 PowerShell 窗口才能生效。
```

**回滚**(误加了一键清掉):

```powershell
# 查看当前用户 PATH
[Environment]::GetEnvironmentVariable('PATH', 'User') -split ';' | ForEach-Object { Write-Host $_ }

# 备份后清空
$backup = [Environment]::GetEnvironmentVariable('PATH', 'User')
$backup | Out-File "$env:USERPROFILE\path_backup_$(Get-Date -Format 'yyyyMMdd_HHmmss').txt"
[Environment]::SetEnvironmentVariable('PATH', '', 'User')
Write-Host '用户 PATH 已清空(备份在 path_backup_*.txt)' -ForegroundColor Yellow
```

**自定义安装路径**(如果你把工具装在非默认位置):

```powershell
# 假设你装在 D:\tools\
$customPaths = @(
    'D:\tools\uv\bin',
    'D:\tools\PowerShell\7',
    'D:\tools\ffmpeg\bin',
    'D:\tools\nodejs'
)

$currentPath = [Environment]::GetEnvironmentVariable('PATH', 'User') -split ';' | Where-Object { $_ }
foreach ($p in $customPaths) {
    if (Test-Path $p) {
        $currentPath = @($p) + $currentPath
        Write-Host "[ADD] $p" -ForegroundColor Green
    }
}
[Environment]::SetEnvironmentVariable('PATH', ($currentPath -join ';'), 'User')
```

**故障排查 — "什么也没输出"**:

| 现象 | 原因 + 修复 |
|---|---|
| 脚本跑完,**一行输出都没有** | 复制粘贴时中文引号 `""` 替换了英文 `""` → 重贴一次,**用 VSCode / Notepad++ 编辑后再贴**,或保存为 `.ps1` 文件跑 `pwsh -File .\add_paths.ps1` |
| 脚本跑完,只输出"所有路径都已就位" | 所有路径都已在 PATH,无需改动(正常) |
| 脚本跑完,**窗口关闭了**(看不到输出) | PowerShell ISE / VSCode 终端的运行模式 → 用 `pwsh -File` 跑,或加 `Read-Host "按 Enter 退出"` 末尾 |
| 输出乱码 / 方框 | PowerShell 5.1 默认 GBK 编码 → 跑 `chcp 65001` 切 UTF-8,或用 Windows Terminal(Cascadia Code 字体) |
| `[ADD]` 行加完但 `Get-Command` 还找不到 | 旧 PS 窗口还是旧 PATH → **必须重开**所有 PowerShell / WT / VSCode 窗口 |
| 报 `SetEnvironmentVariable: PermissionDenied` | 试图改 Machine PATH(需管理员)→ 脚本用 `'User'` 级别,无需管理员;若仍报错,看是不是 IT 部门策略锁了 |

**简化版诊断脚本**(如果上面的没输出,**用这个**,每步都 echo):

```powershell
# === 简化版 — 一行输出都不会漏 ===

Write-Host '[1] 当前 PowerShell 版本:'
Write-Host ("    PSVersion = {0}" -f $PSVersionTable.PSVersion)
Write-Host ("    Edition   = {0}" -f $PSVersionTable.PSEdition)
Write-Host ''

Write-Host '[2] 当前用户 PATH(逐行):'
$up = [Environment]::GetEnvironmentVariable('PATH', 'User')
if ([string]::IsNullOrEmpty($up)) { Write-Host '    (空)' -ForegroundColor Yellow }
else { $up -split ';' | ForEach-Object { Write-Host ("    {0}" -f $_) } }
Write-Host ''

Write-Host '[3] 检查每个候选路径:'
$candidates = @(
    "$env:USERPROFILE\.local\bin",
    "$env:ProgramFiles\PowerShell\7",
    "$env:ProgramFiles\ffmpeg\bin",
    "$env:ProgramFiles\Git\cmd",
    "$env:ProgramFiles\nodejs",
    "$env:APPDATA\npm"
)
foreach ($p in $candidates) {
    $exists = Test-Path $p
    $inPath = $up -split ';' | Where-Object { $_ -eq $p }
    $marker = if (-not $exists) { '[NOFILE]' }
               elseif ($inPath)   { '[INPATH]' }
               else               { '[MISSIN]' }
    $color = if (-not $exists) { 'DarkGray' }
              elseif ($inPath) { 'Gray' }
              else             { 'Red' }
    Write-Host ("    {0} {1}" -f $marker, $p) -ForegroundColor $color
}
Write-Host ''
Write-Host '[4] 提示:看到 [MISSIN] 行的,就是需要手动加 PATH 的。'
Write-Host '   复制 2.6 节主脚本跑,会自动处理这些路径。'
```

**期望输出**(用户友好版):

```
[1] 当前 PowerShell 版本:
    PSVersion = 7.4.6
    Edition   = Core

[2] 当前用户 PATH(逐行):
    C:\Users\sirocco\.local\bin
    C:\Users\sirocco\AppData\Roaming\npm
    (其他若干行...)

[3] 检查每个候选路径:
    [INPATH] C:\Users\sirocco\.local\bin
    [NOFILE] C:\Program Files\PowerShell\7
    [MISSIN] C:\Program Files\ffmpeg\bin      ← 需要加
    [INPATH] C:\Program Files\Git\cmd
    [MISSIN] C:\Program Files\nodejs           ← 需要加
    [INPATH] C:\Users\sirocco\AppData\Roaming\npm

[4] 提示:看到 [MISSIN] 行的,就是需要手动加 PATH 的。
   复制 2.6 节主脚本跑,会自动处理这些路径。
```

**保存为 `.ps1` 文件跑**(最稳,避免复制粘贴编码问题):

```powershell
# 在 PowerShell 窗口:
notepad "$env:USERPROFILE\add_paths.ps1"
# 把 2.6 节主脚本粘进去,保存,关闭

# 跑(任何 PS 窗口都行)
pwsh -File "$env:USERPROFILE\add_paths.ps1"
# 或
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\add_paths.ps1"
```

---

## 三、开发者工具:Claude Code 接入(可选)

> **范围**:Claude Code 是 Anthropic 的命令行 AI 编码工具,在 Windows 上跑本项目开发时强烈建议接入。**这不是 vla 运行必需**,只影响开发体验。

#### 3.1 安装前置

```powershell
# 1. Node.js 20 LTS+(Claude Code 官方推荐 Node 18+,Node 20 LTS 最稳)
winget install OpenJS.NodeJS.LTS
# 验证
node --version    # v20.x.x 或 v22.x.x
npm --version     # 10.x.x

# 2. Git for Windows(克隆项目用,见 5.6)
winget install Git.Git

# 3. VS Code(可选,Claude Code 自带 CLI 不需要编辑器,但 VS Code 体验更佳)
winget install Microsoft.VisualStudioCode
```

#### 3.2 装 Claude Code CLI

```powershell
# 官方推荐方式:npm 全局装
npm install -g @anthropic-ai/claude-code

# 验证
claude --version
# 应输出 claude-code 1.x.x

# 首次启动需要 Anthropic 账号(Pro/Max/Team)或 Console API key
claude
# 按提示:
#   1. 浏览器打开 https://claude.ai/login
#   2. 登录账号 → 复制 OAuth code 回来粘贴
#   或:设置环境变量 ANTHROPIC_API_KEY 走 API 计费
```

#### 3.3 配 Claude Code + 本项目

```powershell
# 在项目根目录跑(Claude Code 会自动读 CLAUDE.md / .claude/)
cd video-learning-agent
claude

# 关键文件(项目已含):
#   .claude/CLAUDE.md          — 项目规范(SSOT = requirements.md)
#   .claude/skills/            — 自定义 skill(vla-learn-bill-jc)
#   requirements.md            — 需求 SSOT(改需求先改这里)
```

#### 3.4 常用命令

| 命令 | 作用 |
|---|---|
| `claude` | 进入交互模式(默认读 CLAUDE.md) |
| `claude "实现 Phase 4"` | 单次任务(自动选 skill) |
| `claude -c` | 续接上一次会话 |
| `claude --model claude-sonnet-4.5` | 切模型 |
| `claude /loop 5m "vla doctor"` | 每 5 分钟跑一次 vla doctor |
| `/help` | 内置命令(`/clear` `/compact` `/memory` 等) |

#### 3.5 故障排查

| 现象 | 原因 + 修复 |
|---|---|
| `'claude' is not recognized` | npm 全局 bin 目录没在 PATH:`npm config get prefix` → 把返回路径下的 `bin` 加 PATH(如 `C:\Users\<u>\AppData\Roaming\npm`) |
| `npm install` 报 `EACCES` | 路径权限 → 重开 PowerShell 管理员模式,或换 prefix:`npm config set prefix "$env:USERPROFILE\.npm-global"` |
| `claude` 跑起来后看不到 `.claude/` | 当前目录不对 → `cd` 到项目根(有 `.claude/CLAUDE.md` 那个目录) |
| 报 `ANTHROPIC_API_KEY not set` | Pro/Max 用户走 OAuth 自动登录;API 用户:`[Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY","sk-ant-...","User")` |
| 中文路径乱码 | PowerShell 终端字体 → Windows Terminal 改 `fontFace: "Cascadia Code"` 或 `"Microsoft YaHei Mono"` |
| PowerShell 执行策略拦截 | `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned` |

**macOS doctor 还多验证**:`screencapture` 是否被 TCC 授权(若失败 → 引导去"系统设置 → 隐私与安全性 → 屏幕录制")。

**禁忌**:
- ❌ Microsoft Store 装 Python(路径含空格,uv 装不了 + pip 装包会失败)
- ❌ python.org .exe 安装包(勾 Add to PATH 后会污染全局 PATH,uv sync 会冲突)
- ❌ 系统自带 Python 3.x(版本老 + 没有 pip)

