from vla.utils.bvid import extract_bvid, make_url_key


class TestExtractBvid:
    def test_standard_url(self):
        assert extract_bvid("https://www.bilibili.com/video/BV1dzui69EYV") == "BV1dzui69EYV"

    def test_url_with_query_string(self):
        assert extract_bvid("https://www.bilibili.com/video/BV1vE411W7VE?p=1") == "BV1vE411W7VE"

    def test_short_url(self):
        assert extract_bvid("https://b23.tv/abc123") is None  # 短链没有 bvid 模式

    def test_no_bvid_returns_none(self):
        assert extract_bvid("https://example.com/") is None

    def test_empty_string_returns_none(self):
        assert extract_bvid("") is None

    def test_lowercase_bv_prefix(self):
        assert extract_bvid("https://www.bilibili.com/video/bv1xxx") == "bv1xxx"


class TestMakeUrlKey:
    def test_no_p(self):
        assert make_url_key("python", "BV1xxx") == "bilibili://group/python/BV1xxx"

    def test_with_p(self):
        assert make_url_key("python", "BV1xxx", p=2) == "bilibili://group/python/BV1xxx?p=2"

    def test_with_p_zero(self):
        assert make_url_key("g", "BV1", p=0) == "bilibili://group/g/BV1?p=0"

    def test_with_none_p_omits_query(self):
        assert make_url_key("g", "BV1", p=None) == "bilibili://group/g/BV1"
