"""BilibiliOfficialSubtitle 测试(策略 ①,SSOT: requirements.md FR-2.1 + implementation-plan.md Phase 3)。

三步 API 调用,httpx 全部 mock;extract_bv_id / get_subtitle 两个公开方法。
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from vla.subtitle.bilibili_official import BilibiliOfficialSubtitle


# ---------------- extract_bv_id ----------------


class TestExtractBvId:
    def test_extracts_bvid_from_standard_url(self):
        s = BilibiliOfficialSubtitle()
        assert s.extract_bv_id("https://www.bilibili.com/video/BV1abc123") == "BV1abc123"

    def test_extracts_bvid_from_short_url(self):
        s = BilibiliOfficialSubtitle()
        assert s.extract_bv_id("https://b23.tv/BV1abc123") == "BV1abc123"

    def test_extracts_bvid_with_query_params(self):
        s = BilibiliOfficialSubtitle()
        assert s.extract_bv_id(
            "https://www.bilibili.com/video/BV1abc123?spm_id_from=333.788&p=1"
        ) == "BV1abc123"

    def test_invalid_url_raises(self):
        s = BilibiliOfficialSubtitle()
        with pytest.raises(ValueError):
            s.extract_bv_id("https://example.com/no-bvid-here")


# ---------------- get_subtitle ----------------


def _make_response(json_data: dict) -> MagicMock:
    """构造 httpx.Response 风格的 mock。"""
    r = MagicMock()
    r.json.return_value = json_data
    r.raise_for_status.return_value = None
    return r


class TestGetSubtitle:
    def test_returns_none_when_no_view_data(self):
        """view API 返回非 0 → None。"""
        s = BilibiliOfficialSubtitle()
        with patch(
            "vla.subtitle.bilibili_official.httpx.get",
            return_value=_make_response({"code": -404, "message": "啥都木有"}),
        ):
            assert s.get_subtitle("https://www.bilibili.com/video/BV1xxx") is None

    def test_returns_none_when_no_subtitles(self):
        """player API 返回的 subtitles 列表为空 → None。"""
        s = BilibiliOfficialSubtitle()
        responses = [
            _make_response({"code": 0, "data": {"cid": 12345}}),
            _make_response({"code": 0, "data": {"subtitle": {"subtitles": []}}}),
        ]
        with patch(
            "vla.subtitle.bilibili_official.httpx.get",
            side_effect=responses,
        ):
            assert s.get_subtitle("https://www.bilibili.com/video/BV1xxx") is None

    def test_returns_text_when_zh_hans_available(self):
        """zh-Hans 命中优先级最高,拼接 body[].content。"""
        s = BilibiliOfficialSubtitle()
        responses = [
            _make_response({"code": 0, "data": {"cid": 12345}}),
            _make_response({
                "code": 0,
                "data": {
                    "subtitle": {
                        "subtitles": [
                            {"lan": "en-US", "lan_doc": "英文(美国)", "subtitle_url": "//aisub.example.com/en.json"},
                            {"lan": "zh-Hans", "lan_doc": "中文(简体)", "subtitle_url": "//sub.example.com/zh.json"},
                        ]
                    }
                },
            }),
            _make_response({
                "body": [
                    {"from": 0.0, "to": 2.0, "content": "你好"},
                    {"from": 2.0, "to": 4.0, "content": "世界"},
                ]
            }),
        ]
        with patch(
            "vla.subtitle.bilibili_official.httpx.get",
            side_effect=responses,
        ) as mock_get:
            result = s.get_subtitle("https://www.bilibili.com/video/BV1xxx")

        assert result is not None
        text, metadata = result
        assert text == "你好\n世界"
        assert metadata["language"] == "zh-Hans"
        assert metadata["lan_doc"] == "中文(简体)"
        # 第三次请求 URL 应该是 https:// + 原 url
        last_call_url = mock_get.call_args_list[2].args[0]
        assert last_call_url.startswith("https://")
        assert last_call_url.endswith("/zh.json")

    def test_falls_back_to_zh_cn_when_no_zh_hans(self):
        """没有 zh-Hans 时,选 zh-CN。"""
        s = BilibiliOfficialSubtitle()
        responses = [
            _make_response({"code": 0, "data": {"cid": 12345}}),
            _make_response({
                "code": 0,
                "data": {
                    "subtitle": {
                        "subtitles": [
                            {"lan": "zh-CN", "lan_doc": "中文", "subtitle_url": "//sub.example.com/zhcn.json"},
                        ]
                    }
                },
            }),
            _make_response({"body": [{"from": 0.0, "to": 1.0, "content": "嗨"}]}),
        ]
        with patch(
            "vla.subtitle.bilibili_official.httpx.get",
            side_effect=responses,
        ):
            text, metadata = s.get_subtitle("https://www.bilibili.com/video/BV1xxx")

        assert metadata["language"] == "zh-CN"
        assert text == "嗨"

    def test_returns_none_when_only_unwanted_languages(self):
        """只有不在优先级的语言 → None。"""
        s = BilibiliOfficialSubtitle()
        responses = [
            _make_response({"code": 0, "data": {"cid": 12345}}),
            _make_response({
                "code": 0,
                "data": {
                    "subtitle": {
                        "subtitles": [
                            {"lan": "ja-JP", "lan_doc": "日语", "subtitle_url": "//ja.json"},
                        ]
                    }
                },
            }),
        ]
        with patch(
            "vla.subtitle.bilibili_official.httpx.get",
            side_effect=responses,
        ):
            assert s.get_subtitle("https://www.bilibili.com/video/BV1xxx") is None
