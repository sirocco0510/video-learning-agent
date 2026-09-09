import pytest

from vla.subtitle.internal_site_spider import InternalSiteSpider


def test_construct_accepts_default_resolution():
    s = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="abc")
    assert s.resolution == "720p"


def test_construct_accepts_custom_resolution():
    s = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="abc", resolution="480p")
    assert s.resolution == "480p"


@pytest.mark.asyncio
async def test_list_tasks_placeholder_raises_not_implemented():
    s = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="abc")
    with pytest.raises(NotImplementedError, match="走单独 PR"):
        await s.list_tasks()


@pytest.mark.asyncio
async def test_fetch_m3u8_placeholder_raises_not_implemented():
    s = InternalSiteSpider(cdp_url="http://localhost:9222", college_id="abc")
    with pytest.raises(NotImplementedError, match="走单独 PR"):
        await s.fetch_m3u8("kng-id-123")
