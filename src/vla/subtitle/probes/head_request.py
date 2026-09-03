"""HEAD 请求探针:对所有 HTTP(S) url 生效。

逻辑:用 ctx.session 发 HEAD(allow_redirects=True),2xx/3xx = ok。
session=None → 立刻 fail(没有 HTTP 客户端就跑不动)。
"""

from __future__ import annotations

from vla.subtitle.probe_strategy import ProbeContext, ProbeResult


class HeadRequestProbe:
    name = "head_request"

    def match(self, url: str) -> bool:
        return url.startswith(("http://", "https://"))

    def run(self, url: str, ctx: ProbeContext) -> ProbeResult:
        if ctx.session is None:
            return ProbeResult(ok=False, note="no session")
        try:
            r = ctx.session.head(url, allow_redirects=True, timeout=5)
        except Exception as e:
            return ProbeResult(ok=False, note=f"exception: {e!r}")
        if r.status_code < 400:
            return ProbeResult(ok=True, note=f"HTTP {r.status_code}")
        return ProbeResult(ok=False, note=f"HTTP {r.status_code}")
