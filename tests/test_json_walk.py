"""json_walk 测试(SSOT: R-13 plan,2026-09-03)。

测试覆盖:
- 基本 passthrough:str/int/float/bool/None
- 集合(list / dict / 混合 / 嵌套)
- 深度嵌套(≥3 层)
- visit 调用次数
- inplace 变异(list / dict / 返回 None)
"""

from __future__ import annotations

from vla.utils.json_walk import walk_strings, walk_strings_inplace


# ---------------- 基本 passthrough ----------------


class TestWalkStringsBasic:
    def test_string_upper(self):
        """字符串会被 visit() 转换。"""
        result = walk_strings("hello", lambda s: s.upper())
        assert result == "HELLO"

    def test_int_passthrough(self):
        """int 原样返回(不变 str)。"""
        result = walk_strings(42, lambda s: s.upper())
        assert result == 42
        assert isinstance(result, int)

    def test_float_passthrough(self):
        """float 原样返回。"""
        result = walk_strings(3.14, lambda s: s.upper())
        assert result == 3.14
        assert isinstance(result, float)

    def test_bool_passthrough(self):
        """bool 原样返回(True/False 不是 str)。"""
        result = walk_strings(True, lambda s: s.upper())
        assert result is True
        result2 = walk_strings(False, lambda s: s.upper())
        assert result2 is False

    def test_none_passthrough(self):
        """None 原样返回。"""
        result = walk_strings(None, lambda s: s.upper())
        assert result is None


# ---------------- 集合 ----------------


class TestWalkStringsCollections:
    def test_list_of_strings(self):
        """list 元素逐个 visit。"""
        result = walk_strings(["a", "bb", "ccc"], lambda s: s.upper())
        assert result == ["A", "BB", "CCC"]

    def test_dict_of_strings(self):
        """dict 的 value 是 str 时逐个 visit(保留原 dict 类型)。"""
        result = walk_strings({"a": "x", "b": "y"}, lambda s: s.upper())
        assert result == {"a": "X", "b": "Y"}
        assert isinstance(result, dict)

    def test_mixed_list_with_primitives(self):
        """list 里混 str/int/None,只 str 被 visit。"""
        result = walk_strings([1, "two", None, 4.0], lambda s: s.upper())
        assert result == [1, "TWO", None, 4.0]

    def test_nested_dict_in_list(self):
        """list[dict[str, str]] → list[dict[str, str]](visit 全部 string leaf)。"""
        result = walk_strings(
            [{"a": "x"}, {"b": "y"}],
            lambda s: s.upper(),
        )
        assert result == [{"a": "X"}, {"b": "Y"}]


# ---------------- 深度嵌套 ----------------


class TestWalkStringsDeepNesting:
    def test_three_level_dict(self):
        """3 层 dict 嵌套,最里层字符串仍被 visit。"""
        data = {"l1": {"l2": {"l3": "deep"}}}
        result = walk_strings(data, lambda s: s.upper())
        assert result == {"l1": {"l2": {"l3": "DEEP"}}}

    def test_dict_list_dict_string(self):
        """dict → list → dict → str 的混合嵌套。"""
        data = {"items": [{"text": "hello"}, {"text": "world"}]}
        result = walk_strings(data, lambda s: s + "!")
        assert result == {"items": [{"text": "hello!"}, {"text": "world!"}]}

    def test_empty_collections_passthrough(self):
        """空 dict / 空 list 原样返回(不调 visit,无错)。"""
        result_d = walk_strings({}, lambda s: s.upper())
        assert result_d == {}
        result_l = walk_strings([], lambda s: s.upper())
        assert result_l == []
        # dict 含空 list
        result_mixed = walk_strings({"k": []}, lambda s: s.upper())
        assert result_mixed == {"k": []}


# ---------------- visit 计数 ----------------


class TestVisitSideEffects:
    def test_visit_count_matches_all_string_values(self):
        """visit() 调用次数 = 容器树中所有 string value 数(不含 dict key)。

        语义:dict key 不动,只有 value 中的 str leaf 触发 visit。
        """
        calls: list[str] = []

        def visit(s: str) -> str:
            calls.append(s)
            return s

        data = {
            "k1": "v1",
            "k2": ["v2", "v3"],
            "k3": {"nested": "v4"},
        }
        walk_strings(data, visit)

        # 4 个 string value:v1 / v2 / v3 / v4("nested" 是 dict key,不算)
        assert len(calls) == 4
        assert sorted(calls) == sorted(["v1", "v2", "v3", "v4"])


# ---------------- inplace 变异 ----------------


class TestWalkStringsInplace:
    def test_mutates_dict_inplace(self):
        """inplace 模式:传入的 dict 本身被修改,不是返回新 dict。"""
        original = {"a": "x", "b": "y"}
        result = walk_strings_inplace(original, lambda s: s.upper())

        # 返回 None
        assert result is None
        # 原 dict 被修改
        assert original == {"a": "X", "b": "Y"}

    def test_mutates_list_inside_dict(self):
        """inplace 模式:dict 里的 list 也被原地修改。"""
        original = {"items": ["a", "b", "c"]}
        walk_strings_inplace(original, lambda s: s.upper())

        assert original == {"items": ["A", "B", "C"]}

    def test_returns_none(self):
        """inplace 模式显式验证返回 None(无返回值语义)。"""
        # 包括顶层容器是 list / dict / str / primitive 的情况
        assert walk_strings_inplace([1, "x"], lambda s: s.upper()) is None
        assert walk_strings_inplace({"k": "v"}, lambda s: s.upper()) is None
        assert walk_strings_inplace("hello", lambda s: s.upper()) is None
        assert walk_strings_inplace(42, lambda s: s.upper()) is None