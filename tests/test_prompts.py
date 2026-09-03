from vla.llm.prompts import build_chat_prompt, enforce_json_response


class TestBuildChatPrompt:
    def test_joins_system_and_user_with_blank_line(self):
        result = build_chat_prompt("SYS", "USER")
        assert result == "SYS\n\nUSER"

    def test_empty_system(self):
        assert build_chat_prompt("", "USER") == "\nUSER"

    def test_empty_user(self):
        assert build_chat_prompt("SYS", "") == "SYS\n\n"

    def test_multiline_preserved(self):
        sys = "line1\nline2"
        user = "lineA\nlineB"
        assert build_chat_prompt(sys, user) == "line1\nline2\n\nlineA\nlineB"


class TestEnforceJsonResponse:
    def test_default_extra_appended(self):
        result = enforce_json_response("base system")
        assert "只输出 JSON" in result
        assert result.startswith("base system")

    def test_custom_extra(self):
        result = enforce_json_response("base", extra="Respond with JSON only")
        assert "Respond with JSON only" in result

    def test_appended_at_end_with_blank_line_separator(self):
        result = enforce_json_response("base")
        # 双换行分隔,LLM 容易把约束当独立指令
        assert result.endswith("只输出 JSON")

    def test_no_duplicate_appending(self):
        """如果已经包含 JSON 指令,不要重复加(避免 prompt 变长)。"""
        first = enforce_json_response("base")
        second = enforce_json_response(first)
        assert second.count("只输出 JSON") == 1
