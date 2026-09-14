---
name: vla-learn-bill-jc
description: Use when the user wants to transcribe video from b-learning.bill-jc.com (公司内部学习平台) — single video OR a whole course catalog. Guides URL paste, validates the URL is from bill-jc, then either runs scripts/spike_bill_jc_full.py end-to-end (single video) or `vla learn` (course catalog, paged batch). Triggers on "学 bill-jc 视频", "跑 bill-jc 视频", "bill-jc 转写", "批量跑课程", "跑 spike", or user invoking /vla-learn-bill-jc.
---

# vla-learn-bill-jc

引导用户粘贴 **b-learning.bill-jc.com**(公司内部学习平台)链接 → 校验 → 落盘到 `logs/transcribed/<date>/transcripts/`(+ 长视频 `summaries/`)。

**两种模式**(2026-09-10 起):

| 模式 | 用户粘什么 | 走什么 |
|---|---|---|
| **单视频**(默认) | 视频**详情页** URL(含 `kngId`) | `scripts/spike_bill_jc_full.py`(Step 3–6) |
| **整课批量** | 课程**目录页** URL(含 `catalogId` + `cid`) | `uv run vla learn`(见下方「批量模式」) |
| **watch**(2026-09-14 起) | 课程**目录页** URL(含 `catalogId` + `cid`) | `uv run vla watch`(见下方「watch 模式」) |

三种都只支持 bill-jc 内部站。其他平台(B站 / YouTube / b23.tv 短链)请用 `vla process` 主命令,**不在本 skill 兜底**。

---

## 进入 skill 前的硬性前置

两种模式**共同**的前置:

- ✅ `uv run vla doctor` 全 OK(失败项先补,见 `.claude/CLAUDE.md`)
- ✅ Chrome debug 9222 已启,且 Chrome 里已登录 b-learning.bill-jc.com
  - macOS 启法:`/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-debug-vla`
  - Windows 启法见 [docs/WINDOWS.md §3](../../docs/WINDOWS.md)
- ✅ 工作区 clean 或用户明确说"可以混着改"

**模式相关**的额外前置(Step 2 分流后才确定哪种,进 skill 时先不用查):

| 模式 | 额外前置 |
|---|---|
| 单视频(`kngId`) | `scripts/spike_bill_jc_full.py` 存在 —— 它是单视频路径的**唯一执行者**,缺它直接停 |
| 整课批量(`catalogId` + `cid`) | **无额外前置** |

> **批量不依赖 spike 脚本。** `vla learn` 是 `src/vla/cli.py:501` 的 typer 命令 +
> `src/vla/learn.py`,全链**不 import 也不调用** `scripts/spike_bill_jc_full.py`。
> 两条路径共享的只是**库模块** `vla.subtitle.internal_site_spider.InternalSiteSpider`
> (spike 脚本 L38 也是 import 它)—— 删掉 spike 脚本,`vla learn` 照跑。
> 所以「spike 脚本存在」这条检查只对单视频模式有意义。

任一不满足 → **停下,先解决**,不进入 skill。

---

## 流程

### 1. 引导用户粘贴 URL

向用户说:

```text
请粘贴 bill-jc 链接 —— 两种都可以:

① 单个视频(详情页):
     https://b-learning.bill-jc.com/kng/#/video/play?kngId=<kng_id>
     https://b-learning.bill-jc.com/?kngId=<kng_id>

② 整门课(课程目录页)—— 会按目录翻页批量跑:
     https://b-learning.bill-jc.com/kng/#/list?catalogId=<X>&cid=<Y>&order=0&sort=0&type=
```

(其它域 / path / 短链 → 第 2 步会拒绝)

### 2. URL 校验(白名单)

**必须先判断是哪一种**,再分别校验。

#### 2a. 单视频(含 `kngId`)

> **`kngId` 永远是查询参数,不在 path 里。** `/learn/<kng_id>` 这种 path 形式
> **不存在**(它是 2026-09-09 设计文档里的凭空假设;2026-09-10 用户裁定真机没有这种
> URL)。代码侧 `internal_site_adapter` 也据此改成解析查询参数。

| 形式 | regex |
|---|---|
| **SPA 视频详情页**(实际唯一形式) | `^https?://b-learning\.bill-jc\.com/kng/#/video/play\?kngId=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:&.*)?$` |
| SPA query(根 + kngId) | `^https?://b-learning\.bill-jc\.com/\?kngId=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$` |

通过 → 提取 `kng_id`(UUID),继续 Step 3(单视频路径)。

#### 2b. 课程目录页(含 `catalogId` + `cid`)—— 批量模式

前缀必须匹配:

```text
^https?://b-learning\.bill-jc\.com/kng/#/list\?
```

然后从 query 里**分别**抽出两个参数(**顺序任意**,不要写成一个位置固定的 regex):

| 参数 | 含义 | 传给 `vla learn` |
|---|---|---|
| `catalogId` | 目录 ID | `--catalog-id` |
| `cid` | **就是 collegeId**(不是"课程 ID") | `--college-id` |

两个都必须是 UUID 格式。任一缺失或非 UUID:

```text
❌ 目录页 URL 缺 <catalogId / cid>,请从课程目录页地址栏完整复制(两个参数都要带)。
```

通过 → 跳下方「批量模式」。**不要**往下走 Step 3。

#### 2c. 拒绝

- ❌ `www.bilibili.com/video/BV1xxx` → "本 skill 仅支持 bill-jc 内部站;B 站请用 `uv run vla process --url <url>`"
- ❌ `youtube.com/watch?v=...` → 同上
- ❌ `b23.tv/xxx` → 同上
- ❌ `b-learning.bill-jc.com`(既无 kng_id 也无 catalogId) → "URL 不含 kng_id 或 catalogId,请进入具体视频详情页 / 课程目录页后复制 URL"

#### 2d. 批量模式分流:learn vs watch(2026-09-14 加)

Step 2b 通过后,**默认走 `learn`**(完整转写链路)。但有些场景用户**明确**
要的是 watch(开 tab + 不录 + 不调 process_asset)。需要靠**用户原话关键词**
识别。**两者 URL 形式完全相同**,必须看用户的意图,不能看 URL。

**走 watch 的关键词**(任一命中即走 watch):

| 类别 | 关键词 / 短语 |
|---|---|
| **显式命令** | "跑 watch"、"用 watch"、"watch 模式"、"用 vla watch" |
| **不要录屏** | "不录"、"不录音"、"不抓音"、"关闭录屏"、"不开录屏"、"skip 录屏" |
| **不要转写** | "不转写"、"不调 process_asset"、"不产字幕"、"先不跑转写" |
| **预览 / 检查** | "先看一下"、"先看看"、"预览"、"先列一下"、"列个清单"、"标个清单"、"扫一遍" |
| **调试 nav** | "测 browser nav"、"测 browser 路径"、"调试 browser"、"只跑 nav"、"只看 nav 是否通" |
| **capture 质量未达标** | "capture 有问题"、"先不开"、"capture 质量没解决"、"暂时关掉录屏" |
| **预期产物提示** | "先看看哪些待转写"、"只标'待转写'"、"不要 transcripts/"、"不要 summaries/" |

**走 learn 的关键词**(任一命中即走 learn,**优先级最高**):

| 类别 | 关键词 / 短语 |
|---|---|
| **显式命令** | "跑 learn"、"用 learn"、"learn 模式"、"用 vla learn"、"全跑"、"跑完整" |
| **明确要产物** | "产出字幕"、"写 transcripts/"、"写 summaries/"、"我要字幕"、"转出来" |
| **跑端到端** | "端到端跑"、"端到端"、"e2e"、"完整跑" |
| **结合历史** | "接着上次跑"、"把没转写的补上"、"补齐剩下的"、"跳过已转写" |

**两难判定**(都没命中):

```text
⚠️ 课程目录页既能走 learn(完整转写)也能走 watch(只开 tab 不录)。
   您 想要哪个?
   - learn → 全跑,会产字幕+摘要(默认)
   - watch → 只开浏览器 tab 跑 nav,确认能播放,不录、不转写
```

把选项抛回去,**不要猜**。

**已知陷阱**(2026-09-14 实际跑出来的):

| 用户原话 | 期望意图 | skill 不要走错 |
|---|---|---|
| "先开 tab 看下" | watch(开 tab,看是否能跑通) | ❌ 不要走 learn(会转写整个目录) |
| "看一下课程有几条" | watch(列清单) | ❌ 不要走 learn(用户没要转写) |
| "跑完整转写" | learn | ❌ 不要走 watch(用户明确要转写) |
| "先用 watch 看一下能不能跑" | watch(试探 nav) | ❌ 不要走 learn(试探不转写) |

> ⚠️ **不要用 URL 形式区分**:`learn` 和 `watch` 都吃 `catalogId` + `cid`。
> 区分**唯一依据**是用户的意图关键词。

### 3. (单视频路径)默认走 `--parse-only`(2026-09-10 设计:让用户先看参数,再决定跑不跑)

> Step 3–6 只适用**单视频**(Step 2a 通过)。批量走 Step 2b → 「批量模式」。

**skill 默认不跑转写**,只跑 `spider.fetch_metadata(kng_id)`,返回 JSON 含**播放就绪信息**:

- `kng_id` / `m3u8_url` / `resolution` / `fileId` — 必返
- `all_resolutions` — 服务端提供的全部档位(如 `["1080p","720p","480p","360p"]`)
- `subtitles_flag` — `0` = 服务端无官方字幕(走抽音 + Whisper),`1` = 有官方字幕
- `title` / `duration_sec` / `college_id` — **固定 None**(2026-09-10 真账号探勘结论:
  yunxuetang kngPlay API 顶层响应**不含**业务元数据,只返播放配置)

**业务元数据从哪里来**:
- `title` / `college_id` — 用户从课程目录页 URL 上下文拿,或在端到端 spike 跑起来后
  通过 `list_tasks` 间接拿到(端到端路径会自动覆盖)
- `duration_sec` — 端到端路径下,转写前 `ffprobe` 抽音后真实测量

**这一步 `kng_id` 是必需的,但 `college_id` 不是** —— `--parse-only` 不依赖 college_id,
直接 kngPlay API 拿播放信息。

```bash
uv run python scripts/spike_bill_jc_full.py \
  --parse-only \
  --kng-id <kng_id> \
  --cdp-url http://localhost:9222
```

### 4. 报告参数 + 询问是否继续

把 parse 拿到的 JSON 打印给用户,然后:

```text
✅ 已解析该视频(播放就绪):
   kng_id:       <kng_id>
   m3u8:         <m3u8_url>
   resolution:   <resolution>(备选:<all_resolutions>)
   fileId:       <fileId>
   subtitles:    <0 = 无官方字幕(走抽音+Whisper) / 1 = 有官方字幕>

注意:title / duration / college_id **不在 kngPlay 响应里**(2026-09-10 真账号探勘确认)。
  - 若您已知 college_id(同一课程下的多视频),直接给我,我跑端到端。
  - 若您只想要"确认 m3u8 能拿到" → 告诉 AI "到此为止" 即可。

接下来:
- 您已知 college_id → 跑端到端(Step 5)
- 您只想要参数   → 告诉 AI "到此为止"
- 您想看其他分辨率 → 改 --resolution 360p / 480p / 720p / 1080p 重跑 parse-only
```

---

### 5. 跑 spike(端到端模式)

```bash
uv run python scripts/spike_bill_jc_full.py \
  --college-id <college_id> \
  --kng-id <kng_id> \
  --cdp-url http://localhost:9222
```

- **不要**加 `--config` / `--resolution` / `--verbose`(用脚本默认值)
- **不要**加 `--list-only`(skill 的目的是真跑)
- mac 默认 CDP URL 是 `http://localhost:9222`,Windows 也一样,不用问

### 6. 报告结果

**成功**(spike exit 0):

```text
✅ bill-jc 转写完成
   字幕: logs/transcribed/<YYYY-MM-DD>/transcripts/<id>_<safe_title>.txt
   摘要: logs/transcribed/<YYYY-MM-DD>/summaries/<id>_<safe_title>.summary.txt  (如质量超阈值触发)
   质量: <score>/100
   时长: <sec>s
```

**失败**(spike exit ≠ 0):不重试,贴 spike 输出最后 20 行,**让用户决定**(CDP 没启 / m3u8 解密失败 / whisper OOM / 质量 fail 各有不同处理)。

---

## 批量模式(课程目录页,2026-09-10 新增)

**触发**:Step 2b 通过 —— 用户粘的是课程目录页 URL(含 `catalogId` + `cid`)。

### B1. 先 `--dry-run` 列清单,再决定跑不跑

```bash
uv run vla learn \
  --college-id "<cid>" \
  --catalog-id "<catalogId>" \
  --limit 10 \
  --dry-run
```

`--dry-run` **不装配** transcriber / refiner / LLM(零凭据零副作用),只是翻页把
目录下的视频列出来(编号 + kng_id + 标题),并标注哪些**已转写**(⏭️)。

它会打印:

```text
📋 dry-run:目录共 5250 条 / 已转写 0 条 → 本次将处理 5250 条
```

把条数报给用户:

```text
📋 该目录共 <total> 条,已转写 <done> 条 → 本次将处理 <total-done> 条。
   --limit 10 表示每页 10 条 + 翻页步长;累计 6h 或目录翻完即停。
   接下来:① 全跑 ② 只跑前几条(把 --limit 调小)
```

> ⚠️ **去重依赖 `logs/transcribed_history.jsonl`,而且它只记成功。**
>
> 1. 该文件由 `agent.run` 在每条**成功**后写入(`HistoryStore.record_success()`,
>    `state/history.py:83`——**没有** `record_failure`)。
> 2. **`scripts/spike_bill_jc_full.py` 从不写它**(spike 直接调 fetch/process,
>    不经过 agent)。所以**此前用 spike 跑过的视频不会被 batch 认成"已转写",
>    会重跑一遍**。若用户在意,先确认 history 文件内容再解释差异。
> 3. **失败条目也不写它** ⇒ 下次跑**会再跑一遍**(见 B4)。
>    `dry-run` 报的"已转写 <done> 条"**只统计成功**,不要把它等同于"这个目录已经处理干净了"。

### B2. 真跑

```bash
uv run vla learn \
  --college-id "<cid>" \
  --catalog-id "<catalogId>" \
  --limit 10
```

- **不要**加 `--config` / `--cdp-url` / `--resolution`(用默认值)
- mac 默认 CDP URL 是 `http://localhost:9222`,Windows 也一样,不用问
- 已转写过的视频按 `logs/transcribed_history.jsonl` **自动跳过**(不是失败,是跳过)
- **全程不弹窗**(FR-11.12):内部站走 m3u8 直抽,不碰浏览器插件路径。**若看到
  "是否已开启字幕插件"的弹窗,说明 kngId 解析 miss 了** —— 停下查 URL 形式
  (见 Step 2a),不要点"跳过"硬跑(会连带记 `transcribe_fail`)

### B3. 停法(报给用户时要说清是哪一种)

| 停法 | 日志/输出 | 含义 |
|---|---|---|
| 目录翻完 | `🏁 翻页结束:offset=N 无更多视频` | 全跑完 |
| 配额到 | `🛑 累计配额已到(X.Xh)…→ 停止翻页` | 累计 6h(`summary_threshold_sec`),session 结束。**已跑完的照常写笔记 + 触发总结** |

### B4. 报告结果

`vla learn` 收尾**只打一行**统计(`cli.py:600`),照抄即可 —— **不要自己编格式**:

```text
📊 课程批量结果:翻页 <pages> 页 / 处理 <processed> / 通过 <passed> / 失败 <failed> / 跳过(已转写)<skipped> / 触发总结 <summarized>
```

| 字段 | 含义 |
|---|---|
| 翻页 | 实际翻了几页 |
| 处理 / 通过 / 失败 | 本 session 的逐条结果 |
| 跳过(已转写) | 命中 `logs/transcribed_history.jsonl` 直接跳过 |
| **触发总结** | 达到 6h 阈值触发了 **FR-9 跨视频批量总结**的次数。**不是** FR-2.15d 单视频摘要 —— 那个每通过一条就产出一份,不计在这里 |

⚠️ **这一行不含任何落盘路径**。要报路径必须自己去看产物 → 见 B4b。

**逐条明细看 `vla learn` 的日志**(每条会打 score / 时长 / 落盘路径)。

**失败条目在本次运行内不重试**,贴失败行让用户决定。

⚠️ **但"不重试"≠"下次会跳过"**:`HistoryStore` **只有 `record_success()`**
(`state/history.py:83`),**失败不写 history** ⇒ **下次跑同一门课,失败的条目会被再跑一遍**。
这与"已转写自动跳过"是两回事,报结果时**别说反**。

### B4b. 跑完必须验产物(2026-09-10 新增)

**"通过 N 条"≠"产出 N 份文件"。** 统计数**看不出**摘要有没有真的写出来 ——
2026-09-10 那次事故里,`process_asset` 在质量门控失败分支直接 `return None`,
**FR-2.15d 单视频摘要在整条链路里静默不产出**,而 `vla learn` 照常打统计行、
不报任何异常。只念统计数就会漏掉这类"静默不产出"。

所以每次批量跑完,**必须**核一遍产物:

```bash
ls -1 logs/transcribed/<YYYY-MM-DD>/transcripts/
ls -1 logs/transcribed/<YYYY-MM-DD>/summaries/
```

| 产物 | 何时存在 |
|---|---|
| `<id>_<safe_title>.txt` | 每个**通过质量门控**的条目(canonical,FR-7.7)。**成功路径上唯一的文本产物** |
| `<id>_<safe_title>.summary.txt` | 每个**通过**的条目(FR-2.15d **无条件**触发,不看长度) |
| `<stem>.transcript.txt` / `<stem>.refined.txt` | **只在「未通过」的条目上残留** |

⚠️ **不要数 `transcripts/` 的文件个数** —— 该目录**混着两类命名**:
`<id>_<title>.txt`(成功)与 `<stem>.transcript.txt` / `<stem>.refined.txt`(失败残留)。

⚠️ **成功条目看不到 `.refined.txt` 是正常的,不是 bug。** 成功路径在
`process_asset` Step 6 主动删掉转写中间产物(`main_provider.py:317`,
`TranscriptionLog.discard_transcribe_intermediates`)—— 它们只对**未通过**的视频有诊断价值。
精修后的文本**已经作为 canonical `.txt` 落盘**,没有丢。**别去 happy path 上找这个文件**。

**核对口径**:

- 通过 N 条 → 期望 **N 份 `.txt` + N 份 `.summary.txt`**
- 数量对不上 → **停下查,不要只把统计数报给用户**
- 对不上的话,先看 `logs/failed_texts/`(失败文本)与 `logs/quality_fail.csv`,再看 `logs/transcribe_fail.csv`

⚠️ **`# notes:` 只在未通过条目的 `.refined.txt` 里**。分块精修(FR-2.15c,2026-09-10)
会把"哪几块没精修过 / 有没有触发 `_MAX_CHUNKS` 护栏"写进 `.refined.txt` 末尾一行 `# notes:`:

```bash
tail -3 logs/transcribed/<YYYY-MM-DD>/transcripts/<stem>.refined.txt   # 仅未通过条目存在
```

**但成功条目读不到它** —— Step 6 删 `.refined.txt` 时把 notes **一起删了**
(`save_transcribed` 只写 `cleaned_text`,不写 notes)。⇒ **成功条目上的精修降级
事后无法从产物看出**。

**所以批量真跑时不要把 stdout 丢掉**:`vla learn` 的日志里有
`📏 分块数超出上限…` / `⚠️ LLM 清理调用失败` 这类 warning,
是唯一能发现"成功但降级"的线索。**别用 `| tail -1` 之类的管道把中间输出截掉。**

### B4c. 「通过」不再等于「文本干净」(2026-09-10)

FR-4.2 放宽后,质量门控只判**能不能用**:

- **fail** 只留给"转写失败":大面积乱码 / 重复死循环 / 覆盖面严重不足 / 语速远超正常
- **错别字、繁简混杂、口语化、个别语序混乱** → **pass**,只记进 `issues` + 扣 `score`

⇒ 报告"通过 N 条"时**不要**说成"字幕质量良好"。要说清:**通过门控 = 可用,但可能仍含错别字**。
用户要看细节,就把该条的 score 与 issues 从 `vla learn` 日志里摘出来。

⚠️ **配套后果(用户 2026-09-10 已知悉并接受)**:这些条目会**以"成功"写进
`transcribed_history.jsonl`** ⇒ 下次**不再重跑** —— 即"带错别字但判定可用"的文本
会**进知识库并锁死**。真机实例:`PASS`(应为 Python)、`加碼`(应为 Java)那类错字仍会落盘。

### B5. 批量模式的额外红线

| 触发 | 处理 |
|---|---|
| 用户想批量但给的 URL 是**详情页** | 提示:"批量需要**课程目录页** URL(含 `catalogId` + `cid`),请点进课程目录后复制地址栏" |
| 用户一次想跑**多门课** | 一次只跑一门(一个 `catalogId`)。多门 → 分开跑,或提示这是 FR-11 之外的扩展需求 |
| `--dry-run` 报 0 条 | 目录为空或 `catalogId` 给错 → 停下核对 URL,**不要**直接改 `--limit` 硬试 |

> ⚠️ **不要用 `vla batch` 做这件事** —— `vla batch` 吃的是手写任务列表文件(YAML/JSON),
> 需要用户自己提供每条 URL。课程目录页批量走 `vla learn`(它自己翻页取任务)。

---

## watch 模式(2026-09-14 新增)

**触发**:用户说"先看一下课程"、"只跑浏览器 nav 不转写"、"先开 tab 不录"、
"跑 watch"等。**Step 2b 通过**(catalogId + cid)后,**用户主动选择**走 watch
而非 learn 时进入本节。

> ⚠️ watch 与 learn 的核心区别:**不开录屏 + 不调 process_asset**。Capture
> 质量未达标(2026-09-14 决议)前,跑 watch 只是为了验证**浏览器 nav 路径**
> 能跑通,不要把 watch 当作"轻量版 learn"——它**不产字幕、不写笔记**。

### W1. 直接跑(没有 `--dry-run`,单一模式)

```bash
uv run vla watch \
  --college-id "<cid>" \
  --catalog-id "<catalogId>" \
  --max-sec 30
```

- `--max-sec` 默认 **1800**(30 分钟,JS 侧 setTimeout 兜底)
  - 推荐先 `--max-sec 30` 快速验证 nav 路径(每个 video ~38s,3 个 video ~2min)
  - 验证 OK 后再改 `--max-sec 1800` 跑完整 30 min 兜底
- **不要**加 `--config` / `--cdp-url`(用默认值)
- 已转写过的视频按 `logs/transcribed_history.jsonl` **自动跳过**(同 learn)
- **全程静音播放**(`video.muted=true`,2026-09-14 加):不打扰用户 +
  muted autoplay 更稳(浏览器无需 user gesture)

### W2. 实际行为(每个未转写 video)

1. `extract_browser_audio(video_url=..., disable_capture=True, mute=True,
   max_duration_sec=...)`
2. 打开**新 Chrome tab**(借已登录的 cookie,JWT 自动可用)
3. 双 `page.goto`:先 bill-jc 根 URL 让 SPA 初始化,再 video URL
4. `wait_for_selector("button.yxtf-button--primary")` → click "开始/继续/重新学习"
5. `wait_for_function("video.readyState >= 2")` → `<video>` ready
6. JS evaluate 跑 capture JS,`disableCapture=true` 跳过 MediaRecorder,
   `mute=true` 把 `video.muted=true`,**只等 video.ended 或 setTimeout** → 关闭
7. **不调 process_asset** → 不转写 / 不落盘 / 不打分 / 不摘要 / 不写 history

### W3. 异常处理(2026-09-14 batch 容错)

每个 video 自己 try/except,异常**只 log + 继续下个 video**(不中断整 batch):

- `<video>` 没 ready / button 没渲染 / Chrome tab 被关 → RuntimeError → log + 跳过
- asyncio.wait_for 30 min 安全超时触发 → RuntimeError → log + 跳过
- 任何未知异常 → 同上

### W4. 报告

`vla watch` 收尾**只打一行**统计:

```text
👀 watch 模式(开 tab + 不录 + 不调 process_asset):目录共 <total> 条 / 已转写 <done> 条 / 待转写 <total-done> 条
```

⚠️ **没有产物**:`transcripts/` / `summaries/` 不会有任何新文件(watch 不调
转写链路)。如果用户期望"跑完应该有字幕",那是 `vla learn` 不是 `vla watch`。
**主动说明**,不要让用户误以为字幕在跑。

### W5. 与 learn 的对比

| 维度 | `learn` | `watch` |
|---|---|---|
| 翻页取任务 | ✅ | ✅ |
| 借 cookie 调 list_tasks API | ✅ | ✅ |
| 走 m3u8 直抽(FR-2.30) | ✅ | ❌ |
| 浏览器 MediaRecorder 抓音 | ❌(已删 fallback) | ❌(关录屏) |
| 静音播放(`video.muted=true`) | n/a | ✅(默认) |
| 转写 + 质量门控 | ✅ | ❌ |
| 落盘 transcripts/ summaries/ | ✅ | ❌ |
| 写 history.jsonl | ✅ | ❌ |
| 触发 6h 总结 | ✅ | ❌ |

**何时用 watch**:
- ✅ 验证浏览器 nav 路径(检查 button click / video ready / SPA 重置)
- ✅ 测 JS 侧 setTimeout 兜底行为
- ✅ 调试 capture JS(用 `disable_capture=True` 跳过录屏)
- ❌ 不要拿 watch 当"轻量 learn"——它**不产字幕**,用户会误以为在跑转写

### W6. 重新启用 capture 时(后续)

1. 在 cli.py 的 `_iterate` 函数里,把 `disable_capture=True` 改 `False`
2. 重新装配 `_build_watch_provider`(git 2026-09-14 当天版本里有)+ VideoLearningAgent +
   `run_course_batch` 完整链路
3. 移除 try/except,改用 `main.py:run()` 自带的 batch 容错

---

## 红线(违反即停)

| 触发 | 处理 |
|---|---|
| 用户贴 B 站 / YouTube / b23.tv URL | 拒绝,提示用 `vla process`(本 skill 不兜底) |
| kng_id / catalogId 不匹配 UUID 格式 | 停下,提示用户复制完整 URL |
| Chrome debug 9222 unreachable | 停下,引用 macOS 启法或 `docs/WINDOWS.md §3` |
| spike 报错但退出 0(如 quality fail) | 报告失败,**不**自动重试 |
| 用户中途改主意要换 URL | 重启 Step 1 |

---

## 不要做的事

- ❌ 不自动重试 spike / `vla learn`(失败一次报一次,让用户决定)
- ❌ 不引 Phase 1+ 的 spider / 录屏路径(单视频走 spike 既有 happy path)
- ❌ 不假装 skill 走 `vla process` fallback(严格 bill-jc 专用)
- ❌ 不自动 commit 落盘的 .txt / .summary.txt(让用户决定)
- ❌ **不跨视频做总结** —— 单视频走 FR-2.15d 的 200-300 字摘要,批量走 FR-9 的 6h
  跨视频总结(由 `vla learn` 内部配额触发)。skill 自己不合成跨视频内容

---

## 退出条件

skill 在以下任一情况**自然结束**:

| 情况 | 下一步建议 |
|---|---|
| 单视频跑成功 | 报告落盘路径,提示 "想批量跑整门课?把课程**目录页** URL 粘过来即可" |
| 批量跑成功 | **先按 B4b 核产物数量 + 读 `# notes:`**,再报统计 + 落盘目录,提示 "再跑一门课就直接粘新目录页 URL" |
| 跑失败(spike / learn 报错) | 贴输出 + 定位失败环节,等用户决定 |
| 用户输入非 bill-jc URL | 提示用 `vla process`,不自动切换 |

---

## 相关文档

- `requirements.md` — 需求 SSOT(FR-2.15 / FR-9 / **FR-11 课程目录批量**)
- `docs/WINDOWS.md` — Windows 启 Chrome debug + 跑 spike 的具体步骤
- `scripts/spike_bill_jc_full.py` — 单视频路径的实际执行者
- `src/vla/learn.py` — 批量路径的实际执行者(翻页 + 时长回填)