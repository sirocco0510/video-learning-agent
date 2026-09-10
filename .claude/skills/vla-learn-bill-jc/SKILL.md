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

两种都只支持 bill-jc 内部站。其他平台(B站 / YouTube / b23.tv 短链)请用 `vla process` 主命令,**不在本 skill 兜底**。

---

## 进入 skill 前的硬性前置

- ✅ `uv run vla doctor` 全 OK(失败项先补,见 `.claude/CLAUDE.md`)
- ✅ Chrome debug 9222 已启,且 Chrome 里已登录 b-learning.bill-jc.com
  - macOS 启法:`/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-debug-vla`
  - Windows 启法见 [docs/WINDOWS.md §3](../../docs/WINDOWS.md)
- ✅ Spike 脚本存在:`scripts/spike_bill_jc_full.py`(vla doctor 隐式检查)
- ✅ 工作区 clean 或用户明确说"可以混着改"

任一不满足 → **停下,先解决**,不进入 skill。

---

## 流程

### 1. 引导用户粘贴 URL

向用户说:

```text
请粘贴 bill-jc 链接 —— 两种都可以:

① 单个视频(详情页):
     https://b-learning.bill-jc.com/learn/<kng_id>
     https://b-learning.bill-jc.com/kng/#/video/play?kngId=<kng_id>

② 整门课(课程目录页)—— 会按目录翻页批量跑:
     https://b-learning.bill-jc.com/kng/#/list?catalogId=<X>&cid=<Y>&order=0&sort=0&type=
```

(其它域 / path / 短链 → 第 2 步会拒绝)

### 2. URL 校验(白名单)

**必须先判断是哪一种**,再分别校验。

#### 2a. 单视频(含 `kngId`)—— 三种形式任一

| 形式 | regex |
|---|---|
| `learn` 路径 | `^https?://b-learning\.bill-jc\.com/learn/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/?$` |
| SPA query(根 + kngId) | `^https?://b-learning\.bill-jc\.com/\?kngId=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$` |
| **SPA 视频详情页**(常见) | `^https?://b-learning\.bill-jc\.com/kng/#/video/play\?kngId=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:&.*)?$` |

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

> ⚠️ **去重依赖 `logs/transcribed_history.jsonl`**。该文件由 `agent.run` 在每条
> 成功后写入 —— **`scripts/spike_bill_jc_full.py` 从不写它**(spike 直接调
> fetch/process,不经过 agent)。所以:**此前用 spike 跑过的视频不会被 batch 认成
> "已转写",会重跑一遍**。若用户在意,先确认 history 文件内容再解释差异。

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

### B3. 停法(报给用户时要说清是哪一种)

| 停法 | 日志/输出 | 含义 |
|---|---|---|
| 目录翻完 | `🏁 翻页结束:offset=N 无更多视频` | 全跑完 |
| 配额到 | `🛑 累计配额已到(X.Xh)…→ 停止翻页` | 累计 6h(`summary_threshold_sec`),session 结束。**已跑完的照常写笔记 + 触发总结** |

### B4. 报告结果

```text
✅ 课程批量转写完成
   翻页 <pages> 页 / 处理 <processed> / 通过 <passed> / 失败 <failed> / 跳过(已转写)<skipped>
   落盘: logs/transcribed/<YYYY-MM-DD>/transcripts/<id>_<safe_title>.txt
```

**逐条明细看 `vla learn` 的日志**(每条会打 score / 时长 / 落盘路径);
**失败条目不重试**,贴失败行让用户决定。

### B5. 批量模式的额外红线

| 触发 | 处理 |
|---|---|
| 用户想批量但给的 URL 是**详情页** | 提示:"批量需要**课程目录页** URL(含 `catalogId` + `cid`),请点进课程目录后复制地址栏" |
| 用户一次想跑**多门课** | 一次只跑一门(一个 `catalogId`)。多门 → 分开跑,或提示这是 FR-11 之外的扩展需求 |
| `--dry-run` 报 0 条 | 目录为空或 `catalogId` 给错 → 停下核对 URL,**不要**直接改 `--limit` 硬试 |

> ⚠️ **不要用 `vla batch` 做这件事** —— `vla batch` 吃的是手写任务列表文件(YAML/JSON),
> 需要用户自己提供每条 URL。课程目录页批量走 `vla learn`(它自己翻页取任务)。

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
| 批量跑成功 | 报告统计 + 落盘目录,提示 "再跑一门课就直接粘新目录页 URL" |
| 跑失败(spike / learn 报错) | 贴输出 + 定位失败环节,等用户决定 |
| 用户输入非 bill-jc URL | 提示用 `vla process`,不自动切换 |

---

## 相关文档

- `requirements.md` — 需求 SSOT(FR-2.15 / FR-9 / **FR-11 课程目录批量**)
- `docs/WINDOWS.md` — Windows 启 Chrome debug + 跑 spike 的具体步骤
- `scripts/spike_bill_jc_full.py` — 单视频路径的实际执行者
- `src/vla/learn.py` — 批量路径的实际执行者(翻页 + 时长回填)