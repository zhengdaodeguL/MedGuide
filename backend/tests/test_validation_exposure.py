"""Regression tests for MedGuide issue #8 (backend validation & exposure, 6a-6f)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.auth import AuthStore, MySQLAuthStore, UnknownUserError
from app.embeddings import _normalize_embedding_base_url
from app.ingestion import MedicalDocumentCleaner
from app.llm import _normalize_base_url
from app.main import app
from app.retrieval import HybridRetriever
from app.safety import SafetyEngine
from app.sql_guard import ReadOnlySQLGuard
from app.workflow import MedGuideWorkflow
from conftest import login_client


def _workflow() -> MedGuideWorkflow:
    return MedGuideWorkflow(HybridRetriever(), ReadOnlySQLGuard(), SafetyEngine())


# 6a: age extraction has no upper bound (999 -> elderly high risk).
def test_age_above_120_is_ignored_and_not_flagged_as_elderly() -> None:
    workflow = _workflow()
    state = workflow.normalize(
        {
            "user_message": "999岁男性咳嗽三天",
            "profile": {"symptoms": [], "associated_symptoms": []},
        }
    )
    result = workflow.extract_profile(state)
    assert "age" not in result["profile"]
    assessment = SafetyEngine().screen("999岁男性咳嗽三天", result["profile"])
    assert "婴幼儿或高龄人群" not in assessment.flags


def test_plausible_age_is_still_extracted() -> None:
    workflow = _workflow()
    state = workflow.normalize(
        {
            "user_message": "今年25岁，咳嗽三天",
            "profile": {"symptoms": [], "associated_symptoms": []},
        }
    )
    result = workflow.extract_profile(state)
    assert result["profile"]["age"] == 25


# 6b: base_url normalization keeps query/fragment, accepts any scheme,
# and silently "corrects" mistyped endpoints.
@pytest.mark.parametrize(
    "normalizer",
    [_normalize_base_url, _normalize_embedding_base_url],
)
def test_base_url_normalizers_drop_query_and_fragment_and_require_http(normalizer) -> None:
    assert normalizer("https://provider.example/v1?api_key=secret#frag") == "https://provider.example/v1"
    assert normalizer("https://provider.example/v1/chat/completions") == "https://provider.example/v1"
    with pytest.raises(ValueError, match="HTTP\\(S\\) host"):
        normalizer("ftp://provider.example/v1")
    with pytest.raises(ValueError, match="mistyped completion endpoint"):
        normalizer("https://provider.example/v1/chaat/completions")


# 6c: create_session does not check that the user exists; the foreign-key
# failure is misreported as a token-allocation failure.
@pytest.fixture(params=["sqlite", "mysql-adapter-on-sqlite"])
def auth_store(request, tmp_path):
    database_path = tmp_path / "auth.sqlite3"
    kwargs = {
        "session_ttl_seconds": 300,
        "password_iterations": 100_000,
        "clock": lambda: 1_000.0,
    }
    if request.param == "sqlite":
        store = AuthStore(database_path, **kwargs)
    else:

        class SQLiteSchemaMySQLAuthStore(MySQLAuthStore):
            def _initialize(self):
                event.listen(
                    self.engine,
                    "connect",
                    lambda connection, _record: connection.execute("PRAGMA foreign_keys = ON"),
                )
                with self.engine.begin() as connection:
                    connection.exec_driver_sql(
                        "CREATE TABLE medguide_users (username TEXT PRIMARY KEY, "
                        "password_salt BLOB NOT NULL, password_hash BLOB NOT NULL, "
                        "password_iterations INTEGER NOT NULL, created_at INTEGER NOT NULL)"
                    )
                    connection.exec_driver_sql(
                        "CREATE TABLE medguide_auth_sessions (token_hash BLOB PRIMARY KEY, "
                        "username TEXT NOT NULL REFERENCES medguide_users(username), "
                        "created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL)"
                    )

        store = SQLiteSchemaMySQLAuthStore(f"sqlite:///{database_path.as_posix()}", **kwargs)
    yield SimpleNamespace(store=store)
    if isinstance(store, MySQLAuthStore):
        store.close()


def test_create_session_for_unknown_user_reports_the_user_not_a_token_failure(auth_store) -> None:
    with pytest.raises(UnknownUserError, match="user does not exist"):
        auth_store.store.create_session("ghostuser123")


def test_create_session_for_registered_user_still_works(auth_store) -> None:
    auth_store.store.register("realuser", "Correct-Horse-42")
    token, expires_at = auth_store.store.create_session("realuser")
    assert token
    assert expires_at == 1_300


# 6d: /api/query exposes SQL; sample rows are indistinguishable from live data.
def test_api_query_response_has_no_sql_and_labels_sample_data() -> None:
    with TestClient(app, base_url="https://testserver") as client:
        # Issue #6: /api/query requires a session in every mode, so this
        # 6d response-shape check must log in first (same as the other
        # API tests); the assertions below are unchanged.
        login_client(client, "validation_exposure_user")
        response = client.post("/api/query", json={"text": "查询布洛芬库存"})
    assert response.status_code == 200
    body = response.json()
    assert body["matched"] is True
    result = body["result"]
    assert "sql" not in result
    # The internal object still carries the statement for logging/debugging.
    guard = ReadOnlySQLGuard()
    internal = guard.from_natural_language("查询布洛芬库存")
    assert internal is not None and internal.sql.startswith("SELECT")
    assert result["sample_data"] is True
    assert result["rows"] and result["rows"][0]["name"] == "布洛芬缓释胶囊"


# 6e: nested-credential check bypassed via `#` fragments and nested userinfo.
@pytest.mark.parametrize(
    "query",
    [
        "redirect=https://evil/x%23token=secret",
        "redirect=https://user:secretpass@evil.example/",
    ],
)
def test_source_url_rejects_fragment_and_userinfo_nested_credentials(query: str) -> None:
    with pytest.raises(ValueError, match="sensitive query parameter"):
        MedicalDocumentCleaner.sanitize_source_url(f"https://www.nhs.uk/reference?{query}")


def test_source_url_still_allows_plain_nested_urls_without_credentials() -> None:
    value = "https://www.nhs.uk/reference?redirect=https://example.org/guide?view=full"
    assert MedicalDocumentCleaner.sanitize_source_url(value) == value


# 6f: test-mode local vector retrieval is dead code; everything is BM25.
def test_local_vector_search_fuses_vectors_in_test_mode() -> None:
    retriever = HybridRetriever()
    assert len(retriever._local_vectors) == len(retriever.chunks) > 0
    labels = set()
    for query in ("发烧咳嗽怎么办", "头痛的原因", "感冒吃什么药"):
        for citation in retriever.search(query, "disease", top_k=4):
            labels.add(citation.retrieval)
    assert labels - {"bm25", "hybrid", "vector"} == set()
    assert labels & {"hybrid", "vector"}, f"expected vector fusion, got only {labels}"


def test_local_vector_weight_does_not_outrank_bm25() -> None:
    # The local vectors are 64-dim token hashes: with the remote fusion
    # weights (0.72 vector / 0.20 lexical) a chest X-ray doc outranked the
    # blood-test doc for a 血常规 query. Local vectors must stay a
    # secondary cue behind BM25.
    results = HybridRetriever().search("血常规检查前需要注意什么？", "exam", top_k=4)
    assert results
    assert results[0].id.startswith("exam-cbc-001")
    assert results[0].retrieval == "hybrid"
