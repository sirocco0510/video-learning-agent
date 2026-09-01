# video-learning-agent 项目规范

本文件是项目级指令,**优先级**:
1. 用户直接指令(本对话里的话)
2. 本文件(项目级)
3. 父级 `sirocco的知识库/CLAUDE.md`(Obsidian Vault 笔记规范,只约束 Vault 笔记处理)
4. superpowers skill 默认行为

继承父 Vault 规则:不擅自改笔记结构 / 不批量重命名 / 改需求先改 SSOT。

---

## 项目铁三角(SSOT 锚定)

| 文档 | 角色 | 修改时机 |
|---|---|---|
| `README.md` | 项目门户 + 快速上手 | 入口介绍变更 |
| `requirements.md` | 需求 SSOT(FR-1 ~ FR-10) | **任何需求变更先改这里** |
| `implementation-plan.md` | 9 个 Phase 实施计划 + 验收代码 | 需求变更后同步;每 Phase 完成后打勾 |

**改流程**:改 `requirements.md` → 同步 `implementation-plan.md` → 改代码 → 跑该 Phase 验收代码。

---

## Python 工程约定

- **运行时**:Python 3.12,uv 管理,src layout(`src/vla/`)
- **包管理**:`uv add / uv remove / uv sync / uv lock`,lockfile 已提交
- **数据模型**:pydantic v2(`models.py`),所有 FR 接口都用 pydantic
- **类型注解**:函数签名必填,函数体内可省;`mypy --strict` 不强制但默认走
- **CLI**:typer(已在 `cli.py`),rich 输出
- **异步**:pytest-asyncio,`asyncio_mode = "auto"`
- **Lint/format**:建议 ruff(`uv add --dev ruff`),行宽 100

### 模块布局

```text
src/vla/
├── cli.py             # typer 入口(vla doctor / process / batch)
├── config.py          # 配置加载(.env + config/vla.yaml)
├── models.py          # pydantic 数据模型
├── main.py            # 主调度(Phase 8)
├── llm/               # 统一 LLM 客户端(Phase 1)
├── source/            # 视频源工厂(yt-dlp / ffmpeg 录屏,Phase 2)
├── subtitle/          # 三级字幕策略(Phase 3)
├── transcribe/        # faster-whisper 流式转写(Phase 4)
├── quality/           # 质量门控(启发式 + LLM,Phase 5)
├── summary/           # LLM 批量总结(Phase 7)
├── ui/                # macOS 通知 / 弹窗(Phase 6)
├── log/               # 转写日志 + FailureAlert(Phase 6 / 7.6)
└── state/             # quota / history / plugin_status(Phase 7.5)
```

### 测试约定

- 位置:`tests/`,文件名 `test_*.py`
- fixture 放 `tests/fixtures/`,需要时建子目录
- 单元测试:每个 Phase 至少 1 个 happy path + 1 个失败 path
- 验收:`implementation-plan.md` 每个 Phase 末尾的"验收代码"块 = 唯一真相

---

## 开发流程(强制)

1. **TDD**:每个模块先写 test,再写实现,跑测试 → 跑验收代码
2. **环境检查**:`uv run vla doctor` 全 OK 才能开始 Phase 1+
3. **完成 Phase**:在 `implementation-plan.md` 进度跟踪处打勾(单选 `[x]`)
4. **失败处理**:任何 bug 先走 `superpowers:systematic-debugging`,不直接试错
5. **写代码前**:`superpowers:brainstorming`(除非用户明确说"直接干")
6. **commit 前**:`superpowers:verification-before-completion`(跑 doctor + 受影响测试)

---

## 安全红线(必读)

| 红线 | 含义 |
|---|---|
| **字幕永远本地** | 只用 faster-whisper / B站官方 CC / VideoTrans;**禁止引入云端转写** |
| **云端 LLM 限定两件事** | ① 字幕质量检查(读 `pass` + `score`)② 6h 批量总结(500–800 字) |
| **磁盘友好** | 256 GB 机器,峰值 < 1 GB;转写完才能删源文件,质量过了才能删 |
| **macOS 权限** | 屏幕录制(录屏路径)、通知(B 级)、辅助功能(A 级弹窗)首次需用户授权 |

---

## 关键 FR 语义(常见误解预防)

- **字幕降级**(FR-2.5/2.6/2.8):① 失败 / ② 弹窗超时或跳过 → 降级到 ③,**不是**记 `transcribe_fail`,**不是**跳过当前视频
- **插件一次启动**(FR-2.9/2.10):整 session 只弹一次,`PluginStatus` 单例,`unavailable` 后不再弹窗
- **累计 6h 触发总结**(FR-9):默认 `summary_threshold_sec: 21600`,触发后 session 停止
- **去重**(FR-9.6):成功 URL 写 `logs/transcribed_history.jsonl`,下次自动跳过
- **失败日志上限弹窗**(FR-6.6):跨 `log_alert_threshold` 倍数才弹窗,避免打扰
- **通知分级**(FR-6):A 阻塞弹窗 / B 非阻塞通知 / C 静默只写 CSV

具体规格看 `requirements.md` 对应 FR 段。

---

## macOS 首次运行授权

| 权限 | 触发场景 | 设置路径 |
|---|---|---|
| 屏幕录制 | ffmpeg 录屏(yt-dlp 失败时) | 系统设置 → 隐私与安全性 → 屏幕录制 |
| 通知 | `display notification`(B 级) | 系统设置 → 通知 → 允许终端 / osascript |
| 辅助功能 | AppleScript 弹窗(A 级) | 系统设置 → 辅助功能 |
