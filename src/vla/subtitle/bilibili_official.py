"""B站官方 CC 字幕(策略 ①,SSOT: requirements.md FR-2.1 + implementation-plan.md Phase 3)。

三步 API 调用:
  1. GET /x/web-interface/view?bvid=xxx → cid
  2. GET /x/player/v2?bvid=xxx&cid=xxx → subtitle_url
  3. GET subtitle_url → body[].content 拼接

语言优先级:zh-Hans > zh-CN > zh-Hant > en-US > en。
"""

import re

import httpx

LANG_PRIORITY = ["zh-Hans", "zh-CN", "zh-Hant", "en-US", "en"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com",
}

_BVID_PATTERN = re.compile(r"(BV[a-zA-Z0-9]+)")


class BilibiliOfficialSubtitle:
    """策略 ①:B站官方 CC 字幕。"""

    def extract_bv_id(self, url: str) -> str:
        """从 URL 中提取 bvid(BV 开头的字母数字串)。"""
        match = _BVID_PATTERN.search(url)
        if not match:
            raise ValueError(f"无法从 URL 提取 bvid: {url}")
        return match.group(1)

    def _get_json(self, url: str) -> dict:
        r = httpx.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return r.json()

    def get_subtitle(self, url: str) -> tuple[str, dict] | None:
        """三步获取官方字幕;失败或无字幕返回 None。"""
        bvid = self.extract_bv_id(url)

        # Step 1: cid
        view = self._get_json(
            f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
        )
        if view.get("code") != 0:
            return None
        cid = view["data"]["cid"]

        # Step 2: subtitle list
        player = self._get_json(
            f"https://api.bilibili.com/x/player/v2?bvid={bvid}&cid={cid}"
        )
        if player.get("code") != 0:
            return None
        subtitles = (
            player.get("data", {}).get("subtitle", {}).get("subtitles", [])
        )
        if not subtitles:
            return None

        # 按语言优先级选
        chosen = None
        for lang in LANG_PRIORITY:
            for sub in subtitles:
                if sub.get("lan") == lang:
                    chosen = sub
                    break
            if chosen:
                break
        if not chosen:
            return None

        # Step 3: 下载字幕 JSON
        sub_url = chosen["subtitle_url"]
        if sub_url.startswith("//"):
            sub_url = "https:" + sub_url
        body_data = self._get_json(sub_url)
        text = "\n".join(item["content"] for item in body_data.get("body", []))
        metadata = {
            "language": chosen.get("lan"),
            "lan_doc": chosen.get("lan_doc"),
            "ai_status": chosen.get("ai_status"),
            "bvid": bvid,
            "cid": cid,
        }
        return text, metadata
