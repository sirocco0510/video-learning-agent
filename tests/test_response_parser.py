import pytest
from vla.llm.response import parse_json_response, _try_parse_balanced_object


class TestStripThink:
    def test_strips_think_block_with_json_after(self):
        text = "<think>\n让我想想\n</think>\n{\"pass\": true, \"score\": 80}"
        assert parse_json_response(text) == {"pass": True, "score": 80}

    def test_strips_think_block_with_nested_braces_in_thought(self):
        text = '<think>\n例子 {"a": 1} 不重要\n</think>\n{"x": 2}'
        assert parse_json_response(text) == {"x": 2}

    def test_no_think_block_returns_json_directly(self):
        assert parse_json_response('{"k": "v"}') == {"k": "v"}

    def test_strip_think_false_keeps_think_block(self):
        text = '<think>{"x": 1}</think>{"y": 2}'
        # When strip_think=False, we still try code blocks + brace scan,
        # but {"y": 2} comes AFTER think so it should still be found
        result = parse_json_response(text, strip_think=False)
        assert result == {"y": 2}


class TestCodeBlocks:
    def test_json_code_block(self):
        text = '```json\n{"a": 1, "b": [2, 3]}\n```'
        assert parse_json_response(text) == {"a": 1, "b": [2, 3]}

    def test_plain_code_block_with_json(self):
        text = '```\n{"only": "json inside"}\n```'
        assert parse_json_response(text) == {"only": "json inside"}

    def test_try_code_blocks_false_falls_through(self):
        text = '```json\n{"x": 1}\n``` {"y": 2}'
        # 2026-09-14 行为变更:brace scan 不再 skip code block,即使
        # try_code_blocks=False,也会找第一个 outermost(在 code block 内的
        # {"x": 1});try_code_blocks 只决定是否走 code-block 优先路径,
        # 不再影响 brace scan 的 skip set。
        assert parse_json_response(text, try_code_blocks=False) == {"x": 1}


class TestBraceCounting:
    def test_finds_first_balanced_object(self):
        text = '前缀文字 {"key": "value"} 后缀'
        assert parse_json_response(text) == {"key": "value"}

    def test_handles_nested_objects(self):
        text = '{"outer": {"inner": {"deep": 1}}, "tail": true}'
        assert parse_json_response(text) == {"outer": {"inner": {"deep": 1}}, "tail": True}

    def test_handles_strings_with_braces(self):
        text = '{"text": "hello {world}"}'
        assert parse_json_response(text) == {"text": "hello {world}"}

    def test_handles_escaped_quotes(self):
        text = r'{"a": "say \"hi\""}'
        assert parse_json_response(text) == {"a": 'say "hi"'}


class TestMultipleJsonObjects:
    def test_picks_first_balanced_object(self):
        text = '{"first": 1} {"second": 2}'
        assert parse_json_response(text) == {"first": 1}

    def test_picks_outermost_when_nested(self):
        text = '{"a": {"b": 1}}'
        assert parse_json_response(text) == {"a": {"b": 1}}


class TestFailure:
    def test_no_json_raises(self):
        with pytest.raises(ValueError, match="LLM 响应中没有找到 JSON"):
            parse_json_response("no json here at all")

    def test_unclosed_brace_raises(self):
        with pytest.raises(ValueError, match="LLM 响应中没有找到 JSON"):
            parse_json_response('{"unclosed":')


class TestTryParseBalancedObject:
    def test_returns_none_when_start_not_brace(self):
        assert _try_parse_balanced_object("not json", start=0) is None

    def test_returns_none_on_invalid_json(self):
        assert _try_parse_balanced_object('{"a": }', start=0) is None

    def test_returns_dict_on_valid(self):
        result = _try_parse_balanced_object('{"x": 1}', start=0)
        assert result == {"x": 1}


class TestLLMRealOutput20260914:
    """复现 2026-09-14 batch 现场事故(质量门控 / 摘要解析失败 → EPIPE 进程死)。

    场景:MiniMax-M3 + thinking_mode="disabled" 后,LLM 在 Refiner / QualityChecker /
    VideoSummarizer 三处都用 ```json``` Markdown 包裹 JSON。原有 try_code_blocks
    regex 在以下两类真实输出上 fail:

    (1) JSON 字符串值里出现 ``` 字符(如示例代码)→ regex non-greedy
        在内部 ``` 处截断,inner 不是完整 JSON。
    (2) LLM 响应被 max_tokens 截断,无结束 ``` → regex 整体不匹配。

    修复目标:在两层都拿得到 dict,不抛 ValueError。
    """

    def test_code_block_with_triple_backticks_in_string_value(self):
        """场景 1:JSON 字符串值里有 ```(示例代码)→ 不能让 code block regex 提前结束。"""
        text = (
            '```json\n'
            '{\n'
            '  "summary_text": "示例代码:\n```python\nprint()\n``` 上面是装饰器",\n'
            '  "score": 80\n'
            '}\n'
            '```'
        )
        result = parse_json_response(text)
        assert result["score"] == 80
        assert "装饰器" in result["summary_text"]

    def test_code_block_truncated_no_closing_fence(self):
        """场景 2a:```json 开头但响应被 max_tokens 截断,无结束 ```。

        期望:truncated 路径补 } 解析成功,而不是 raise ValueError。
        """
        # 模拟:max_tokens 截断到 "summary_text": "本视频围" 处
        text = '```json\n{\n  "summary_text": "本视频围'
        result = parse_json_response(text)
        assert "summary_text" in result
        assert "本视频围" in result["summary_text"]

    def test_code_block_truncated_with_partial_string(self):
        """场景 2b:```json 开头 + JSON 截断在 string value 中间(无收尾 ")。"""
        text = '```json\n{\n  "summary_text": "Decor'
        result = parse_json_response(text)
        # truncated 路径会补 " 和 },所以 result["summary_text"] = "Decor"
        assert result["summary_text"] == "Decor"

    def test_unclosed_brace_no_code_block_raises(self):
        """场景 3(回归):既无 code block 也无完整 JSON → 仍应 raise。

        修复不能把"完全没 JSON 的响应"也兜底成空 dict。
        """
        with pytest.raises(ValueError, match="LLM 响应中没有找到 JSON"):
            parse_json_response("完全没 JSON 也没 code block 的纯文本")