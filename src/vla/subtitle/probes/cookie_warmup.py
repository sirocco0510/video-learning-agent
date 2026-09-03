"""Cookie 预热探针:用浏览器打开首页取一次 cookie,后续请求复用。

逻辑:对每个已知平台 host → home_url,先 GET 一次,让网站种 cookie / 跳反爬闸门。
成功 = ok(cookie 已就绪),失败(网络异常 / timeout)= fail。

可定制:构造时传 `home_urls={host: url}` 覆盖默认。
"""

from __future__ import annotations

from vla.subtitle.probe_strategy import ProbeContext, ProbeResult


_DEFAULT_HOME_URLS: dict[str, str] = {
    "bilibili.com": "https://www.bilibili.com",
    "youtube.com": "https://www.youtube.com",
    "youtu.be": "https://www.youtube.com",
}


class CookieWarmupProbe:
    name = "cookie_warmup"

    def __init__(self, home_urls: dict[str, str] | None = None) -> None:
        self.home_urls = dict(home_urls) if home_urls else dict(_DEFAULT_HOME_URLS)

    def match(self, url: str) -> bool:
        return any(host in url for host in self.home_urls)

    def run(self, url: str, ctx: ProbeContext) -> ProbeResult:
        if ctx.session is None:
            return ProbeResult(ok=False, note="no session")
        host = next((h for h in self.home_urls if h in url), None)
        if not host:
            return ProbeResult(ok=False, note="no home for url")
        try:
            ctx.session.get(self.home_urls[host], timeout=5)
        except Exception as e:
            return ProbeResult(ok=False, note=f"warmup failed: {e!r}")
        return ProbeResult(ok=True, note=f"warmed {host}")
