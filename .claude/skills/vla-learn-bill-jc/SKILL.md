---
name: vla-learn-bill-jc
description: Use when the user wants to transcribe a video from b-learning.bill-jc.com (公司内部学习平台) — guides URL paste, validates the URL is from bill-jc, extracts kng_id, asks for college_id, and runs scripts/spike_bill_jc_full.py end-to-end. Triggers on "学 bill-jc 视频", "跑 bill-jc 视频", "bill-jc 转写", "跑 spike", or user invoking /vla-learn-bill-jc.
---

# vla-learn-bill-jc

引导用户粘贴 **b-learning.bill-jc.com**(公司内部学习平台)视频链接 → 解析 kng_id → 跑 spike → 落盘到 `logs/transcribed/<date>/transcripts/`(+ 长视频 `summaries/`)。

**只支持 bill-jc 内部站**(2026-09-10 设计决定)。其他平台(B站 / YouTube / b23.tv 短链)请用 `vla process` 主命令,**不在本 skill 兜底**。

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
请粘贴 bill-jc 视频详情页 URL,形如:
  https://b-learning.bill-jc.com/learn/<kng_id>

或 SPA 形式的课程目录页:
  https://b-learning.bill-jc.com/?kngId=<kng_id>
```

(其它域 / path / 短链 → 第 2 步会拒绝)

### 2. URL 校验(白名单)

**必须**匹配以下两种之一:

| 形式 | regex |
|---|---|
| `learn` 路径 | `^https?://b-learning\.bill-jc\.com/learn/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/?$` |
| SPA query | `^https?://b-learning\.bill-jc\.com/\?kngId=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$` |

**拒绝**所有其他形式:
- ❌ `www.bilibili.com/video/BV1xxx` → "本 skill 仅支持 bill-jc 内部站;B 站请用 `uv run vla process --url <url>`"
- ❌ `youtube.com/watch?v=...` → 同上
- ❌ `b23.tv/xxx` → 同上
- ❌ `b-learning.bill-jc.com`(无 kng_id) → "URL 不含 kng_id,请进入具体视频详情页后复制 URL"

通过校验 → 提取 `kng_id`(UUID)。

### 3. 询问 college_id

**URL 里没有 college_id**,必须问用户:

```text
college_id 从课程目录页 URL 拿(形如 /college/<id>/... 或首页 URL path segment)。
例如: --college-id 7c80b070-28ac-4c1a-b54b-35b327b870eb

请提供本视频所属课程的 college_id:
```

如用户在最近对话里已经给过 college_id(同一课程下的多视频),直接复用,不必再问。

### 4. 跑 spike

```bash
uv run python scripts/spike_bill_jc_full.py \
  --college-id <college_id> \
  --kng-id <kng_id> \
  --cdp-url http://localhost:9222
```

- **不要**加 `--config` / `--resolution` / `--verbose`(用脚本默认值)
- **不要**加 `--list-only`(skill 的目的是真跑)
- mac 默认 CDP URL 是 `http://localhost:9222`,Windows 也一样,不用问

### 5. 报告结果

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

## 红线(违反即停)

| 触发 | 处理 |
|---|---|
| 用户贴 B 站 / YouTube / b23.tv URL | 拒绝,提示用 `vla process`(本 skill 不兜底) |
| kng_id 不匹配 UUID 格式 | 停下,提示用户复制完整 URL |
| Chrome debug 9222 unreachable | 停下,引用 macOS 启法或 `docs/WINDOWS.md §3` |
| spike 报错但退出 0(如 quality fail) | 报告失败,**不**自动重试 |
| 用户中途改主意要换 URL | 重启 Step 1 |

---

## 不要做的事

- ❌ 不自动重试 spike(失败一次报一次,让用户决定)
- ❌ 不改 `scripts/spike_bill_jc_full.py`(本次只写 skill 文件)
- ❌ 不引 Phase 1+ 的 spider / 录屏路径(走 spike 既有 happy path)
- ❌ 不假装 skill 走 `vla process` fallback(严格 bill-jc 专用)
- ❌ 不自动 commit spike 落盘的 .txt / .summary.txt(让用户决定)
- ❌ 不处理 batch / cross-video 总结(单视频 scope)

---

## 退出条件

skill 在以下任一情况**自然结束**:

| 情况 | 下一步建议 |
|---|---|
| 跑成功 | 报告落盘路径,提示 "想批量跑其他视频?直接粘新 URL 即可" |
| 跑失败(spike 报错) | 贴 spike 输出 + 定位失败环节,等用户决定 |
| 用户输入非 bill-jc URL | 提示用 `vla process`,不自动切换 |

---

## 相关文档

- `requirements.md` — 需求 SSOT(FR-2.15 / FR-9 等)
- `docs/WINDOWS.md` — Windows 启 Chrome debug + 跑 spike 的具体步骤
- `scripts/spike_bill_jc_full.py` — 实际执行者(spike 子命令化的 Phase 10 任务在 plan 中,本次不动)