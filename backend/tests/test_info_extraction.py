"""Issue #4: key information (fever, duration, temperature, history) must be
extracted from colloquial user messages instead of being dropped."""

from app.retrieval import HybridRetriever
from app.safety import SafetyEngine
from app.sql_guard import ReadOnlySQLGuard
from app.workflow import MedGuideWorkflow


def make_workflow() -> MedGuideWorkflow:
    return MedGuideWorkflow(HybridRetriever(), ReadOnlySQLGuard(), SafetyEngine())


def run_turn(workflow: MedGuideWorkflow, state: dict, text: str) -> dict:
    state = dict(state)
    state["user_message"] = text
    return workflow.run(state)


def test_issue4_two_turn_conversation_extracts_fever_duration_temperature_history():
    workflow = make_workflow()
    state = run_turn(
        workflow,
        {"session_id": "issue4-main", "profile": {}},
        "我最近三天发烧38.5度，还咳嗽，应该怎么办？",
    )
    profile = state["profile"]
    assert "发烧" in profile["symptoms"]
    assert "咳嗽" in profile["symptoms"]
    assert profile["duration"] == "3天"
    assert profile["temperature"] == "38.5度"
    assert "体温38.5度" in state["summary"]
    assert "持续3天" in state["summary"]

    state = run_turn(workflow, state, "今年25岁，没有慢性病，昨天开始喉咙痛")
    profile = state["profile"]
    assert profile["age"] == 25
    assert profile["history"] == "否认慢性病"
    assert "喉咙痛" in profile["symptoms"]
    # The sore throat started yesterday, but the earlier, more precise
    # 3-day duration must not be overwritten by the relative onset.
    assert profile["duration"] == "3天"
    assert "喉咙痛" in profile["associated_symptoms"]
    assert state["next_question"] != "症状从什么时候开始，持续了多久？"
    assert "持续3天" in state["summary"]
    assert "既往史：否认慢性病" in state["summary"]


def test_issue4_relative_onset_only_fills_missing_duration():
    workflow = make_workflow()
    state = run_turn(
        workflow,
        {"session_id": "issue4-onset", "profile": {}},
        "昨天开始喉咙痛",
    )
    assert state["profile"]["duration"] == "1天"
    assert "喉咙痛" in state["profile"]["symptoms"]


def test_issue4_short_colloquial_symptom_messages():
    workflow = make_workflow()
    headache = run_turn(
        workflow, {"session_id": "issue4-headache", "profile": {}}, "头疼"
    )
    assert "头疼" in headache["profile"]["symptoms"]

    tired = run_turn(
        workflow,
        {"session_id": "issue4-tired", "profile": {}},
        "最近总觉得累，睡眠也不好",
    )
    assert "累" in tired["profile"]["symptoms"]
    assert "睡眠不好" in tired["profile"]["symptoms"]


def test_issue4_colloquial_variants_and_temperature_spellings():
    workflow = make_workflow()
    state = run_turn(
        workflow,
        {"session_id": "issue4-variants", "profile": {}},
        "我发烧了，体温38.5℃，嗓子疼",
    )
    profile = state["profile"]
    assert "发烧" in profile["symptoms"]
    assert "嗓子疼" in profile["symptoms"]
    assert profile["temperature"] == "38.5度"

    state = workflow.extract_profile(
        {"normalized_message": "我有高血压", "profile": {}}
    )
    assert state["profile"]["history"] == "高血压"


def test_issue4_negated_or_third_party_symptoms_still_excluded():
    workflow = make_workflow()
    negated = run_turn(
        workflow,
        {"session_id": "issue4-negated", "profile": {}},
        "我没有发烧，也没有咳嗽",
    )
    assert negated["profile"]["symptoms"] == []

    third_party = run_turn(
        workflow,
        {"session_id": "issue4-third-party", "profile": {}},
        "我妈妈发烧咳嗽，我想问问怎么办",
    )
    assert third_party["profile"]["symptoms"] == []

    coordinated = workflow.extract_profile(
        {
            "normalized_message": "咳嗽，已经三天，没有胸痛或呼吸困难",
            "profile": {"symptoms": [], "associated_symptoms": []},
        }
    )
    assert coordinated["profile"]["symptoms"] == ["咳嗽"]

    denied_history = workflow.extract_profile(
        {"normalized_message": "否认有糖尿病", "profile": {}}
    )
    assert denied_history["profile"].get("history") != "糖尿病"


def test_issue4_duration_guards_still_hold():
    workflow = make_workflow()
    for message in ("我32岁，咳嗽不是三天", "我2026年做过体检，最近咳嗽"):
        state = workflow.extract_profile(
            {
                "normalized_message": message,
                "profile": {"symptoms": [], "associated_symptoms": []},
            }
        )
        assert "duration" not in state["profile"]


def test_issue4_temperature_in_external_summary():
    workflow = make_workflow()
    outbound = workflow._external_query_summary(
        {
            "intent": "disease",
            "profile": {
                "age": 25,
                "chief_complaint": "发烧",
                "duration": "3天",
                "temperature": "38.5度",
            },
            "risk_flags": [],
            "citations": [],
        }
    )
    assert "体温：38.5度" in outbound
    assert "持续时间：3天" in outbound
