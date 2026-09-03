# Refactor Consolidation(2026-09-03)

## 背景

`video-learning-agent` 经过 Phase 1-9 多轮迭代,代码里出现以下问题:

1. **真 bug**:`models.py` 三个核心类被定义两次(Python 后定义赢,前几行是死代码)
2. **死代码**:`FailureAlert` 实现 + 测试齐全但从未接入;`source/_record_screen` 与 FR-8 矛盾但仍挂在 `get()` 里
3. **重复抽象**:同一类 LLM JSON 解析器两份实现;`LLMClientLike` Protocol 在 3-4 个模块各自重定义;`save_transcribed` 写格式与 `_parse_file` 解析格式只有 regex 耦合

用户决策(2026-09-03):
- 整体重构(全 9 个 Phase 范围)
- 两次 LLM 调用(refine + quality check)保留分离,但共用 parser + prompt 模板
- 完全删除 `FailureAlert`(连同测试 + implementation-plan.md 章节)
- 完全删除 `_record_screen` + ffmpeg fallback(网络失败 = 报错退出)

## 范围

### 包含

- §3 列出的 11 个 Phase 工作(R.1 ~ R.11)
- 删除 `FailureAlert` 相关一切:`log/failure_alert.py` / `tests/test_e2e.py::TestFailureAlertE2E` / `implementation-plan.md:1717-1744` 章节
- 删除 `_record_screen` + `config.video_source.record.*` 配置
- 改名 `alert_blocking` → `alert`(若 R.7 取消则无需做,顺带删除)

### 不包含

- `requirements.md` 内容(冻结,不改)
- `implementation-plan.md` 内容(冻结,不改;Phase 进度块保持 `all [x]`,包括失效的 FailureAlert 章节 — 历史快照)
- web entry spec(`docs/superpowers/specs/2026-09-02-web-entry-and-queue-design.md`,用户已声明之后提供数据)
- 任何 FR 行为变更(对外可观测的下载、转写、评分、汇总、通知、历史、URL key 全部保持)

## 原则

1. **不动业务行为** — FR-1 ~ FR-10 可观测行为不变
2. **共用抽象,不一锅端** — refine + quality check 是两次调用,但共用 parser + prompt 工具;summarizer 仍独立
3. **删死代码优先** — 14 处全部处理
4. **TDD** — 每个 R.Phase 先写 test,再写实现,跑测试
6. **SSOT 重新分层**(2026-09-03 用户决策):
   - 产品需求 SSOT:`requirements.md`
   - 设计 SSOT:`docs/superpowers/specs/2026-09-03-refactor-consolidation.md`(本文件)
   - 执行方案:superpowers writing-plans skill 输出
   - 历史:`implementation-plan.md`(冻结)

## 红线(保持)

- 字幕永远本地 — 不引入云端转写
- 云端 LLM 限定两件事 — ① subtitle quality 相关(refine + check)② 6h batch summary
- 磁盘友好 — 256 GB 机器,峰值 < 1 GB
- macOS 权限 — 屏幕录制 / 通知 / 辅助功能(R.8 后屏幕录制不再需要)

## 现状映射(2026-09-03 agent 报告摘要)

| 类别 | Phase | 状态 | 备注 |
|---|---|---|---|
| ✅ | 0/1/3/4/5/7/7.5/8 | 完成 | |
| 🚧 partial | 2 | 部分 | `_record_screen` 与 FR-8 矛盾 |
| 🚧 partial | 6 | 部分 | `FailureAlert` 死代码(本 spec 完全删除) |
| 🚧 partial | 9 | 部分 | e2e test 还在跑通但脆 |

`implementation-plan.md:1919-1959` 全部 `[x]` 是历史快照,本 spec 后不再更新。

## 14 处冗余处理清单

### A. 真 bug(必须修)

| # | 位置 | 处理 |
|---|---|---|
| 1 | `models.py` 全文 | 删第一份 `VideoTask` / `SubtitleResult` / `QualityResult` 定义,留第二份 |
| 2 | `quality/checker.py:30` + `quality/refiner.py:38` + `summary/llm_summarizer.py:33` | `LLMClientLike` 统一定义在 `llm/client.py`;其他 import |
| 3 | `log/failure_alert.py` + `tests/test_e2e.py::TestFailureAlertE2E` | **完全删除**(用户决策);`implementation-plan.md` 不动 — 历史快照失效章节自然保留 |
| 4 | (合并到 #3) | `alert_blocking` 协议与 FailureAlert 一起删除,`MacOSNotifier.alert()` 不变 |
| 5 | `source/video_source.py:78` `_record_screen` + `config.video_source.record.*` | **完全删除**;`get()` 失败抛 `DownloadError`,上层不 fallback(用户决策) |

### B. 共用抽象(设计核心)

| # | 位置 | 处理 |
|---|---|---|
| 6 | `quality/checker.py:187` `_parse_json` vs `quality/refiner.py:247` `_parse_json` + `_try_parse_balanced_object` | 抽到 `llm/response.py` 单实现 `parse_json_response(text, *, strip_think=True, try_code_blocks=True)`,brace-counting 统一策略 |
| 7 | `quality/checker.py` PROMPT + `quality/refiner.py` `_SYSTEM_PROMPT` / `_USER_PROMPT_TEMPLATE` | 抽 `llm/prompts.py`:`build_chat_prompt(system, user)` + `enforce_json_response(system, extra="只输出 JSON")` |
| 8 | `cli.py:229` + `bilibili_official.py:33` bvid 提取 | 抽 `utils/bvid.py`:`extract_bvid(url) -> str | None` + `make_url_key(group_id, bvid, p=None) -> str` |
| 9 | `log/transcription_log.py:116` 写 + `summary/llm_summarizer.py:114` `_parse_file` 解析 | 抽 `log/transcribed_file.py`:`write(path, item: TranscribedItem)` + `read(path) -> TranscribedItem` |
| 10 | `subtitle/browser_record.py` 的 `_pause_page_video` 兼容包装 | 删除,3 处调用直接用 `page_control.pause_page_video` |

### C. 配置整合

| # | 位置 | 处理 |
|---|---|---|
| 11 | `cfg.quality_check.model` + `cfg.summary.model` + `cfg.quality_check.refine_model` | 抽 `LLMConfig` 顶层子配置 `cfg.llm.{refine,quality,summary}_model`,`cfg.llm_client` 保留(provider + env 解析);`from_yaml` 自动迁移 |
| 12 | `cfg.video_source.record.{screen_index,fps,crf,audio_input,preset}` | 删除(R.8 删 ffmpeg 后无意义) |

### D. 不做(YAGNI)

| # | 位置 | 决定 |
|---|---|---|
| 13 | `state/{quota,history,plugin_status}` | 不动 |
| 14 | web entry spec | 不动 |

## 模块布局(目标态)

```text
src/vla/
├── cli.py                    # typer 入口(改:用 utils.bvid.extract_bvid)
├── main.py                   # VideoLearningAgent.run()(R.7 删 FailureAlert 后变更最小)
├── main_provider.py          # 修 #5:get() 失败不 fallback,直接抛
├── config.py                 # 抽 LLMConfig (#11),video_source 删 record.* (#12)
├── models.py                 # **修 #1**:删重复定义
├── llm/
│   ├── client.py             # LLMClient + 统一定义的 LLMClientLike Protocol (#2)
│   ├── response.py           # **新增** parse_json_response (#6)
│   └── prompts.py            # **新增** build_chat_prompt + enforce_json_response (#7)
├── quality/
│   ├── checker.py            # 用 llm.response + llm.prompts
│   └── refiner.py            # 删内部 _parse_json + _try_parse_balanced_object
├── summary/
│   └── llm_summarizer.py     # 用 llm.prompts;删内部 LLMClientLike
├── log/
│   ├── transcription_log.py  # save_transcribed → transcribed_file.write
│   └── transcribed_file.py   # **新增** write/read (#9)
├── source/
│   └── video_source.py       # 删 _record_screen,get() 失败抛 DownloadError
├── subtitle/                 # 删 _pause_page_video 兼容包装
├── transcribe/               # 不变
├── ui/                       # 不变
├── state/                    # 不变
└── utils/                    # **新增**
    └── bvid.py               # extract_bvid + make_url_key (#8)
```

## 执行 Phase(R.1 ~ R.11)

| Phase | 内容 | 验收 |
|---|---|---|
| **R.1** | `llm/response.py` 新建;refiner 现有 brace-counting 迁过来;checker / refiner 改用 | `tests/test_response_parser.py` 40 case(原 refiner test 全迁);现有 41 个 refiner test 仍 pass |
| **R.2** | `llm/client.py` 顶层定义 `LLMClientLike` Protocol;删 checker / refiner / summarizer 的本地副本 | `grep "class LLMClientLike"` 返回 1 次 |
| **R.3** | `llm/prompts.py` 新建 `build_chat_prompt` + `enforce_json_response`;checker / refiner / summarizer 改用 | `tests/test_prompts.py` |
| **R.4** | `models.py` 删重复类定义;字段对照保留 | 现有所有 test 仍 pass |
| **R.5** | `utils/bvid.py` 新建;`cli.py` / `bilibili_official.py` 改用 | `tests/test_bvid.py` |
| **R.6** | `log/transcribed_file.py` 新建 `write/read`;`save_transcribed` + `LLMSummarizer._parse_file` 改用 | `tests/test_transcribed_file.py` + summarizer 现有 test |
| **R.7** | **完全删除** `log/failure_alert.py` + `tests/test_e2e.py::TestFailureAlertE2E`(`implementation-plan.md` 章节保留为历史快照) | `grep "FailureAlert" --include="*.py"` 返回 0 |
| **R.8** | 删 `_record_screen` + `video_source.record.*` 配置 + `cfg.video_source.record.enabled = true`;`get()` 失败抛 `DownloadError`,**不 fallback**;`main_provider.py` 不再 import `screen` 路径 | `tests/test_video_source.py` 验证失败抛错;pipeline 网络失败时直接报错 |
| **R.9** | 删 `subtitle/browser_record.py` 的 `_pause_page_video` 兼容包装,3 处调用直接用 | `grep "_pause_page_video"` 返回 0 |
| **R.10** | `config.py` 新 `LLMConfig`,迁移 3 个 model 字段;`from_yaml` 兼容旧 yaml(自动归并到 `cfg.llm.*`) | `tests/test_config.py` 更新 |
| **R.11** | 全量回归:test 全 pass + `vla doctor` + 一次真实 B站 跑通 | 35+ tests pass;pipeline end-to-end |

## 风险

| 风险 | 缓解 |
|---|---|
| `models.py` 删重复后字段不一致(原代码第一份 vs 第二份有差异) | R.4 跑现有 test;如有差异看 test 期望定 |
| `transcribed_file.py` 解析格式与 summarizer 旧假设有偏 | R.6 保留原 regex 行为,只换封装 |
| 删 `_record_screen` 后有人依赖 ffmpeg 兜底 | R.11 实跑 B站 验证;失败报错符合预期 |
| `LLMConfig` 迁移后旧 yaml 配置不生效 | R.10 加 yaml 兼容(检测旧字段自动映射到 `cfg.llm.*`) |
| web entry spec 之后想用 `_record_screen` 兜底 | R.8 后 web spec 也得改 — 与 web 实现的协调是后续 spec 范围 |

## 不在本 spec 范围

- 任何 FR 行为变更
- requirements.md 内容
- implementation-plan.md 内容(冻结)
- web entry spec 实现
- macOS 权限相关改动
- 任何 .obsidian / CLAUDE.md 内容变更
- 用户实际运行 B站视频的验证(R.11 后做,不在 spec 内)

## 后续

- 用户审 spec,通过后用 superpowers writing-plans skill 拆 R.1 ~ R.11 的执行 plan
- 每个 R.Phase 由独立 plan 执行(测试先行,验收由该 plan 的"验收代码"块定义)
- R.11 完成后,新功能/新需求走 brainstorming → spec → writing-plans 流程,不再回 implementation-plan.md