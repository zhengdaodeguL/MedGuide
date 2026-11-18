"""Issue #7: drug questions must be answered, and structured lookups must
match registered entities (or ask for clarification) instead of running a
lookup with a garbage prefix that returns zero rows."""

import pytest

from app.retrieval import HybridRetriever, IntentRouter
from app.safety import SafetyEngine
from app.sql_guard import ReadOnlySQLGuard
from app.workflow import MedGuideWorkflow


def make_workflow() -> MedGuideWorkflow:
    return MedGuideWorkflow(HybridRetriever(), ReadOnlySQLGuard(), SafetyEngine())


# --- A: drug questions -------------------------------------------------------

def test_ibuprofen_how_to_take_returns_cited_checklist() -> None:
    result = make_workflow().run(
        {"session_id": "drug-7", "user_message": "布洛芬怎么吃？有什么注意事项？", "profile": {}}
    )
    assert result["intent"] == "drug"
    assert result["answer_source"] != "clarification-template"
    assert any(item["id"].startswith("drug-ibuprofen-001") for item in result["citations"])
    # The catalog entry carries no dosage of its own: the answer must quote
    # its checklist and defer dosage to the package insert, not invent one.
    assert "过敏史" in result["response"]
    assert "具体剂量和疗程以当前药品说明书及医生或药师建议为准" in result["response"]
    assert "缺少足够的背景信息" not in result["response"]


def test_loratadine_question_is_not_swallowed_by_safety_review() -> None:
    result = make_workflow().run(
        {"session_id": "drug-7", "user_message": "氯雷他定怎么吃，有什么注意事项", "profile": {}}
    )
    assert result["intent"] == "drug"
    assert any(item["id"].startswith("drug-loratadine-001") for item in result["citations"])
    assert "不自行停药" in result["response"]
    assert "已停止展示" not in result["response"]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("布洛芬怎么吃？有什么注意事项？", "drug"),
        ("布洛芬有什么副作用", "drug"),
        ("皮疹看什么科？", "department"),
        ("咳嗽三天应该挂什么科？", "department"),
    ],
)
def test_router_classifies_drug_questions(query: str, expected: str) -> None:
    router = IntentRouter(drug_names=HybridRetriever().drug_names)
    assert router.classify(query) == expected


def test_router_knows_catalog_drug_names_without_hardcoding_them() -> None:
    names = HybridRetriever().drug_names
    assert "布洛芬" in names
    assert "氯雷他定" in names
    # Generic effect words must not vote for the drug intent: "发烧怎么退热"
    # is a symptom question, not a drug question.
    assert "退热" not in names
    assert "止痛" not in names
    assert IntentRouter(drug_names=names).classify("氯雷他定有什么注意事项") == "drug"


# --- B: structured lookup term extraction -----------------------------------

@pytest.mark.parametrize(
    "query",
    [
        "我想查一下布洛芬的库存",
        "请帮我查布洛芬库存",
        "布洛芬还有货吗",
        "布洛芬有没有货",
        "查询布洛芬库存",
    ],
)
def test_colloquial_inventory_questions_find_ibuprofen(query: str) -> None:
    result = ReadOnlySQLGuard().from_natural_language(query)
    assert result is not None
    assert result.blocked is False
    assert result.params == ("布洛芬%",)
    assert result.rows and result.rows[0]["name"] == "布洛芬缓释胶囊"


def test_colloquial_inventory_question_finds_loratadine() -> None:
    result = ReadOnlySQLGuard().from_natural_language("氯雷他定还有货吗")
    assert result is not None
    assert result.blocked is False
    assert result.params == ("氯雷他定%",)
    assert result.rows and result.rows[0]["name"] == "氯雷他定片"


@pytest.mark.parametrize(
    ("query", "kind"),
    [
        ("阿司匹林库存", "药品"),
        ("心电图多少钱", "检查或检验"),
    ],
)
def test_unregistered_entity_is_blocked_with_clarification(query: str, kind: str) -> None:
    result = ReadOnlySQLGuard().from_natural_language(query)
    assert result is not None
    assert result.blocked is True
    assert result.rows == ()
    assert f"未能识别出已登记的{kind}名称" in (result.reason or "")


def test_bare_inventory_question_still_asks_for_a_specific_name() -> None:
    result = ReadOnlySQLGuard().from_natural_language("查库存")
    assert result is not None
    assert result.blocked is True
    assert "具体" in (result.reason or "")


def test_injection_inside_inventory_question_cannot_escape_the_allowlist() -> None:
    result = ReadOnlySQLGuard().from_natural_language(
        "布洛芬库存' UNION SELECT name, stock, unit, updated_at FROM drug_inventory --"
    )
    assert result is not None
    assert result.blocked is False
    assert "UNION" not in result.sql
    assert result.rows and result.rows[0]["name"] == "布洛芬缓释胶囊"


def test_chat_answer_distinguishes_unheard_from_out_of_stock() -> None:
    workflow = make_workflow()
    unknown = workflow.run(
        {"session_id": "drug-7", "user_message": "阿司匹林库存", "profile": {}}
    )
    assert unknown["structured_result"]["blocked"] is True
    assert "未能识别出已登记的药品名称" in unknown["response"]
    assert "暂时没有匹配记录" not in unknown["response"]

    known = workflow.run(
        {"session_id": "drug-7", "user_message": "布洛芬还有货吗", "profile": {}}
    )
    assert known["structured_result"]["blocked"] is False
    assert "布洛芬缓释胶囊" in known["response"]
    assert "128" in known["response"]
