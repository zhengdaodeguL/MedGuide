from app.retrieval import HybridRetriever
from app.safety import SafetyEngine
from app.sql_guard import ReadOnlySQLGuard
from app.workflow import MedGuideWorkflow, WORKFLOW_ORDER, WorkflowCancelled
import pytest
import threading


def make_workflow() -> MedGuideWorkflow:
    return MedGuideWorkflow(HybridRetriever(), ReadOnlySQLGuard(), SafetyEngine())


def make_workflow_for_runner(runner: str) -> MedGuideWorkflow:
    if runner == "langgraph":
        pytest.importorskip("langgraph")
    workflow = make_workflow()
    if runner == "deterministic":
        workflow.graph = None
    else:
        assert workflow.graph is not None
    return workflow


def assert_progress_contract(state: dict) -> None:
    assert [event["node"] for event in state["events"]] == list(WORKFLOW_ORDER)
    assert [event["node"] for event in state["events"] if event["status"] == "completed"] == state["workflow_path"]
    assert [event["node"] for event in state["events"] if event["status"] == "skipped"] == state["workflow_skipped_nodes"]


@pytest.mark.parametrize("text,sex", [
    ("32岁男性，咳嗽三天", "男"), ("28岁女性咳嗽", "女"),
    ("我32岁男，咳嗽", "男"), ("我男朋友32岁男性，咳嗽", None),
    ("不是32岁男性", None), ("32岁男朋友咳嗽", None),
])
def test_compact_age_gender_profile(text, sex) -> None:
    result = make_workflow().extract_profile({"normalized_message": text, "profile": {}})
    assert result["profile"].get("sex") == sex


@pytest.mark.parametrize("text", ["我突然剧烈头痛", "我突然剧烈腹痛", "呕吐物是绿色", "我有咖啡渣样呕吐"])
def test_new_topic_emergency_signals_skip_generation(text):
    result = make_workflow().run({"session_id": "synthetic", "user_message": text, "profile": {}})
    assert result["risk_level"] == "high"
    assert result["citations"] == []
    assert "急诊" in result["response"]


def test_cough_retrieval_does_not_expand_demographics_into_urinary_advice():
    result = make_workflow().run({"session_id": "synthetic", "user_message":
        "32岁男性，咳嗽三天，没有胸痛，没有呼吸困难", "profile": {}})
    ids = [item["id"] for item in result["citations"]]
    assert any(item.startswith("disease-cough-001") for item in ids)
    assert not any(item.startswith("disease-uti-001") for item in ids)


def test_workflow_collects_profile_and_citations() -> None:
    workflow = make_workflow()
    result = workflow.run({"session_id": "test", "user_message": "我32岁，咳嗽持续3天，想知道看什么科室", "profile": {"symptoms": []}})
    assert result["intent"] in ("department", "disease")
    assert result["profile"]["age"] == 32
    assert result["profile"]["duration"] == "3天"
    assert result["citations"]
    assert result["response"]
    assert "32岁" in result["summary"]
    assert "主要症状：咳嗽" in result["summary"]
    assert len(result["events"]) == 9


def test_workflow_engine_label_matches_actual_runner() -> None:
    workflow = make_workflow()
    result = workflow.run({"session_id": "engine", "user_message": "咳嗽", "profile": {"symptoms": []}})

    assert result["workflow_engine"] == ("langgraph" if workflow.graph is not None else "deterministic")


def test_workflow_never_drafts_diagnosis_for_red_flag() -> None:
    result = make_workflow().run({"session_id": "test", "user_message": "突然胸痛，呼吸困难", "profile": {"symptoms": []}})
    assert result["risk_level"] == "high"
    assert "急诊" in result["response"]
    assert "确诊" not in result["response"]
    assert result["citations"] == []


def test_workflow_executes_structured_query() -> None:
    result = make_workflow().run({"session_id": "test", "user_message": "查布洛芬库存", "profile": {"symptoms": []}})
    assert result["intent"] == "structured"
    assert result["structured_result"]["blocked"] is False
    assert result["structured_result"]["rows"]


def test_workflow_routes_retrieval_and_structured_queries_exclusively() -> None:
    retrieval = make_workflow().run(
        {"session_id": "route-rag", "user_message": "我32岁，咳嗽3天", "profile": {}}
    )
    assert retrieval["workflow_version"] == "2026.11"
    assert retrieval["workflow_path"] == [
        "normalize", "extract_profile", "intent", "risk", "retrieve", "draft", "safety_review", "finalize"
    ]
    assert "structured_query" in retrieval["workflow_skipped_nodes"]
    assert any(event["status"] == "skipped" for event in retrieval["events"])

    structured = make_workflow().run(
        {"session_id": "route-sql", "user_message": "查布洛芬库存", "profile": {}}
    )
    assert structured["workflow_path"] == [
        "normalize", "extract_profile", "intent", "risk", "structured_query", "draft", "safety_review", "finalize"
    ]
    assert "retrieve" in structured["workflow_skipped_nodes"]


@pytest.mark.parametrize("runner", ["langgraph", "deterministic"])
def test_sql_to_rag_turn_does_not_reuse_previous_structured_result(runner: str) -> None:
    workflow = make_workflow_for_runner(runner)
    first = workflow.run({"session_id": "sql-rag", "user_message": "查布洛芬库存", "profile": {}})
    assert first["structured_result"]["rows"]

    second = workflow.run({**first, "user_message": "头痛可能是什么原因？"})

    assert second["intent"] == "disease"
    assert second["structured_result"] is None
    assert second["citations"]
    assert second["answer_source"] != "read-only-query"
    assert "retrieve" in second["workflow_path"]
    assert "structured_query" in second["workflow_skipped_nodes"]
    assert_progress_contract(second)


@pytest.mark.parametrize("runner", ["langgraph", "deterministic"])
def test_rag_to_sql_turn_clears_previous_citations_and_query(runner: str) -> None:
    workflow = make_workflow_for_runner(runner)
    first = workflow.run({"session_id": "rag-sql", "user_message": "我32岁，咳嗽3天", "profile": {}})
    assert first["citations"]
    assert first["rewritten_query"]

    second = workflow.run({**first, "user_message": "查布洛芬库存"})

    assert second["intent"] == "structured"
    assert second["structured_result"]["rows"]
    assert second["citations"] == []
    assert second["rewritten_query"] == ""
    assert second["answer_source"] == "read-only-query"
    assert "retrieve" in second["workflow_skipped_nodes"]
    assert_progress_contract(second)


@pytest.mark.parametrize("runner", ["langgraph", "deterministic"])
def test_rag_to_high_risk_turn_clears_all_skipped_branch_outputs(runner: str) -> None:
    workflow = make_workflow_for_runner(runner)
    first = workflow.run({"session_id": "rag-high", "user_message": "我32岁，咳嗽3天", "profile": {}})
    assert first["citations"]
    assert first["rewritten_query"]

    second = workflow.run({**first, "user_message": "突然胸痛，呼吸困难"})

    assert second["risk_level"] == "high"
    assert second["citations"] == []
    assert second["structured_result"] is None
    assert second["rewritten_query"] == ""
    assert second["answer_source"] == "safety-rules"
    assert second["workflow_skipped_nodes"] == ["retrieve", "structured_query"]
    assert_progress_contract(second)


@pytest.mark.parametrize("runner", ["langgraph", "deterministic"])
def test_cancelled_run_never_starts_a_node(runner: str, monkeypatch) -> None:
    workflow = make_workflow_for_runner(runner)
    cancelled = threading.Event()
    cancelled.set()

    def fail_if_called(_state):
        raise AssertionError("cancelled workflow must not execute normalize")

    monkeypatch.setattr(workflow, "normalize", fail_if_called)
    with pytest.raises(WorkflowCancelled):
        workflow.run({"session_id": "cancelled", "user_message": "咳嗽", "profile": {}}, cancel_event=cancelled)


@pytest.mark.parametrize("runner", ["langgraph", "deterministic"])
def test_cancel_signal_between_nodes_stops_before_next_side_effect(runner: str, monkeypatch) -> None:
    workflow = make_workflow_for_runner(runner)
    cancelled = threading.Event()
    original_normalize = workflow.normalize

    def cancel_after_normalize(state):
        state = original_normalize(state)
        cancelled.set()
        return state

    def fail_if_called(_state):
        raise AssertionError("cancelled workflow must not execute extract_profile")

    monkeypatch.setattr(workflow, "normalize", cancel_after_normalize)
    monkeypatch.setattr(workflow, "extract_profile", fail_if_called)
    with pytest.raises(WorkflowCancelled):
        workflow.run({"session_id": "cancel-between", "user_message": "咳嗽", "profile": {}}, cancel_event=cancelled)
