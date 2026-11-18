"""Regression tests for MedGuide issue #9 (low-priority polish collection)."""

from __future__ import annotations

import time

import pytest

from app.metrics import MetricsStore
from app.retrieval import HybridRetriever, IntentRouter
from app.safety import SafetyEngine
from app.sql_guard import ReadOnlySQLGuard
from app.store import SessionStore
from app.workflow import MedGuideWorkflow


def make_workflow() -> MedGuideWorkflow:
    return MedGuideWorkflow(HybridRetriever(), ReadOnlySQLGuard(), SafetyEngine())


# 13. LangGraph path records real per-node timings.
class _SleepingGraph:
    def stream(self, state, stream_mode="updates"):
        yield {"normalize": {"normalized_message": state.get("user_message", "")}}
        time.sleep(0.02)
        yield {"extract_profile": {"profile": {}}}


def test_langgraph_stream_events_carry_real_elapsed_ms() -> None:
    workflow = make_workflow()
    workflow.graph = _SleepingGraph()
    result = workflow.run({"session_id": "timing", "user_message": "咳嗽", "profile": {}})
    events = {event["node"]: event for event in result["events"]}
    assert events["normalize"]["status"] == "completed"
    assert events["extract_profile"]["status"] == "completed"
    assert isinstance(events["extract_profile"]["elapsed_ms"], float)
    assert events["extract_profile"]["elapsed_ms"] >= 10
    assert events["normalize"]["elapsed_ms"] >= 0


# 14. Structured rows are truncated with a visible total.
def test_format_rows_notes_total_when_truncated() -> None:
    text = MedGuideWorkflow._format_rows({"rows": [{"name": "布洛芬", "stock": 1}] * 7})
    assert "共 7 行" in text
    assert "前 5 行" in text
    assert text.count("布洛芬") == 5


def test_format_rows_without_truncation_has_no_total_note() -> None:
    text = MedGuideWorkflow._format_rows({"rows": [{"name": "布洛芬", "stock": 1}] * 5})
    assert "共" not in text


# 15. A comment is saved on a same-rating resubmission, and "commented"
# counts messages that carry a comment (once each).
def test_feedback_comment_saved_on_same_rating_resubmission() -> None:
    metrics = MetricsStore()
    assert metrics.add_feedback("up", "清楚", "m1", session_id="s") is True
    # Same rating, new comment text: the comment is saved, so this changed.
    assert metrics.add_feedback("up", "补充了一句", "m1", session_id="s") is True
    # Same rating, identical comment: nothing changed.
    assert metrics.add_feedback("up", "补充了一句", "m1", session_id="s") is False
    assert metrics.snapshot()["feedback"].get("commented") == 1


def test_feedback_comment_counted_when_rating_changes() -> None:
    metrics = MetricsStore()
    assert metrics.add_feedback("up", None, "m1", session_id="s") is True
    # First comment on this message, attached to a rating change: it counts.
    assert metrics.add_feedback("down", "不好", "m1", session_id="s") is True
    snapshot = metrics.snapshot()
    assert snapshot["feedback"].get("commented") == 1
    assert snapshot["feedback"].get("up", 0) == 0
    assert snapshot["feedback"].get("down") == 1
    # A second message with a comment counts separately.
    assert metrics.add_feedback("up", "清楚", "m2", session_id="s") is True
    assert metrics.snapshot()["feedback"].get("commented") == 2


# 16. Pure questions and colloquial "resolved" wording are not high risk,
# while real emergencies still are.
@pytest.mark.parametrize(
    "text",
    (
        "39度严重吗",
        "胸痛严重吗",
        "胸痛危险吗",
        "胸痛要紧吗",
        "39度吗",
        "昨天胸痛，今天好了",
        "胸痛不疼了",
        "昨天胸痛，今天好多了",
        "昨天发烧39度，今天好了",
        "胸痛严重吗，需要就医吗",
    ),
)
def test_pure_questions_and_resolved_symptoms_are_not_high(text: str) -> None:
    assert SafetyEngine().screen(text).level != "high", text


@pytest.mark.parametrize(
    "text",
    (
        "胸痛很严重",
        "胸痛很严重怎么办",
        "胸痛怎么办？",
        "昨天胸痛，今天好了，但现在又疼",
        "39度，怎么办",
        "39度很严重",
        "我现在胸痛怎么办",
        "胸痛已经缓解，但现在再次胸痛",
        "我胸痛，很严重吗？",
    ),
)
def test_real_emergencies_stay_high(text: str) -> None:
    assert SafetyEngine().screen(text).level == "high", text


# 17. Local mode does not claim network access.
def test_store_public_reports_no_network_in_local_mode() -> None:
    assert SessionStore(mode="local").public({"session_id": "x"})["network_enabled"] is False
    assert SessionStore(mode="offline").public({"session_id": "x"})["network_enabled"] is False
    assert SessionStore(mode="test").public({"session_id": "x"})["network_enabled"] is False
    assert SessionStore(mode="production").public({"session_id": "x"})["network_enabled"] is True


# 18. Test mode reports the real (hash) embedding model.
def test_ready_health_reports_real_embedding_model_in_test_mode() -> None:
    assert HybridRetriever().health()["embedding_model"] == "sha256-token-hash"


# 19. Intent ties are broken by an explicit priority, not dict order.
@pytest.mark.parametrize(
    "text,expected",
    (
        ("发热吃药", "disease"),
        ("咳嗽三天应该挂什么科？", "department"),
        ("皮疹看什么科？", "department"),
        ("布洛芬有什么副作用", "drug"),
        ("头痛吃药", "disease"),
    ),
)
def test_intent_tie_breaks_by_priority(text: str, expected: str) -> None:
    assert IntentRouter().classify(text) == expected


# 20. Common departments are accepted for schedule queries.
@pytest.mark.parametrize(
    "text",
    (
        "眼科排班",
        "耳鼻喉科排班",
        "皮肤科排班",
        "口腔科排班",
        "骨科排班",
        "神经内科排班",
        "神经外科排班",
        "儿科排班",
        "妇产科排班",
        "泌尿外科排班",
        "眼科号源",
    ),
)
def test_common_departments_are_accepted(text: str) -> None:
    result = ReadOnlySQLGuard().from_natural_language(text)
    assert result is not None
    assert result.blocked is False
    assert result.intent == "doctor_schedules"
