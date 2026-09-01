"""BrowserDriver 测试(SSOT: requirements.md FR-2.4/2.5/2.6/2.7/2.8/2.10 + implementation-plan.md Phase 3.1)。

4 种 JS 探测 + 跨 origin context.request。
通过注入 mock browser_provider 避免依赖真实 playwright。
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vla.config import VLAConfig
from vla.subtitle.browser_driver import BrowserDriver


# ---------------- Mocks ----------------


class FakeResponse:
    def __init__(self, status: int, body, content_type: str = "application/json"):
        self.status = status
        self._body = body
        self.headers = {"content-type": content_type}

    def json(self):
        if isinstance(self._body, (dict, list)):
            return self._body
        return json.loads(self._body)

    def text(self):
        if isinstance(self._body, str):
            return self._body
        return json.dumps(self._body, ensure_ascii=False)


class FakeRequest:
    def __init__(self, responses: dict[str, FakeResponse] | None = None):
        self.responses = responses or {}
        self.calls: list[str] = []

    def get(self, url: str):
        self.calls.append(url)
        if url in self.responses:
            return self.responses[url]
        raise KeyError(f"no mock response for {url}")


class FakePage:
    """mock page,evaluate 返回预设值,顺序消费 responses 列表。"""

    def __init__(self, evaluate_responses: list | None = None, goto_ok: bool = True):
        self.evaluate_responses = list(evaluate_responses or [])
        self.evaluate_calls: list[str] = []
        self.goto_calls: list[str] = []
        self.wait_calls: list[int] = []
        self.closed = False
        self._goto_ok = goto_ok
        self.context = None  # 由测试 fixture 注入

    def goto(self, url, **kwargs):
        self.goto_calls.append(url)
        if not self._goto_ok:
            raise RuntimeError("goto failed")

    def wait_for_timeout(self, ms: int):
        self.wait_calls.append(ms)

    def close(self):
        self.closed = True

    def evaluate(self, script: str, *args):
        self.evaluate_calls.append(script)
        if not self.evaluate_responses:
            return None
        return self.evaluate_responses.pop(0)


class FakeContext:
    def __init__(self, request: FakeRequest):
        self.request = request


class FakeBrowser:
    def __init__(self, contexts: list[FakeContext]):
        self.contexts = contexts


# ---------------- Fixtures ----------------


@pytest.fixture
def cfg(tmp_path: Path) -> VLAConfig:
    return VLAConfig.model_validate({
        "storage": {"tmp_dir": "./tmp", "auto_cleanup_on_pass": True},
        "whisper": {"model": "small", "language": "zh", "segment_seconds": 30, "compute_type": "int8"},
        "video_source": {
            "prefer_download": True,
            "download": {"format": "worst"},
            "record": {"enabled": True, "screen_index": 2, "fps": 30, "crf": 28, "audio_input": "0", "preset": "ultrafast"},
        },
        "quality_check": {"enabled": True, "model": "x", "min_score_to_pass": 70, "min_char_per_second": 1.0, "max_char_per_second": 15.0},
        "browser_plugin": {"name": "VideoTrans", "enabled": True, "remind_timeout_sec": 30, "plugin_paths": []},
        "summary": {"model": "x", "target_words_min": 500, "target_words_max": 800, "notes_file": "./notes/v.md", "cross_video_dedup": True, "trigger_mode": "quota", "notes_section_header": "## x"},
        "quota": {"summary_threshold_sec": 21600, "on_exhausted": "stop_session"},
        "history": {"file": "./logs/h.jsonl"},
        "logging": {"log_dir": "./logs", "notify_on_fail": False, "log_alert_threshold": 50, "log_alert_enabled": True},
        "llm_client": {"provider": "openai", "api_key_env": "OPENAI_API_KEY", "base_url_env": "OPENAI_BASE_URL"},
    })


@pytest.fixture
def driver(cfg: VLAConfig) -> BrowserDriver:
    return BrowserDriver(cfg)


# ---------------- connect() ----------------


class TestConnect:
    def test_connect_calls_browser_provider(self, driver: BrowserDriver):
        """connect() 调用注入的 browser_provider,获取 browser。"""
        fake_browser = FakeBrowser(contexts=[FakeContext(FakeRequest())])
        driver.set_browser_provider(lambda: fake_browser)

        result = driver.connect()
        assert result is fake_browser

    def test_default_debugging_url_uses_port_9222(self, cfg: VLAConfig):
        """_cdp_url() 拼出 config.puppeteer.cdp_url()(默认 9222)。"""
        d = BrowserDriver(cfg)
        assert d._cdp_url() == "http://localhost:9222"

    def test_custom_debugging_port(self, cfg: VLAConfig):
        """debugging_port 可被配置覆盖。"""
        cfg.puppeteer.debugging_port = 9333
        d = BrowserDriver(cfg)
        assert d._cdp_url() == "http://localhost:9333"


# ---------------- new_background_page() ----------------


class TestNewBackgroundPage:
    def test_creates_page_from_first_context(self, driver: BrowserDriver, cfg: VLAConfig):
        ctx = FakeContext(FakeRequest())
        browser = FakeBrowser(contexts=[ctx])
        driver.set_browser_provider(lambda: browser)
        driver.connect()

        # FakeContext 需要 new_page 方法
        page_created = []

        def new_page():
            p = FakePage()
            page_created.append(p)
            return p

        ctx.new_page = new_page

        page = driver.new_background_page()
        assert page is page_created[0]

    def test_closes_page_after_fetch(self, driver: BrowserDriver):
        """fetch_subtitle_via_browser 完成后关闭 page。"""
        ctx = FakeContext(FakeRequest())
        browser = FakeBrowser(contexts=[ctx])
        page = FakePage(evaluate_responses=[None])  # 全 miss
        page.context = ctx
        ctx.new_page = lambda: page

        driver.set_browser_provider(lambda: browser)
        driver.connect()

        driver.fetch_subtitle_via_browser(page, "https://example.com/v/1")
        assert page.closed


# ---------------- fetch_subtitle_via_browser:4 种 JS 探测 ----------------


class TestMethod1TrackTag:
    def test_track_tag_returns_subtitle(self, driver: BrowserDriver):
        """<track> 标签命中 → 拿 src 跨 origin 下载,返回文本。"""
        sub_url = "https://cdn.example.com/x.vtt"
        ctx = FakeContext(FakeRequest(responses={
            sub_url: FakeResponse(200, "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\n你好", "text/vtt"),
        }))
        page = FakePage(evaluate_responses=[
            {"src": sub_url, "lang": "zh-CN"},  # track probe
        ])
        page.context = ctx

        text, meta = driver.fetch_subtitle_via_browser(page, "https://example.com/v/1")
        assert text is not None
        assert "你好" in text
        assert meta["method"] == "track"
        assert meta["lang"] == "zh-CN"

    def test_track_with_no_src_returns_no_match(self, driver: BrowserDriver):
        """<track> 存在但 src 为空 → 跳到下一探测。"""
        page = FakePage(evaluate_responses=[{"src": "", "lang": "zh"}])
        page.context = FakeContext(FakeRequest())

        text, meta = driver.fetch_subtitle_via_browser(page, "https://example.com/v/1")
        assert text is None


class TestMethod2InitialState:
    def test_initial_state_returns_subtitle(self, driver: BrowserDriver):
        """__INITIAL_STATE__ 找到字幕 URL → 跨 origin 下载。"""
        sub_url = "https://cdn.example.com/x.json"
        ctx = FakeContext(FakeRequest(responses={
            sub_url: FakeResponse(200, {"body": [{"from": 0, "to": 1, "content": "世界"}]}),
        }))
        page = FakePage(evaluate_responses=[
            None,  # 1. track miss
            sub_url,  # 2. initial_state hit
        ])
        page.context = ctx

        text, meta = driver.fetch_subtitle_via_browser(page, "https://example.com/v/1")
        assert text == "世界"
        assert meta["method"] == "initial_state"

    def test_initial_state_no_match_falls_through(self, driver: BrowserDriver):
        page = FakePage(evaluate_responses=[None, None])
        page.context = FakeContext(FakeRequest())

        text, meta = driver.fetch_subtitle_via_browser(page, "https://example.com/v/1")
        assert text is None


class TestMethod3PlayerObject:
    def test_player_getsubtitle_returns_subtitle(self, driver: BrowserDriver):
        """window.player.getSubtitle() 命中。"""
        page = FakePage(evaluate_responses=[
            None,  # track
            None,  # initial_state
            "玩家对象字幕",  # player.getSubtitle
        ])
        page.context = FakeContext(FakeRequest())

        text, meta = driver.fetch_subtitle_via_browser(page, "https://example.com/v/1")
        assert text == "玩家对象字幕"
        assert meta["method"] == "player_object"


class TestMethod4DomSelector:
    def test_dom_selector_returns_subtitle(self, driver: BrowserDriver):
        """DOM 扫描字幕文本。"""
        page = FakePage(evaluate_responses=[
            None,  # track
            None,  # initial_state
            None,  # player
            "DOM 扫描到的字幕",  # dom
        ])
        page.context = FakeContext(FakeRequest())

        text, meta = driver.fetch_subtitle_via_browser(page, "https://example.com/v/1")
        assert text == "DOM 扫描到的字幕"
        assert meta["method"] == "dom_selector"


class TestAllMiss:
    def test_all_methods_miss_returns_none(self, driver: BrowserDriver):
        page = FakePage(evaluate_responses=[None, None, None, None])
        page.context = FakeContext(FakeRequest())

        text, meta = driver.fetch_subtitle_via_browser(page, "https://example.com/v/1")
        assert text is None
        assert meta is None

    def test_priority_order_track_before_dom(self, driver: BrowserDriver):
        """track 命中时,不应调到 DOM 探测。"""
        sub_url = "https://cdn.example.com/x.vtt"
        ctx = FakeContext(FakeRequest(responses={
            sub_url: FakeResponse(200, "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\ntrack hit", "text/vtt"),
        }))
        # 只提供 1 个 evaluate response(track probe),如果走到 player/DOM 就会 IndexError
        page = FakePage(evaluate_responses=[{"src": sub_url, "lang": "zh"}])
        page.context = ctx

        text, meta = driver.fetch_subtitle_via_browser(page, "https://example.com/v/1")
        assert "track hit" in text
        assert meta["method"] == "track"


# ---------------- _fetch_subtitle_text:跨域处理 ----------------


class TestFetchSubtitleText:
    def test_protocol_relative_url_gets_https_prefix(self, driver: BrowserDriver):
        """// 开头的 URL 自动补 https:。"""
        ctx = FakeContext(FakeRequest(responses={
            "https://cdn.example.com/x.vtt": FakeResponse(200, "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhi", "text/vtt"),
        }))
        page = FakePage()
        page.context = ctx

        result = driver._fetch_subtitle_text(page, "//cdn.example.com/x.vtt")
        assert result is not None
        assert "hi" in result
        assert "https://cdn.example.com/x.vtt" in ctx.request.calls

    def test_http_error_returns_none(self, driver: BrowserDriver):
        ctx = FakeContext(FakeRequest(responses={
            "https://cdn.example.com/x.vtt": FakeResponse(404, ""),
        }))
        page = FakePage()
        page.context = ctx

        result = driver._fetch_subtitle_text(page, "https://cdn.example.com/x.vtt")
        assert result is None

    def test_request_exception_returns_none(self, driver: BrowserDriver):
        ctx = FakeContext(FakeRequest())
        page = FakePage()
        page.context = ctx

        result = driver._fetch_subtitle_text(page, "https://broken.example.com/x")
        assert result is None
