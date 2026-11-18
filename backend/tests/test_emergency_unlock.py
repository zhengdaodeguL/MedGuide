"""Issue #5: after an emergency turn the session must not stay locked in
emergency mode. Risk is re-evaluated from each message: unrelated
follow-up questions get normal answers (with retrieval), the earlier
high-risk turn is kept in ``risk_history`` and shown as a brief reminder
on the first normal turn, and a new high-risk message re-enters the
emergency flow.
"""

from __future__ import annotations

from app.retrieval import HybridRetriever
from app.safety import SafetyEngine
from app.sql_guard import ReadOnlySQLGuard
from app.workflow import MedGuideWorkflow

EMERGENCY_PREFIX = "当前描述包含需要优先排除的风险信号"


def _workflow() -> MedGuideWorkflow:
    return MedGuideWorkflow(HybridRetriever(), ReadOnlySQLGuard(), SafetyEngine())


def _emergency_turn() -> dict:
    return _workflow().run(
        {
            "session_id": "emergency-unlock",
            "user_message": "胸口突然剧烈疼痛，呼吸困难怎么办？",
            "profile": {"symptoms": [], "associated_symptoms": []},
        }
    )


def test_emergency_response_names_120_and_carries_disclaimer() -> None:
    first = _emergency_turn()

    assert first["risk_level"] == "high"
    assert "120" in first["response"]
    assert "不替代医生诊断或处方" in first["response"]


def test_drug_question_after_emergency_is_answered_normally() -> None:
    first = _emergency_turn()
    second = _workflow().run(
        {**first, "user_message": "布洛芬怎么吃？有什么注意事项？"}
    )

    assert first["risk_level"] == "high"
    assert second["risk_level"] == "low"
    assert not second["response"].startswith(EMERGENCY_PREFIX)
    assert second["citations"], "a normal follow-up should retrieve references again"
    # The earlier emergency is not forgotten: history keeps it and the
    # answer carries a brief reminder.
    assert [entry["level"] for entry in second["risk_history"]].count("high") == 1
    assert "再次出现" in second["response"]


def test_unrelated_question_after_emergency_is_answered_normally() -> None:
    first = _emergency_turn()
    second = _workflow().run({**first, "user_message": "今天天气怎么样"})

    assert second["risk_level"] == "low"
    assert not second["response"].startswith(EMERGENCY_PREFIX)


def test_emergency_recurs_when_high_risk_signals_return() -> None:
    first = _emergency_turn()
    second = _workflow().run(
        {**first, "user_message": "布洛芬怎么吃？有什么注意事项？"}
    )
    assert second["risk_level"] == "low"
    third = _workflow().run({**second, "user_message": "胸痛又发作了，呼吸困难"})

    assert third["risk_level"] == "high"
    assert third["citations"] == []
    assert third["response"].startswith(EMERGENCY_PREFIX)
    assert "120" in third["response"]
    assert [entry["level"] for entry in third["risk_history"]].count("high") == 2


def test_reminder_only_on_first_turn_after_emergency() -> None:
    first = _emergency_turn()
    second = _workflow().run(
        {**first, "user_message": "布洛芬怎么吃？有什么注意事项？"}
    )
    assert "再次出现" in second["response"]
    third = _workflow().run({**second, "user_message": "今天天气怎么样"})

    assert third["risk_level"] == "low"
    assert "再次出现" not in third["response"]


def test_watch_level_is_reevaluated_too() -> None:
    workflow = _workflow()
    first = workflow.run(
        {
            "session_id": "watch-reevaluate",
            "user_message": "发热38度",
            "profile": {"symptoms": [], "associated_symptoms": []},
        }
    )
    assert first["risk_level"] == "watch"
    second = workflow.run({**first, "user_message": "今天天气怎么样"})

    assert second["risk_level"] == "low"
    assert not second["response"].startswith(EMERGENCY_PREFIX)
