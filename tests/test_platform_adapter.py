"""PlatformAdapter Protocol + Registry 测试(SSOT: requirements.md FR-2.0 + implementation-plan.md Phase 3.0)。

测试 register / get_for_url / match 域名 / 找不到返回 None / 多 adapter 注册顺序。
"""

from vla.subtitle.platform_adapter import PlatformAdapterRegistry


class FakeBilibiliAdapter:
    """测试用 adapter,只匹配 bilibili.com / b23.tv。"""

    def __init__(self):
        self.created_with = None

    @classmethod
    def match(cls, url: str) -> bool:
        return "bilibili.com" in url or "b23.tv" in url

    def fetch_api_subtitle(self, url: str):
        return None

    def fetch_browser_subtitle(self, driver, url: str):
        return None

    def fetch_via_recording(self, driver, url: str, duration_sec: int):
        return None


class FakeInternalAdapter:
    """测试用 adapter,只匹配 internal.example.com。"""

    @classmethod
    def match(cls, url: str) -> bool:
        return "internal.example.com" in url

    def fetch_api_subtitle(self, url: str):
        return None

    def fetch_browser_subtitle(self, driver, url: str):
        return None

    def fetch_via_recording(self, driver, url: str, duration_sec: int):
        return None


class TestRegister:
    def test_register_adds_adapter_class(self):
        reg = PlatformAdapterRegistry()
        reg.register(FakeBilibiliAdapter)
        assert FakeBilibiliAdapter in reg.list_adapters()

    def test_register_dedups_same_class(self):
        """重复注册同一类只算一次。"""
        reg = PlatformAdapterRegistry()
        reg.register(FakeBilibiliAdapter)
        reg.register(FakeBilibiliAdapter)
        assert len(reg.list_adapters()) == 1


class TestGetForUrl:
    def test_returns_instance_when_match(self):
        reg = PlatformAdapterRegistry()
        reg.register(FakeBilibiliAdapter)
        adapter = reg.get_for_url("https://www.bilibili.com/video/BV1xxx")
        assert isinstance(adapter, FakeBilibiliAdapter)

    def test_returns_none_when_no_match(self):
        reg = PlatformAdapterRegistry()
        reg.register(FakeBilibiliAdapter)
        adapter = reg.get_for_url("https://www.youtube.com/watch?v=xxx")
        assert adapter is None

    def test_matches_b23_short_url(self):
        """b23.tv 短链也要能匹配。"""
        reg = PlatformAdapterRegistry()
        reg.register(FakeBilibiliAdapter)
        adapter = reg.get_for_url("https://b23.tv/BV1xxx")
        assert isinstance(adapter, FakeBilibiliAdapter)

    def test_multiple_adapters_first_match_wins(self):
        """多个 adapter 时,第一个匹配返回。"""
        reg = PlatformAdapterRegistry()
        reg.register(FakeBilibiliAdapter)
        reg.register(FakeInternalAdapter)
        # bilibili URL → Bilibili adapter
        a1 = reg.get_for_url("https://www.bilibili.com/video/BV1xxx")
        # internal URL → Internal adapter
        a2 = reg.get_for_url("https://internal.example.com/v/123")
        assert isinstance(a1, FakeBilibiliAdapter)
        assert isinstance(a2, FakeInternalAdapter)

    def test_empty_registry_returns_none(self):
        reg = PlatformAdapterRegistry()
        assert reg.get_for_url("https://www.bilibili.com/video/BV1xxx") is None

    def test_get_for_url_instantiates_fresh_each_call(self):
        """每次 get_for_url 返回新实例(状态隔离)。"""
        reg = PlatformAdapterRegistry()
        reg.register(FakeBilibiliAdapter)
        a1 = reg.get_for_url("https://www.bilibili.com/video/BV1xxx")
        a2 = reg.get_for_url("https://www.bilibili.com/video/BV1yyy")
        assert a1 is not a2  # 不同实例


class TestListAdapters:
    def test_empty_initially(self):
        reg = PlatformAdapterRegistry()
        assert reg.list_adapters() == []

    def test_returns_registered_in_order(self):
        reg = PlatformAdapterRegistry()
        reg.register(FakeBilibiliAdapter)
        reg.register(FakeInternalAdapter)
        assert reg.list_adapters() == [FakeBilibiliAdapter, FakeInternalAdapter]


class TestRegisterInstance:
    """2026-09-02 新增:支持 pre-built adapter 实例注册(带 deps)。"""

    def test_register_instance_returns_same_object(self):
        """带 deps 的 adapter(构造需要参数)用 register_instance 注册,
        get_for_url 应返回同一实例(共享 deps),不是新实例。"""
        reg = PlatformAdapterRegistry()
        instance = FakeBilibiliAdapter()
        instance.deps = {"injected": True}  # 模拟 deps
        reg.register_instance(instance)

        result = reg.get_for_url("https://www.bilibili.com/video/BV1xxx")
        assert result is instance
        assert result.deps == {"injected": True}

    def test_instance_takes_priority_over_class(self):
        """实例先匹配;类 fallback。"""
        reg = PlatformAdapterRegistry()
        instance = FakeBilibiliAdapter()
        instance.tag = "instance"
        reg.register_instance(instance)
        reg.register(FakeBilibiliAdapter)  # class also registered

        result = reg.get_for_url("https://www.bilibili.com/video/BV1xxx")
        assert result is instance  # 实例优先,不是新 class 实例
        assert getattr(result, "tag", None) == "instance"

    def test_list_adapters_excludes_instances(self):
        """list_adapters() 只返回 class,instance 通过 list_instances() 看。
        保持 legacy 测试不破。"""
        reg = PlatformAdapterRegistry()
        reg.register(FakeBilibiliAdapter)
        reg.register_instance(FakeBilibiliAdapter())

        assert reg.list_adapters() == [FakeBilibiliAdapter]
        assert len(reg.list_instances()) == 1

    def test_instance_only_no_class_registered(self):
        """只注册实例时,get_for_url 仍能匹配。"""
        reg = PlatformAdapterRegistry()
        instance = FakeInternalAdapter()  # 不通过 register() 注册
        # 显式 instance 注册
        reg._instances.append(instance)

        result = reg.get_for_url("https://internal.example.com/v/123")
        assert result is instance
