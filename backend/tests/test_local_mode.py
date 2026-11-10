from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.bm25 import BM25Index
from app.embeddings import keyword_embedding_provider_from_env
from app.ingestion import MedicalDocumentCleaner, load_catalog
from app.llm import OpenAIAnswerer, _normalize_local_base_url
from app.main import AppServices
from app.workflow import WorkflowExecutionError


@pytest.mark.parametrize("endpoint", [
    "https://api.example.com/v1", "http://192.168.1.2:8080/v1", "http://medguide-llm.example.com/v1",
    "http://user:password@medguide-llm:8080/v1", "http://medguide-llm/v1?token=value",
    "http://medguide-llm/v1#fragment", "ftp://medguide-llm/v1", "http://medguide-llm/chat/completions",
    "http://medguide-llm", "http://medguide-llm:0/v1", "http://medguide-llm:65536/v1",
    "http://medguide-llm/v1?", "http://medguide-\tllm/v1", "http://[::1%25eth0]/v1",
])
def test_local_endpoint_rejects_external_or_ambiguous_destinations(endpoint: str) -> None:
    with pytest.raises(ValueError, match="local HTTP"):
        _normalize_local_base_url(endpoint)


@pytest.mark.parametrize("endpoint", [
    "http://medguide-llm:8080/v1", "http://localhost:8080/v1", "http://127.0.0.1:8080/v1",
    "https://[::1]:8080/v1", "http://127.0.0.2/v1",
])
def test_local_endpoint_accepts_only_explicit_service_or_loopback(endpoint: str) -> None:
    assert _normalize_local_base_url(endpoint + "/") == endpoint


@pytest.fixture
def local_environment(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("MEDGUIDE_MODE", "local")
    monkeypatch.setenv("MEDGUIDE_LOCAL_LLM_BASE_URL", "http://medguide-llm:8080/v1")
    monkeypatch.setenv("MEDGUIDE_LOCAL_LLM_MODEL", "medguide-local-qwen3-1.7b")
    monkeypatch.setenv("MEDGUIDE_AUTH_DB_PATH", str(tmp_path / "auth.sqlite3"))
    monkeypatch.setenv("MEDGUIDE_COOKIE_SECURE", "true")
    monkeypatch.setenv("MEDGUIDE_REQUIRE_HTTPS", "true")
    monkeypatch.delenv("MEDGUIDE_KNOWLEDGE_BACKEND", raising=False)
    monkeypatch.delenv("MEDGUIDE_LOCAL_LLM_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("MEDGUIDE_LOCAL_LLM_MAX_TOKENS", raising=False)
    monkeypatch.delenv("MEDGUIDE_API_TRUSTED_PROXY_CIDR", raising=False)
    monkeypatch.delenv("MEDGUIDE_TRUSTED_PROXY_CIDR", raising=False)
    path = tmp_path / "bm25-index.json"
    catalog = Path(__file__).resolve().parents[2] / "data" / "knowledge" / "catalog.json"
    chunks = MedicalDocumentCleaner().ingest(load_catalog(catalog))
    BM25Index(chunks, keyword_embedding_provider_from_env().spec).save(path)
    monkeypatch.setenv("BM25_INDEX_PATH", str(path))
    return path


def test_local_sdk_isolates_credentials_proxies_redirects_and_request_settings(
    local_environment, monkeypatch,
) -> None:
    for name, value in {
        "OPENAI_API_KEY": "external-synthetic-key", "OPENAI_MODEL": "external-model",
        "OPENAI_CHAT_BASE_URL": "https://external.example/v1", "OPENAI_BASE_URL": "https://other.example/v1",
        "OPENAI_ORG_ID": "external-org", "OPENAI_PROJECT_ID": "external-project",
        "OPENAI_WEBHOOK_SECRET": "external-webhook", "OPENAI_TIMEOUT_SECONDS": "1", "OPENAI_MAX_TOKENS": "8192",
        "HTTP_PROXY": "http://external.example:8080", "HTTPS_PROXY": "http://external.example:8080",
        "ALL_PROXY": "http://external.example:8080",
    }.items():
        monkeypatch.setenv(name, value)
    requests = []
    options = []
    real_client = httpx.Client

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={
            "id": "local-test", "object": "chat.completion", "created": 1,
            "model": "medguide-local-qwen3-1.7b",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "本地资料整理结果"}}],
        })

    class LocalHTTPClient(real_client):
        def __init__(self, **kwargs):
            options.append(kwargs)
            super().__init__(**kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(httpx, "Client", LocalHTTPClient)
    answerer = OpenAIAnswerer(api_key="ignored-explicit-key", model="ignored-model", timeout=1, base_url="https://ignored.example/v1")
    try:
        assert answerer.required and answerer.local
        assert answerer.model == "medguide-local-qwen3-1.7b"
        assert answerer.timeout == 120 and answerer.max_tokens == 512
        assert options == [{"trust_env": False, "follow_redirects": False, "timeout": 120.0}]
        assert answerer.ensure_ready()
        assert answerer.ensure_ready()
        assert len(requests) == 1
        assert answerer.generate("咳嗽", "low", "相关资料") == "本地资料整理结果"
        assert len(requests) == 2
        for request in requests:
            assert str(request.url) == "http://medguide-llm:8080/v1/chat/completions"
            assert "authorization" not in request.headers
            assert request.headers.get("openai-organization") != "external-org"
            assert request.headers.get("openai-project") != "external-project"
            body = json.loads(request.content)
            assert body["model"] == "medguide-local-qwen3-1.7b"
            assert body["chat_template_kwargs"] == {"enable_thinking": False}
        assert json.loads(requests[0].content)["max_tokens"] == 16
        assert json.loads(requests[1].content)["max_tokens"] == 512
    finally:
        answerer.close()


@pytest.mark.parametrize("missing", ["MEDGUIDE_LOCAL_LLM_MODEL", "MEDGUIDE_LOCAL_LLM_BASE_URL"])
def test_local_missing_configuration_never_falls_back_to_openai(local_environment, monkeypatch, missing) -> None:
    monkeypatch.delenv(missing)
    monkeypatch.setenv("OPENAI_API_KEY", "external-synthetic-key")
    monkeypatch.setenv("OPENAI_MODEL", "external-model")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://external.example/v1")

    def forbidden(_self):
        raise AssertionError("an incomplete local deployment must not construct any provider client")

    monkeypatch.setattr(OpenAIAnswerer, "_create_resources", forbidden)
    answerer = OpenAIAnswerer()
    assert answerer.required and not answerer.available
    assert not answerer.ensure_ready()
    assert answerer.last_error in {"MissingModelConfiguration", "MissingLocalEndpointConfiguration"}


@pytest.mark.parametrize("value,expected", [("nan", 120), ("inf", 120), ("bad", 120), ("0", 1), ("999", 180), ("60", 60)])
def test_local_timeout_is_finite_and_bounded(monkeypatch, value, expected) -> None:
    monkeypatch.setenv("MEDGUIDE_LOCAL_LLM_TIMEOUT_SECONDS", value)
    assert OpenAIAnswerer._read_local_timeout() == expected


@pytest.mark.parametrize("value,expected", [("bad", 512), ("0", 64), ("9999", 1024), ("512", 512)])
def test_local_response_budget_is_bounded(monkeypatch, value, expected) -> None:
    monkeypatch.setenv("MEDGUIDE_LOCAL_LLM_MAX_TOKENS", value)
    assert OpenAIAnswerer._read_local_max_tokens() == expected


@pytest.fixture
def local_services(local_environment, monkeypatch):
    class Answerer:
        local = True
        required = True
        available = True
        model = "medguide-local-qwen3-1.7b"
        calls = 0
        text: str | None = "根据咳嗽相关资料，可先记录症状变化；持续或加重时及时就医。"

        def ensure_ready(self):
            return self.available

        def generate(self, _query, _risk, _context):
            self.calls += 1
            return self.text

        def close(self):
            pass

    answerer = Answerer()
    monkeypatch.setattr(main_module, "OpenAIAnswerer", lambda: answerer)
    current = AppServices()
    try:
        yield current
    finally:
        current.close()


def test_local_uses_no_external_adapters_or_seed_business_records(local_environment, monkeypatch) -> None:
    for name in ("MYSQL_DSN", "MILVUS_URI", "REDIS_URL", "MEDGUIDE_AUTH_DSN"):
        monkeypatch.setenv(name, "https://external.example/unused")
    monkeypatch.setenv("MEDGUIDE_AUTH_BACKEND", "mysql")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("local mode must not construct external adapters")

    monkeypatch.setattr(main_module, "embedding_provider_from_env", forbidden)
    monkeypatch.setattr(main_module.MilvusAdapter, "_create_client", forbidden)
    monkeypatch.setattr(main_module.RedisSessionAdapter, "_create_client", forbidden)
    monkeypatch.setattr(main_module.MySQLReadOnlyAdapter, "_create_engine", forbidden)
    monkeypatch.delenv("MEDGUIDE_LOCAL_LLM_BASE_URL")
    current = AppServices()
    try:
        assert current.local and not current.production
        assert current.knowledge_backend == "bm25"
        assert current.auth.backend_name == "sqlite" and current.auth.cookie_secure
        assert current.store.backend is None and current.metrics.backend is None
        assert not current.vector_search_enabled and not current.structured_data_enabled
        assert not current.milvus.configured and not current.redis.configured and not current.mysql.configured
        assert current.sql_guard.backend is current.mysql
        for query in ("查布洛芬库存", "呼吸内科排班", "血常规价格", "库存"):
            result = current.sql_guard.from_natural_language(query)
            assert result.blocked and not result.rows
            assert "尚未连接医院业务数据" in result.reason
        direct = current.sql_guard.execute("drug_inventory", "SELECT name, stock FROM drug_inventory LIMIT 5")
        assert direct.blocked and not direct.rows
        assert "尚未连接医院业务数据" in direct.reason
    finally:
        current.close()


@pytest.mark.parametrize("backend", ["memory", "milvus", "disabled"])
def test_local_rejects_non_bm25_knowledge_backend(local_environment, monkeypatch, backend) -> None:
    monkeypatch.setenv("MEDGUIDE_KNOWLEDGE_BACKEND", backend)
    with pytest.raises(ValueError, match="requires the bm25"):
        AppServices()


def test_local_ready_requires_model_and_index_and_reports_real_state(local_services, local_environment) -> None:
    current = local_services
    assert current.readiness_missing() == ()
    payload = main_module._readiness_payload(current, True)
    assert payload["ready"] and payload["environment"] == "local"
    assert payload["generator"] == "local:medguide-local-qwen3-1.7b"
    assert payload["knowledge"]["keyword_store"] == "bm25"
    assert payload["database"] == "not-configured" and payload["cache"] == "in-memory-local"
    current.answerer.available = False
    current._readiness_cache_until = 0
    assert current.readiness_missing() == ("本地生成服务",)
    assert main_module._readiness_payload(current, False)["generator"] == "local-unavailable"
    current.answerer.available = True
    local_environment.unlink()
    current._readiness_cache_until = 0
    assert any("BM25 index" in reason for reason in current.readiness_missing())


def test_local_readiness_bounds_and_coalesces_model_probe() -> None:
    release = threading.Event()
    calls = []

    def probe():
        calls.append(True)
        release.wait(5)
        return True

    current = AppServices.__new__(AppServices)
    current.local, current.production = True, False
    current.knowledge_backend = "bm25"
    current.retriever = SimpleNamespace(keyword_ready=True)
    current.answerer = SimpleNamespace(ensure_ready=probe)
    current._readiness_lock = threading.Lock()
    current._readiness_cache_until = 0
    current._readiness_cache_value = ()
    current._readiness_cache_seconds = 5
    current._readiness_timeout_seconds = 0.03
    try:
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: current.readiness_missing(), range(8)))
        assert time.monotonic() - started < 1
        assert results == [("Dependency readiness timed out",)] * 8
        assert calls == [True]
        future = current._readiness_future
        assert future is not None
        release.set()
        assert future.result(timeout=1) == ()
        assert current.readiness_missing() == ()
        assert calls == [True]
    finally:
        release.set()


def test_local_generated_answer_keeps_citations_safety_and_runtime_identity(local_services) -> None:
    current = local_services
    state = current.workflow.run({"session_id": "local", "mode": "local", "user_message": "我32岁，咳嗽三天", "profile": {}})
    assert state["answer_source"] == "local-grounded" and state["citations"]
    assert not state["safety_blocked"]
    public = current.store.public(state)
    assert public["environment"] == "local" and public["network_enabled"]
    assert public["answer_source"] == "local-grounded"
    current.answerer.text = "你已经确诊肺炎，请立即停药。"
    reviewed = current.workflow.run({"session_id": "unsafe", "user_message": "咳嗽三天", "profile": {}})
    assert reviewed["safety_blocked"]
    assert reviewed["answer_source"] == "safety-review"
    assert reviewed["response"] == current.workflow.SAFE_REVIEW_FALLBACK
    current.answerer.text = "可记录咳嗽变化，持续或加重时及时就医。"
    reviewed["user_message"] = "咳嗽还没好"
    next_turn = current.workflow.run(reviewed)
    assert next_turn["answer_source"] == "local-grounded"
    assert next_turn["safety_blocked"] is False
    assert "safety_block_reason" not in next_turn
    before = current.answerer.calls
    urgent = current.workflow.run({"session_id": "urgent", "user_message": "突然胸痛并呼吸困难", "profile": {}})
    assert urgent["answer_source"] == "safety-rules" and urgent["risk_level"] == "high"
    assert current.answerer.calls == before
    business = current.workflow.run({"session_id": "business", "user_message": "查布洛芬库存", "profile": {}})
    assert "尚未连接医院业务数据" in business["response"]
    assert business["structured_result"]["rows"] == []


@pytest.mark.parametrize("runner", ["langgraph", "deterministic"])
def test_local_required_generation_failure_never_uses_a_template(local_services, runner) -> None:
    if runner == "deterministic":
        local_services.workflow.graph = None
    local_services.answerer.text = None
    with pytest.raises(WorkflowExecutionError) as caught:
        local_services.workflow.run({"session_id": "failure", "user_message": "咳嗽三天", "profile": {}})
    assert "本地生成服务暂时不可用" in str(caught.value.__cause__ or caught.value)


def test_local_api_requires_secure_login_and_preserves_ownership_and_failure_wording(local_services, monkeypatch) -> None:
    current = local_services
    monkeypatch.setattr(main_module, "get_services", lambda: current)
    current.auth.register("local_user", "Correct-Horse-42")
    token, _ = current.auth.create_session("local_user")
    current.auth.register("other_user", "Correct-Horse-42")
    other_token, _ = current.auth.create_session("other_user")
    client = TestClient(main_module.app, base_url="https://testserver")
    try:
        assert client.get("/api/ready").status_code == 200
        assert client.post("/api/sessions").status_code == 401
        client.cookies.set(current.auth.cookie_name, token)
        created = client.post("/api/sessions")
        assert created.status_code == 200
        session_id = created.json()["session_id"]
        client.cookies.set(current.auth.cookie_name, other_token)
        assert client.get(f"/api/sessions/{session_id}").status_code == 404
        client.cookies.set(current.auth.cookie_name, token)
        current.answerer.text = None
        failed = client.post("/api/chat", json={"session_id": session_id, "message": "咳嗽三天"})
        assert failed.status_code == 503
        assert failed.json()["detail"] == "本地生成服务暂时不可用，请稍后重试"
        streamed = client.post("/api/chat?stream=true", json={"session_id": session_id, "message": "咳嗽三天"})
        assert "event: error" in streamed.text and "本地生成服务暂时不可用" in streamed.text
        assert "外部生成服务" not in streamed.text and "event: final" not in streamed.text
    finally:
        client.close()
    insecure = TestClient(main_module.app, base_url="http://testserver")
    try:
        insecure.cookies.set(current.auth.cookie_name, token)
        assert insecure.post("/api/sessions").status_code == 426
    finally:
        insecure.close()
