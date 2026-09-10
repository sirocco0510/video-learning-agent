"""LLMClient 测试(SSOT: requirements.md FR-4.1 + implementation-plan.md Phase 5)。

设计:
- LLMClient 统一 OpenAI 兼容协议,适配 OpenAI / Qwen / DeepSeek
- 配置走 LLMClientConfig(api_key_env / base_url_env 是 env 名,值由 .env 提供)
- chat.completions.create → 取 message.content
- 测试用 mock openai.OpenAI 实例(避免真调 API)
"""

from unittest.mock import MagicMock, patch

import pytest

from vla.config import LLMClientConfig, VLAConfig
from vla.llm.client import LLMClient, LLMEmptyResponseError


# ---------------- Fixtures ----------------


@pytest.fixture
def llm_cfg() -> LLMClientConfig:
    return LLMClientConfig(
        provider="openai",
        api_key_env="OPENAI_API_KEY",
        base_url_env="OPENAI_BASE_URL",
    )


# ---------------- 构造 ----------------


class TestConstruct:
    def test_reads_api_key_from_env(self, llm_cfg):
        """构造时读 env 变量(用 OPENAI_API_KEY 而非值)。"""
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test-xxx", "OPENAI_BASE_URL": "https://api.example.com/v1"}):
            with patch("vla.llm.client.openai.OpenAI") as mock_openai:
                client = LLMClient(llm_cfg, model="gpt-4o-mini")
                mock_openai.assert_called_once_with(
                    api_key="sk-test-xxx", base_url="https://api.example.com/v1"
                )

    def test_uses_config_model(self, llm_cfg):
        """构造时 model 参数被存储为 self.model。"""
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-x", "OPENAI_BASE_URL": "https://x.example.com/v1"}):
            with patch("vla.llm.client.openai.OpenAI"):
                client = LLMClient(llm_cfg, model="gpt-4o-mini")
                assert client.model == "gpt-4o-mini"

    def test_default_base_url_when_env_unset(self, llm_cfg):
        """OPENAI_BASE_URL 未设置 → 用 OpenAI 官方默认 URL。"""
        env = {"OPENAI_API_KEY": "sk-x"}  # 没有 BASE_URL
        with patch.dict("os.environ", env, clear=True):
            with patch("vla.llm.client.openai.OpenAI") as mock_openai:
                client = LLMClient(llm_cfg, model="x")
                # base_url 应回退到 OpenAI 官方
                call_kwargs = mock_openai.call_args.kwargs
                assert "base_url" in call_kwargs
                assert "api.openai.com" in call_kwargs["base_url"]


# ---------------- complete() ----------------


class TestComplete:
    def test_calls_chat_completion(self, llm_cfg):
        """complete() → client.chat.completions.create with messages。"""
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-x", "OPENAI_BASE_URL": "https://x/v1"}):
            with patch("vla.llm.client.openai.OpenAI") as mock_openai_cls:
                # mock OpenAI 实例
                mock_instance = MagicMock()
                mock_completion = MagicMock()
                mock_completion.choices = [MagicMock(message=MagicMock(content="hello"))]
                mock_instance.chat.completions.create.return_value = mock_completion
                mock_openai_cls.return_value = mock_instance

                client = LLMClient(llm_cfg, model="gpt-4o-mini")
                result = client.complete("ping", max_tokens=100, temperature=0.5)

                assert result == "hello"
                # 检查传给 openai 的参数
                call_kwargs = mock_instance.chat.completions.create.call_args.kwargs
                assert call_kwargs["model"] == "gpt-4o-mini"
                assert call_kwargs["max_tokens"] == 100
                assert call_kwargs["temperature"] == 0.5
                # messages 应包含 prompt
                assert call_kwargs["messages"] == [{"role": "user", "content": "ping"}]

    def test_default_temperature_and_tokens(self, llm_cfg):
        """默认 max_tokens=1000, temperature=0.3(稳定 + 短)。"""
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-x", "OPENAI_BASE_URL": "https://x/v1"}):
            with patch("vla.llm.client.openai.OpenAI") as mock_openai_cls:
                mock_instance = MagicMock()
                mock_instance.chat.completions.create.return_value = MagicMock(
                    choices=[MagicMock(message=MagicMock(content="ok"))]
                )
                mock_openai_cls.return_value = mock_instance

                client = LLMClient(llm_cfg, model="x")
                client.complete("test")

                kwargs = mock_instance.chat.completions.create.call_args.kwargs
                assert kwargs["temperature"] == 0.3
                assert kwargs["max_tokens"] == 1000

    def test_propagates_api_exception(self, llm_cfg):
        """openai 异常(网络 / 4xx / 5xx) → 向上传播(FR-3.5 风格)。"""
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-x", "OPENAI_BASE_URL": "https://x/v1"}):
            with patch("vla.llm.client.openai.OpenAI") as mock_openai_cls:
                mock_instance = MagicMock()
                mock_instance.chat.completions.create.side_effect = RuntimeError("rate limit")
                mock_openai_cls.return_value = mock_instance

                client = LLMClient(llm_cfg, model="x")

                with pytest.raises(RuntimeError, match="rate limit"):
                    client.complete("test")


# ---------------- VLAConfig 集成 ----------------


class TestReasoningEffort:
    """reasoning_effort 透传(2026-09-10)。

    背景:deepseek-flash 是 reasoning model,默认推理极啰嗦 —— 实测一个 2507 字
    的 refiner 任务烧掉 16232 reasoning tokens,把 max_tokens=4000 吃光 → content
    返回空。修法是把 reasoning_effort="none" 透传给 API(实测 reasoning 归零)。
    """

    def _client(self, mock_openai_cls, content="ok"):
        mock_instance = MagicMock()
        mock_instance.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content=content), finish_reason="stop")]
        )
        mock_openai_cls.return_value = mock_instance
        return mock_instance

    def test_passes_reasoning_effort_from_config(self):
        """LLMClientConfig.reasoning_effort="none" → 透传给 API。"""
        cfg = LLMClientConfig(
            provider="deepseek", api_key_env="OPENAI_API_KEY",
            base_url_env="OPENAI_BASE_URL", reasoning_effort="none",
        )
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-x", "OPENAI_BASE_URL": "https://x/v1"}):
            with patch("vla.llm.client.openai.OpenAI") as mock_openai_cls:
                mock_instance = self._client(mock_openai_cls)
                LLMClient(cfg, model="deepseek-flash").complete("ping")

                kwargs = mock_instance.chat.completions.create.call_args.kwargs
                assert kwargs["reasoning_effort"] == "none"

    def test_omits_reasoning_effort_when_none(self, llm_cfg):
        """未配置(default None)→ 不传该参数(走端点默认,兼容非 reasoning 模型)。"""
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-x", "OPENAI_BASE_URL": "https://x/v1"}):
            with patch("vla.llm.client.openai.OpenAI") as mock_openai_cls:
                mock_instance = self._client(mock_openai_cls)
                LLMClient(llm_cfg, model="gpt-4o-mini").complete("ping")

                kwargs = mock_instance.chat.completions.create.call_args.kwargs
                assert "reasoning_effort" not in kwargs

    def test_per_call_overrides_config(self):
        """per-call 参数优先于 config。"""
        cfg = LLMClientConfig(
            provider="deepseek", api_key_env="OPENAI_API_KEY",
            base_url_env="OPENAI_BASE_URL", reasoning_effort="none",
        )
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-x", "OPENAI_BASE_URL": "https://x/v1"}):
            with patch("vla.llm.client.openai.OpenAI") as mock_openai_cls:
                mock_instance = self._client(mock_openai_cls)
                LLMClient(cfg, model="deepseek-flash").complete(
                    "ping", reasoning_effort="high"
                )

                kwargs = mock_instance.chat.completions.create.call_args.kwargs
                assert kwargs["reasoning_effort"] == "high"


class TestEmptyResponseIsNotSilent:
    """空 content 必须显式报错(2026-09-10)。

    旧行为 `return resp.choices[0].message.content or ""` 把"被 max_tokens 截断"
    和"模型真的返回空"混为一谈 —— deepseek-flash 的 Refiner 因此静默回退原文,
    真因(finish_reason=length)完全不可见。
    """

    def _client_with(self, mock_openai_cls, content, finish_reason):
        mock_instance = MagicMock()
        mock_instance.chat.completions.create.return_value = MagicMock(
            choices=[
                MagicMock(
                    message=MagicMock(content=content),
                    finish_reason=finish_reason,
                )
            ]
        )
        mock_openai_cls.return_value = mock_instance
        return mock_instance

    def test_raises_on_empty_content(self, llm_cfg):
        """content=None → 抛 LLMEmptyResponseError(不再静默返回 "")。"""
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-x", "OPENAI_BASE_URL": "https://x/v1"}):
            with patch("vla.llm.client.openai.OpenAI") as mock_openai_cls:
                self._client_with(mock_openai_cls, None, "length")
                client = LLMClient(llm_cfg, model="deepseek-flash")

                with pytest.raises(LLMEmptyResponseError):
                    client.complete("ping", max_tokens=4000)

    def test_error_message_names_finish_reason_and_model(self, llm_cfg):
        """报错信息要能直接看出根因:模型名 + finish_reason=length + 截断提示。"""
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-x", "OPENAI_BASE_URL": "https://x/v1"}):
            with patch("vla.llm.client.openai.OpenAI") as mock_openai_cls:
                self._client_with(mock_openai_cls, "", "length")
                client = LLMClient(llm_cfg, model="deepseek-flash")

                with pytest.raises(LLMEmptyResponseError) as ei:
                    client.complete("ping", max_tokens=4000)

                msg = str(ei.value)
                assert "deepseek-flash" in msg
                assert "length" in msg
                assert "max_tokens" in msg

    def test_whitespace_only_content_also_raises(self, llm_cfg):
        """纯空白 content 等同空(content.strip() 判定)。"""
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-x", "OPENAI_BASE_URL": "https://x/v1"}):
            with patch("vla.llm.client.openai.OpenAI") as mock_openai_cls:
                self._client_with(mock_openai_cls, "   \n  ", "stop")
                client = LLMClient(llm_cfg, model="x")

                with pytest.raises(LLMEmptyResponseError):
                    client.complete("ping")

    def test_nonempty_content_returns_even_when_truncated(self, llm_cfg):
        """有内容但被截断(length)→ 仍返回内容,不抛(调用方的容错解析负责补 JSON)。"""
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-x", "OPENAI_BASE_URL": "https://x/v1"}):
            with patch("vla.llm.client.openai.OpenAI") as mock_openai_cls:
                self._client_with(mock_openai_cls, '{"cleaned_text": "被截断', "length")
                client = LLMClient(llm_cfg, model="x")

                result = client.complete("ping", max_tokens=4000)
                assert result == '{"cleaned_text": "被截断'

    def test_normal_response_unaffected(self, llm_cfg):
        """正常响应照常返回。"""
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-x", "OPENAI_BASE_URL": "https://x/v1"}):
            with patch("vla.llm.client.openai.OpenAI") as mock_openai_cls:
                self._client_with(mock_openai_cls, "hello", "stop")
                client = LLMClient(llm_cfg, model="x")

                assert client.complete("ping") == "hello"


class TestVLAConfigIntegration:
    def test_resolves_via_vla_config(self):
        """VLAConfig.llm_client 字段可传入 LLMClient。"""
        cfg = VLAConfig.model_validate({
            "storage": {"tmp_dir": "./tmp", "auto_cleanup_on_pass": True},
            "whisper": {"model": "small", "language": "zh", "segment_seconds": 30, "compute_type": "int8"},
            "video_source": {"prefer_download": True, "download": {"format": "worst"}, "record": {"enabled": True, "screen_index": 2, "fps": 30, "crf": 28, "audio_input": "0", "preset": "ultrafast"}},
            "quality_check": {"enabled": True, "model": "x", "min_score_to_pass": 70, "min_char_per_second": 1.0, "max_char_per_second": 15.0},
            "summary": {"model": "x", "target_words_min": 500, "target_words_max": 800, "notes_file": "./notes/v.md", "cross_video_dedup": True, "trigger_mode": "quota", "notes_section_header": "## x"},
            "quota": {"summary_threshold_sec": 21600, "on_exhausted": "stop_session"},
            "history": {"file": "./logs/h.jsonl"},
            "logging": {"log_dir": "./logs", "notify_on_fail": False, "log_alert_threshold": 50, "log_alert_enabled": True},
            "llm_client": {"provider": "openai", "api_key_env": "OPENAI_API_KEY", "base_url_env": "OPENAI_BASE_URL"},
        })

        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-x", "OPENAI_BASE_URL": "https://x/v1"}):
            with patch("vla.llm.client.openai.OpenAI") as mock_openai:
                LLMClient(cfg.llm_client, model="gpt-4o-mini")
                mock_openai.assert_called_once()