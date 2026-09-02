# Web Entry + Persistent Queue(2026-09-02)

## 背景

`video-learning-agent` 当前入口只有 typer CLI(`vla doctor / process / batch`)。
新需求:公司内部视频网站(每个视频一个 ID,课程页列出所有视频)需要一个**用户主动输入**的入口,
爬取课程页 → 解析视频列表 → 批量调现有 `VideoLearningAgent.run()` 流水线。

约束(用户已确认):
- **同 repo**(复用 `VideoLearningAgent` / `HistoryManager` / `QuotaManager` / 配置 / 历史)
- **FastAPI web UI**(浏览器打开 localhost)
- **通用爬虫**(用户表单填 selectors,不写 per-site 代码)
- **Cookie 鉴权**(用户从浏览器 devtools 复制 cookie 粘贴)
- **持久队列 + 后台 worker**(跨 session 恢复)

## 整体架构

```
┌─────────────────┐   提交任务     ┌────────────────────┐
│  FastAPI Web UI │ ────────────▶ │  Persistent Queue  │
│  localhost:8000 │                │  ./queue/jobs.jsonl│
│                 │                └─────────┬──────────┘
│  • 列表页 URL   │                          │ 读
│  • CSS 选择器   │                          ▼
│  • URL 模板     │                ┌────────────────────┐
│  • Cookie       │                │  Background Worker │
│  • 进度查看     │                │  (FastAPI daemon   │
└─────────────────┘                │   thread 启动时起) │
                                    └─────────┬──────────┘
                                              │ 逐个 pop + run
                                              ▼
                              ┌──────────────────────────┐
                              │  Generic Scraper         │
                              │  (httpx + BS4 + Cookie)  │
                              │  → list[VideoTask]       │
                              └─────────┬────────────────┘
                                        │
                                        ▼
                              ┌──────────────────────────┐
                              │  VideoLearningAgent      │
                              │  (Phase 8,无改动)       │
                              └──────────────────────────┘
```

## 模块布局

```text
src/vla/
├── ui_web/                  # 新增:Web UI
│   ├── server.py            # FastAPI app + 启动 worker
│   ├── routes.py            # /api/scrape/preview, /api/queue/submit, /api/queue/status
│   └── templates/
│       └── index.html       # 单页表单 + 队列状态(jinja2)
├── scraper/                 # 新增:通用爬虫
│   ├── generic.py           # GenericScraper(url + selectors + cookie)
│   ├── selector.py          # SelectorSchema pydantic
│   └── url_template.py      # {id} / {slug} 模板渲染
├── queue/                   # 新增:持久队列
│   ├── jobs.py              # Job + VideoItem pydantic
│   ├── store.py             # jobs.jsonl CRUD(append-only)
│   └── worker.py            # BackgroundWorker(thread)
```

## Selector Schema(§ 1)

```jsonc
{
  "list_url": "https://internal.example.com/courses/python-basics",
  "cookie": "session=eyJ...; csrf=...",

  "engine": "css",      // "css" 默认走 BS4; "xpath" 走 lxml 的 xpath(支持 //div[@class='x']/a 这种语法)
  "render": false,      // false 默认纯 HTML,httpx 一次拉完;true 走 Playwright 渲染(SPA/React 站点),慢但能拿 JS DOM

  "selectors": {
    "container": "li.video-item",          // 必填:每个视频的容器 CSS/xpath 选择器
    "id": "[data-vid]",                     // 可选:视频 ID 提取(给 URL 模板用)
    "id_regex": "v(\\d+)",                  // 可选:从 id 选择器文本里再 regex 提取
    "title": ".title",                      // 可选;不填则用 ID 当 title
    "duration": ".duration::text",          // 可选:提取"12:34" 这种;自动转秒
    "detail_url": "a::attr(href)"           // 可选:详情页 URL(相对/绝对都支持)
  },

  "detail_url_template": "https://internal.example.com/videos/{id}"
  // 当 detail_url 没填时,用模板构造。支持 {id} / {slug} / {course_slug} 占位符
}
```

**`engine` 字段(2026-09-02 新增)**:
- `"css"`(默认):`selectors` 字段走 BS4 的 `.select()` / `.select_one()` + `::attr(name)` / `::text` 后缀
- `"xpath"`:走 `lxml.etree` 的 `.xpath()` — 完整 xPath 1.0 支持(无 `::attr` 后缀,属性提取写 `//a/@href`)
- 混用:不推荐,要么全 CSS 要么全 xPath

**`render` 字段(2026-09-02 新增)**:
- `false`(默认):`httpx.get(list_url)` 拿 HTML,直接 BS4 解析
- `true`:启 Playwright Chromium → 访问 list_url → `page.wait_for_load_state("networkidle")` → `page.content()` 拿渲染后 DOM → BS4 解析
- 副作用:慢(每个列表页 +5-10s 启动浏览器),内存峰值 +200MB;依赖 `playwright install chromium`

**两种爬取模式自动适配**:
- 列表页有 `<a href="/videos/123">` → 走 `detail_url` 选择器,直接拿 URL
- 列表页只有 `<li data-vid="123">` → 走 `detail_url_template`,用 `{id}` 拼完整 URL

**`::attr(...)` / `::text` 后缀**(自定扩展 BS4):
- `a::attr(href)` → 取 `<a>` 的 `href` 属性
- `.duration::text` → 取 `.duration` 元素的纯文本(strip 空白)
- 不带后缀 → 取 `get_text(strip=True)`
- 选择器前缀仍按 BS4 语法解析(支持 `.class` / `#id` / `[attr]` / `tag` / 组合)

**时长解析**:`duration` 字段支持自动识别:
- `"12:34"` → 754 秒
- `"1:02:34"` → 3754 秒
- `"754"` → 754 秒
- `"1h 5m"` → 3900 秒(正则提取)
- 解析失败 → 0,UI 标红提示用户手动输入

**`group_id` 推断**:`POST /api/queue/submit` 默认从 list_url 提取最后一段非数字 slug(如 `python-basics`),用户可在 UI 改写;`group_title` 单独输入框。

## Persistent Queue Format(§ 2)

**`./queue/jobs.jsonl`** 每行一个 job:

```json
{
  "job_id": "uuid",
  "created_at": "2026-09-02T16:30:00",
  "status": "pending",           // pending | running | paused | done | failed
  "scraper_config": { ... § 1 Selector Schema 全文 ... },
  "videos": [
    {
      "video_id": "v123",
      "title": "第一讲",
      "url": "https://.../videos/v123",
      "expected_duration": 754,
      "status": "pending",       // pending | running | done | failed | skipped
      "attempts": 0,
      "last_error": null,
      "started_at": null,
      "finished_at": null
    }
  ],
  "group_id": "python-basics",           // 默认从 URL slug 推断
  "group_title": "Python 基础",          // 用户可在 UI 改
  "stats": { "total": 30, "done": 0, "failed": 0, "skipped": 0 }
}
```

**去重**:Worker pop video 前先查 `history.is_already_done(url_key)`(复用 `HistoryManager`),命中 → 标 `skipped`。
**崩溃恢复**:Worker 每完成一个 video 调 `store.update_video_status()` — **采用"周期性 rewrite"策略**(避免 append-only 的状态分裂问题):每 10 秒或每完成 5 个 video,worker 把整个 `jobs.jsonl` 重新写一遍(用 `tempfile + rename` 保证原子性)。重启时扫描 status=running → 重置为 pending 重新跑。

## Worker 设计(§ 3)

- **进程模型**:FastAPI 启动时(`on_event("startup")`)起一个 **daemon thread** 跑 worker 循环。Worker poll interval 默认 2s(可配 `web.worker_poll_interval_sec`)。
- **循环逻辑**:
  ```
  while not shutdown:
      if queue.empty(): sleep(2); continue
      if queue.paused: sleep(2); continue
      job = queue.pop_next_video()           # pending + 未在 history 中
      if not job: sleep(2); continue
      try:
          VideoLearningAgent.run([task])     # 复用现有 Phase 8
      except Exception as e:
          queue.mark_failed(video_id, str(e))
      quota.add(duration)
  ```
- **API 控制**:`POST /api/queue/pause` / `/api/queue/resume` 改全局 paused 标志(写在 `queue/state.json`,简单的 `{"paused": true/false}` 二态)。
- **取消 job**:`POST /api/queue/cancel?job_id=xxx` → 把 job 标 `cancelled` + 所有 `pending` video 标 `cancelled`,worker 跳过。已 running 的不强制中断(尊重 LLM 配额)。
- **错误**:单视频失败不中断整个 job;job 内失败率 >50% → 标 `failed` 并暂停。
- **UI 实时性**:前端轮询 `/api/queue/status?job_id=xxx`(每 2 秒 GET JSON)。
- **未来升级**:Worker 多进程时改 multiprocessing + 文件锁,本次 Phase 1 单线程够用。

## Web UI 表单(§ 4)

**单页 HTML**(jinja2 模板,无 JS 框架 — vanilla JS 轮询即可):

```html
<form id="scrape-form">
  <input name="list_url" placeholder="课程页 URL" required>
  <textarea name="cookie" placeholder="Cookie(从浏览器复制)"></textarea>
  
  <fieldset>
    <legend>CSS 选择器</legend>
    <input name="container" placeholder="容器,如 li.video-item" required>
    <input name="id" placeholder="ID 选择器,如 [data-vid]">
    <input name="title" placeholder="标题选择器">
    <input name="duration" placeholder="时长选择器">
    <input name="detail_url" placeholder="详情页 URL 选择器">
  </fieldset>
  
  <input name="detail_url_template" placeholder="URL 模板,如 https://.../videos/{id}">
  
  <button type="button" onclick="preview()">🔍 测试预览</button>
  <button type="submit">📥 加入队列</button>
</form>

<div id="preview-result"></div>
<div id="queue-status"></div>  <!-- 轮询填充 -->
```

**API endpoints**:

| Method | Path | 用途 |
|---|---|---|
| GET | `/` | 单页 HTML |
| POST | `/api/scrape/preview` | 给配置,返回前 3 个视频的 {id, title, url, duration} |
| POST | `/api/queue/submit` | 完整爬取 + 写入 jobs.jsonl + 启动 worker |
| GET | `/api/queue/status?job_id=xxx` | 返回 job 当前进度 |
| POST | `/api/queue/pause` | 全局暂停 |
| POST | `/api/queue/resume` | 全局恢复 |
| POST | `/api/queue/cancel?job_id=xxx` | 取消某 job |

## 集成点(§ 5)

**VideoLearningAgent.run() 无需改动**。新代码只在入口侧组装 `list[VideoTask]` + 把 `list[VideoTask]` 喂进去:

```python
# src/vla/queue/worker.py
from vla.main import VideoLearningAgent
from vla.models import VideoTask

class BackgroundWorker:
    def __init__(self, agent: VideoLearningAgent, store: QueueStore):
        self.agent = agent
        self.store = store

    def process_one(self, item: VideoItem) -> bool:
        task = VideoTask(
            id=item.video_id,
            title=item.title,
            url=item.url,
            expected_duration=item.expected_duration,
            group_id=self.job.group_id,
            group_title=self.job.group_title,
        )
        stats = self.agent.run([task])
        return stats["passed"] > 0
```

**配置**:`config/vla.yaml` 新增 `web` 段(server 端口、worker sleep interval)。
**CLI**:`vla web` 子命令起 FastAPI(typer 已有,加一行)。
**依赖隔离**:`pyproject.toml` `[project.optional-dependencies]`:

```toml
[project.optional-dependencies]
web = [
  "fastapi>=0.110",
  "uvicorn[standard]>=0.27",
  "beautifulsoup4>=4.12",
  "lxml>=5.0",          # xPath engine (engine="xpath" 需要)
  "jinja2>=3.1",
  "httpx>=0.27",
  "playwright>=1.40",   # render=true 需要
]
```

**`playwright install chromium`** 单独跑一次(用户首次启 `render=true` 时提示)。

## 落地 Phase(§ 6)

| Phase | 内容 | 验收 |
|---|---|---|
| **Web.1** | `scraper/selector.py` + `scraper/generic.py` + `scraper/url_template.py` + 25 单元测试 | 给 2-3 份真实课程页 HTML 样本,能正确输出 list[{id, title, url, duration}];空选区 / 缺字段 / 错 regex 都报友好错 |
| **Web.2** | `queue/jobs.py` + `queue/store.py` + 15 测试 | append / pop_video / update_status / pause-resume / 崩溃恢复(running → pending) 全覆盖 |
| **Web.3** | `queue/worker.py` + 与 `VideoLearningAgent` 集成测试 | mock agent 跑通 worker 循环;失败容错 |
| **Web.4** | `ui_web/server.py` + `routes.py` + `templates/index.html` | 浏览器能填表单、能预览前 3 条、能提交、能看进度(轮询) |
| **Web.5** | E2E + 真实站 spike | 用户给一份带 cookie 的课程页 → 端到端转写 1 个视频成功 → 队列状态正确 |

## 失败模式 / 兜底(§ 7)

| 场景 | 处理 |
|---|---|
| 列表页 cookie 失效 | preview 阶段就 401 → 返回友好错"cookie 无效" |
| 选择器命中 0 条 | preview 阶段返回 0 + 提示"选择器可能错了,试试更宽泛的 container" |
| 单视频转写失败 | 标 failed,worker 继续下一个(attempts++);attempts > 3 标永久 failed |
| Worker 崩溃 | 重启后扫描 status=running → 重置为 pending 重新跑 |
| queue/jobs.jsonl 损坏 | 启动时备份成 `.bak` + log error + 跳过损坏行 |
| 并发提交 | 单 worker 串行;UI 端 disable 提交按钮 2 秒防重复 |

## 不在本 spec 范围

- 多 worker 并行(性能优化)
- 视频详情页二次爬取(取准确时长)— 列表页有就用,没有用 0 占位
- SSE / WebSocket 实时进度 — 轮询够用
- 用户登录 / 鉴权 — localhost 自用
- 视频字幕历史检索 — 已有 `logs/transcribed_history.jsonl`,UI 后续 phase
- 移动端适配 — 桌面端浏览器优先

## 待用户提供(开始 Web.1 前)

- [ ] 1 份课程页 HTML 样本(去掉敏感信息后)— 验证 selector 解析正确
- [ ] 1 个视频详情页 URL 样本 — 确认 URL 模板格式
- [ ] Cookie 字段说明 — 哪些 cookie 是必需的(只粘需要的,别全粘)
