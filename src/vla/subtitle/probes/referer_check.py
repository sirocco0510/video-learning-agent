"""Referer 探针:对已知平台(B 站 / YouTube 等)生效,看返回内容是否包含平台特征关键词。

逻辑:用 ctx.session 带 Referer GET,扫前 8KB body 里有没有该平台的特征关键词。
命中 → ok(资源确实可读 + 平台正常响应);未命中 → fail(可能 Referer 被拒 / 反爬)。
"""

from __future__ import annotations

from vla.subtitle.probe_strategy import ProbeContext, ProbeResult


# host → 平台特征关键词列表(任一命中即认为该平台回应正常)
_PLATFORM_KEYWORDS: dict[str, list[str]] = {
    "bilibili.com": ["bilibili", "视频", "投稿"],
    "youtube.com": ["youtube", "watch"],
    "youtu.be": ["youtube", "watch"],
}


class RefererCheckProbe:
    name = "referer_check"

    def match(self, url: str) -> bool:
        return any(host in url for host in _PLATFORM_KEYWORDS)

    def run(self, url: str, ctx: ProbeContext) -> ProbeResult:
        if ctx.session is None:
            return ProbeResult(ok=False, note="no session")
        host = next((h for h in _PLATFORM_KEYWORDS if h in url), "")
        keywords = _PLATFORM_KEYWORDS.get(host, [])
        try:
            r = ctx.session.get(
                url, timeout=5, headers={"Referer": f"https://{host}/"},
            )
        except Exception as e:
            return ProbeResult(ok=False, note=f"exception: {e!r}")
        body = r.text[:8192]
        if any(kw in body for kw in keywords):
            return ProbeResult(
                ok=True, note=f"matched {host} keywords",
                extra={"status_code": r.status_code},
            )
        return ProbeResult(
            ok=False, note=f"no {host} keywords in body",
            extra={"status_code": r.status_code},
        )
