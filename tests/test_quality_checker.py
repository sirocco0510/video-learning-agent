"""QualityChecker 测试(SSOT: requirements.md FR-4 + implementation-plan.md Phase 5)。

设计:
- 启发式预筛:语速异常(<min 或 >max)直接 fail(不调 LLM)
- 启发式 2:≥3 重复 ≥5 字句子直接 fail(不调 LLM)
- 通过启发式才调 LLM,LLM 返 JSON → 解析 → QualityResult
- LLM 异常向上传播(FR-3.5 风格)

测试用 FakeLLM 注入,避免真调 API。
"""

import json
from pathlib import Path
from typing import Any

import pytest

from vla.config import VLAConfig
from vla.models import QualityResult
from vla.quality.checker import QualityChecker


# ---------------- Mocks ----------------


class FakeLLM:
    """mock LLMClient,记录调用,返回预设文本。"""

    def __init__(self, response: str = ""):
        self.calls: list[dict[str, Any]] = []
        self.response = response

    def complete(self, prompt: str, max_tokens: int = 1000, temperature: float = 0.3) -> str:
        self.calls.append({
            "prompt": prompt,
            "max_tokens": max_tokens,
            "temperature": temperature,
        })
        return self.response


def make_pass_response(score: int = 85) -> str:
    return json.dumps({
        "pass": True,
        "score": score,
        "issues": [],
        "suggestion": "",
    }, ensure_ascii=False)


def make_fail_response(score: int = 50, issues: list[str] | None = None, suggestion: str = "") -> str:
    return json.dumps({
        "pass": False,
        "score": score,
        "issues": issues or ["内容空洞"],
        "suggestion": suggestion or "建议人工审核",
    }, ensure_ascii=False)


# ---------------- Fixtures ----------------


@pytest.fixture
def cfg(tmp_path: Path) -> VLAConfig:
    return VLAConfig.model_validate({
        "storage": {"tmp_dir": "./tmp", "auto_cleanup_on_pass": True},
        "whisper": {"model": "small", "language": "zh", "segment_seconds": 30, "compute_type": "int8"},
        "video_source": {"prefer_download": True, "download": {"format": "worst"}, "record": {"enabled": True, "screen_index": 2, "fps": 30, "crf": 28, "audio_input": "0", "preset": "ultrafast"}},
        "quality_check": {
            "enabled": True,
            "model": "gpt-4o-mini",
            "min_score_to_pass": 70,
            "min_char_per_second": 1.0,
            "max_char_per_second": 15.0,
        },
        "browser_plugin": {"name": "VideoTrans", "enabled": True, "remind_timeout_sec": 30, "plugin_paths": []},
        "summary": {"model": "x", "target_words_min": 500, "target_words_max": 800, "notes_file": "./notes/v.md", "cross_video_dedup": True, "trigger_mode": "quota", "notes_section_header": "## x"},
        "quota": {"summary_threshold_sec": 21600, "on_exhausted": "stop_session"},
        "history": {"file": "./logs/h.jsonl"},
        "logging": {"log_dir": "./logs", "notify_on_fail": False, "log_alert_threshold": 50, "log_alert_enabled": True},
        "llm_client": {"provider": "openai", "api_key_env": "OPENAI_API_KEY", "base_url_env": "OPENAI_BASE_URL"},
    })


@pytest.fixture
def checker(cfg: VLAConfig) -> QualityChecker:
    return QualityChecker(cfg)


def normal_text(n_chars: int = 600) -> str:
    """生成 ~6 char/sec 的正常中文文本(对 100s 视频)。

    用 30+ 条不同的句子循环拼接,确保没有任何句子重复 ≥3 次,
    避免触发重复异常启发式。
    """
    pool = [
        "这是一些正常的中文转写文本。",
        "内容丰富,描述详细,信息密度合理。",
        "今天我们来讨论 Python 装饰器的用法。",
        "装饰器本质是一个接受函数返回函数的高阶函数。",
        "常见的应用场景包括日志记录和权限校验。",
        "with 语法可以配合上下文管理器使用,非常简洁。",
        "生成器通过 yield 关键字实现惰性求值,节省内存。",
        "协程和 async/await 让异步编程更加直观。",
        "类型提示在大型项目中非常重要,提高可维护性。",
        "测试覆盖率不是越高越好,关键路径覆盖即可。",
        "代码审查应该关注可读性和性能,而不是风格偏好。",
        "持续集成可以自动运行测试,及时发现问题。",
        "Docker 让应用打包部署变得简单可重复。",
        "微服务架构适合大型团队协作,但有运维成本。",
        "K8s 提供强大的容器编排能力,适合云原生场景。",
        "服务网格让服务间通信更加可控和可观测。",
        "日志聚合用 ELK 栈非常方便,查询功能强大。",
        "Prometheus 配合 Grafana 是经典的监控组合。",
        "链路追踪帮助快速定位分布式系统问题。",
        "灰度发布可以降低上线风险,逐步放量。",
        "蓝绿部署是常见的零停机部署策略。",
        "回滚机制要提前演练,确保故障时能快速恢复。",
        "混沌工程通过注入故障提高系统韧性。",
        "性能压测要贴近真实场景,才有参考价值。",
        "数据库索引不是越多越好,需要权衡写入性能。",
        "慢查询日志是优化 SQL 的好帮手。",
        "缓存击穿和雪崩需要提前防护。",
        "消息队列可以解耦系统,提高可用性。",
        "幂等性设计让接口可以安全重试。",
        "分布式锁实现要考虑超时和续期问题。",
    ]
    text = ""
    i = 0
    while len(text) < n_chars:
        text += pool[i % len(pool)]
        i += 1
    return text[:n_chars]


# ---------------- 启发式:语速异常 ----------------


class TestHeuristicSpeed:
    def test_cps_below_min_fails_without_llm(self, cfg, checker: QualityChecker):
        """cps < min_char_per_second → fail score=20,不调 LLM。"""
        llm = FakeLLM(response="should not be called")
        checker.set_llm(llm)

        # 50 字 / 600s = 0.08 cps(远低于 1.0)
        text = "短短短" * 20  # 100 字
        result = checker.check(text, "t", duration_sec=600, model_size="small")

        assert isinstance(result, QualityResult)
        assert result.passed is False
        assert result.score == 20
        assert any("语速" in i for i in result.issues)
        assert len(llm.calls) == 0  # 没调 LLM

    def test_cps_above_max_fails_without_llm(self, cfg, checker: QualityChecker):
        """cps > max_char_per_second → fail score=30,不调 LLM。"""
        llm = FakeLLM(response="should not be called")
        checker.set_llm(llm)

        # 1000 字 / 60s = 16.7 cps(高于 15.0)
        text = normal_text(1000)
        result = checker.check(text, "t", duration_sec=60, model_size="small")

        assert result.passed is False
        assert result.score == 30
        assert any("语速" in i for i in result.issues)
        assert len(llm.calls) == 0

    def test_cps_in_range_proceeds_to_llm(self, cfg, checker: QualityChecker):
        """cps 在 [min, max] → 调 LLM。"""
        llm = FakeLLM(response=make_pass_response())
        checker.set_llm(llm)

        # 600 字 / 100s = 6.0 cps
        text = normal_text(600)
        result = checker.check(text, "t", duration_sec=100, model_size="small")

        assert len(llm.calls) == 1  # 调了 LLM

    def test_zero_duration_uses_floor_of_one(self, cfg, checker: QualityChecker):
        """duration_sec=0 → 除数取 max(duration, 1) 防 ZeroDivisionError。"""
        llm = FakeLLM(response=make_pass_response())
        checker.set_llm(llm)

        # 600 字 / max(0, 1) = 600 cps → 远超 max=15 → fail
        text = normal_text(600)
        result = checker.check(text, "t", duration_sec=0, model_size="small")

        assert result.passed is False
        assert any("语速" in i for i in result.issues)


# ---------------- 启发式:重复异常 ----------------


class TestHeuristicRepetition:
    def test_three_plus_repeats_fails_without_llm(self, cfg, checker: QualityChecker):
        """同一 ≥5 字句子重复 ≥3 次 → fail score=10,不调 LLM。"""
        llm = FakeLLM(response="should not be called")
        checker.set_llm(llm)

        text = "这是同一句话。这是同一句话。这是同一句话。今天天气很好。" * 30
        # "这是同一句话" 出现 90 次
        result = checker.check(text, "t", duration_sec=600, model_size="small")

        assert result.passed is False
        assert result.score == 10
        assert any("重复" in i for i in result.issues)
        assert len(llm.calls) == 0

    def test_short_repeated_phrases_ignored(self, cfg, checker: QualityChecker):
        """重复但每条 < 5 字 → 不触发(避免对常见词如 '是的' 误判)。"""
        llm = FakeLLM(response=make_pass_response())
        checker.set_llm(llm)

        # 700 字确保 cps >= 1.0(语速启发式不被触发)
        text = "是的。 是的。 是的。 " + normal_text(700)
        result = checker.check(text, "t", duration_sec=600, model_size="small")

        # "是的。" 只有 2 字,被忽略 → 走 LLM
        assert len(llm.calls) == 1

    def test_two_repeats_ignored(self, cfg, checker: QualityChecker):
        """只重复 2 次 → 不触发(允许常见的 2 次重述)。"""
        llm = FakeLLM(response=make_pass_response())
        checker.set_llm(llm)

        # 2 次重复 + 多样化的正文,确保触发重复阈值
        text = "这是同一句话。这是同一句话。今天天气很好。" + normal_text(700)
        result = checker.check(text, "t", duration_sec=600, model_size="small")

        assert len(llm.calls) == 1


# ---------------- LLM 调用 + JSON 解析 ----------------


class TestLLMCall:
    def test_prompt_contains_video_info(self, cfg, checker: QualityChecker):
        """PROMPT 应包含视频标题 / 时长 / 引擎 / 文本。"""
        llm = FakeLLM(response=make_pass_response())
        checker.set_llm(llm)

        text = normal_text(600)
        checker.check(text, "Python 教程", duration_sec=100, model_size="small")

        prompt = llm.calls[0]["prompt"]
        assert "Python 教程" in prompt
        assert "100" in prompt
        assert "small" in prompt
        assert text in prompt


# ---------------- pass/fail 阈值 ----------------


class TestPassFailThreshold:
    def test_score_above_threshold_passes(self, cfg, checker: QualityChecker):
        """LLM score >= min_score_to_pass → passed=True。"""
        llm = FakeLLM(response=make_pass_response(score=85))
        checker.set_llm(llm)

        result = checker.check(normal_text(600), "t", duration_sec=100, model_size="small")

        assert result.passed is True

    def test_score_below_threshold_fails(self, cfg, checker: QualityChecker):
        """LLM score < min_score_to_pass → passed=False(即使 LLM 写 pass=true)。"""
        llm = FakeLLM(response='{"pass": true, "score": 60, "issues": [], "suggestion": ""}')
        checker.set_llm(llm)

        result = checker.check(normal_text(600), "t", duration_sec=100, model_size="small")

        assert result.passed is False
        assert result.score == 60

    def test_llm_says_false_fails(self, cfg, checker: QualityChecker):
        """LLM 明确 pass=false → passed=False。"""
        llm = FakeLLM(response=make_fail_response(score=80, issues=["术语错误"]))
        checker.set_llm(llm)

        result = checker.check(normal_text(600), "t", duration_sec=100, model_size="small")

        assert result.passed is False
        assert result.score == 80
        assert "术语错误" in result.issues


# ---------------- QualityResult 字段完整性 ----------------


class TestQualityResultFields:
    def test_char_count_recorded(self, cfg, checker: QualityChecker):
        """char_count 总是被填(text 长度)。"""
        llm = FakeLLM(response=make_pass_response())
        checker.set_llm(llm)

        text = normal_text(600)
        result = checker.check(text, "t", duration_sec=100, model_size="small")

        assert result.char_count == len(text)

    def test_issues_and_suggestion_passed_through(self, cfg, checker: QualityChecker):
        """LLM issues / suggestion 透传到 QualityResult。"""
        llm = FakeLLM(response=make_fail_response(
            score=55,
            issues=["重复段落过多", "专业术语不准确"],
            suggestion="建议重新转写或人工修正"
        ))
        checker.set_llm(llm)

        result = checker.check(normal_text(600), "t", duration_sec=100, model_size="small")

        assert "重复段落过多" in result.issues
        assert "专业术语不准确" in result.issues
        assert result.suggestion == "建议重新转写或人工修正"


# ---------------- LLM 注入 ----------------


class TestLLMInjection:
    def test_set_llm_replaces_default(self, cfg, checker: QualityChecker):
        """set_llm() 替换默认 LLM(默认 None = 没初始化)。"""
        llm = FakeLLM(response=make_pass_response())
        checker.set_llm(llm)

        checker.check(normal_text(600), "t", duration_sec=100, model_size="small")

        assert len(llm.calls) == 1

    def test_constructor_accepts_llm(self, cfg):
        """构造函数直接接受 LLM 注入。"""
        llm = FakeLLM(response=make_pass_response())
        c = QualityChecker(cfg, llm=llm)

        c.check(normal_text(600), "t", duration_sec=100, model_size="small")

        assert len(llm.calls) == 1