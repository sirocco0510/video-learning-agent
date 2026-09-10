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

**必须**匹配以下三种之一:

| 形式 | regex |
|---|---|
| `learn` 路径 | `^https?://b-learning\.bill-jc\.com/learn/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/?$` |
| SPA query(根 + kngId) | `^https?://b-learning\.bill-jc\.com/\?kngId=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$` |
| **SPA 视频详情页**(常见) | `^https?://b-learning\.bill-jc\.com/kng/#/video/play\?kngId=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:&.*)?$` |

**拒绝**所有其他形式:
- ❌ `www.bilibili.com/video/BV1xxx` → "本 skill 仅支持 bill-jc 内部站;B 站请用 `uv run vla process --url <url>`"
- ❌ `youtube.com/watch?v=...` → 同上
- ❌ `b23.tv/xxx` → 同上
- ❌ `b-learning.bill-jc.com`(无 kng_id) → "URL 不含 kng_id,请进入具体视频详情页后复制 URL"

通过校验 → 提取 `kng_id`(UUID)。

### 3. 默认走 `--parse-only`(2026-09-10 设计:让用户先看参数,再决定跑不跑)

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