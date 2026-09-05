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
from vla.llm.client import LLMClient


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


class TestVLAConfigIntegration:
    def test_resolves_via_vla_config(self):
        """VLAConfig.llm_client 字段可传入 LLMClient。"""
        cfg = VLAConfig.model_validate({
            "storage": {"tmp_dir": "./tmp", "auto_cleanup_on_pass": True},
            "whisper": {"model": "small", "language": "zh", "segment_seconds": 30, "compute_type": "int8"},
            "video_source": {"prefer_download": True, "download": {"format": "worst"}, "record": {"enabled": True, "screen_index": 2, "fps": 30, "crf": 28, "audio_input": "0", "preset": "ultrafast"}},
            "quality_check": {"enabled": True, "model": "x", "min_score_to_pass": 70, "min_char_per_second": 1.0, "max_char_per_second": 15.0},
            "browser_plugin": {"name": "VideoTrans", "enabled": True, "remind_timeout_sec": 30, "plugin_paths": []},
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