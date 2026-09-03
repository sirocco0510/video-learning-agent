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
        # With try_code_blocks=False, only the bare JSON gets parsed
        assert parse_json_response(text, try_code_blocks=False) == {"y": 2}


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