from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from .models import WorkflowState


@dataclass
class MetricsStore:
    requests: int = 0
    latency_total_ms: float = 0.0
    citation_covered: int = 0
    retrieval_hits: int = 0
    high_risk: int = 0
    safety_reviewed: int = 0
    blocked_queries: int = 0
    structured_successes: int = 0
    safety_blocked: int = 0
    hallucination_labeled: int = 0
    hallucinations: int = 0
    feedback: Counter[str] = field(default_factory=Counter)
    feedback_by_message: dict[str, Literal["up", "down"]] = field(default_factory=dict)
    # Latest comment text per feedback key (f"{session_id}:{message_id}").
    # Only messages that ever carried a non-blank comment get a record; the
    # "commented" counter counts those records, not submissions.
    _feedback_comments: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    _feedback_seq: int = 0
    backend: Any | None = field(default=None, repr=False, compare=False)
    _recorded_responses: set[str] = field(default_factory=set, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, state: WorkflowState) -> None:
        shared = getattr(self.backend, "record_metrics", None) if self.backend is not None else None
        if callable(shared):
            try:
                # A shared store owns deduplication and aggregation when it is
                # available; local counters remain the explicit fallback.
                result = shared(state)
                if result is not False:
                    return
            except Exception:
                # Metrics must never turn a successful medical response into a
                # 5xx.  Fall back to process-local observability if Redis is
                # temporarily unavailable.
                pass
        with self._lock:
            response_id = str(state.get("response_id") or "")
            if response_id and response_id in self._recorded_responses:
                return
            if response_id:
                self._recorded_responses.add(response_id)
            self.requests += 1
            self.latency_total_ms += float(state.get("latency_ms", 0.0))
            citations = state.get("citations") or []
            valid_citations = any(
                isinstance(citation, dict)
                and bool(citation.get("id"))
                and not citation.get("blocked", False)
                for citation in citations
            )
            structured = state.get("structured_result")
            blocked = bool(isinstance(structured, dict) and structured.get("blocked"))
            structured_success = bool(
                isinstance(structured, dict)
                and not blocked
                and structured.get("rows")
            )
            self.citation_covered += int(valid_citations or structured_success)
            self.retrieval_hits += int(valid_citations)
            self.blocked_queries += int(blocked)
            self.structured_successes += int(structured_success)
            self.high_risk += int(state.get("risk_level") == "high")
            self.safety_reviewed += 1
            self.safety_blocked += int(bool(state.get("safety_blocked")))
            label = state.get("hallucination_label")
            if isinstance(label, bool):
                self.hallucination_labeled += 1
                self.hallucinations += int(label)

    def add_feedback(
        self,
        rating: Literal["up", "down"],
        comment: str | None = None,
        message_id: str | None = None,
        *,
        session_id: str | None = None,
    ) -> bool:
        # A blank comment is no comment: it is never stored, never counted
        # and never makes a resubmission count as a change (issue #21).
        text = comment.strip() if isinstance(comment, str) else None
        text = text or None
        shared = getattr(self.backend, "upsert_feedback", None) if self.backend is not None else None
        if callable(shared) and message_id:
            try:
                result = shared(session_id or "", message_id, rating, text)
                if result is not None:
                    return bool(result)
            except Exception:
                pass
        with self._lock:
            previous: str | None = None
            feedback_key = f"{session_id}:{message_id}" if session_id and message_id else message_id
            if feedback_key:
                previous = self.feedback_by_message.get(feedback_key)
            rating_changed = previous != rating
            record = self._feedback_comments.get(feedback_key) if feedback_key else None
            # Same rating + new comment text is a real change: the stored
            # text is updated. Same rating + same (or no) comment is not.
            comment_changed = bool(text) and (record is None or record.get("comment") != text)
            if not rating_changed and not comment_changed:
                return False
            if rating_changed:
                if previous:
                    self.feedback[previous] -= 1
                    if self.feedback[previous] <= 0:
                        self.feedback.pop(previous, None)
                if feedback_key:
                    self.feedback_by_message[feedback_key] = rating
                self.feedback[rating] += 1
            if comment_changed and feedback_key:
                if record is None:
                    # "commented" counts messages that ever carried a comment.
                    self.feedback["commented"] += 1
                self._feedback_seq += 1
                self._feedback_comments[feedback_key] = {
                    "session_id": session_id,
                    "message_id": message_id,
                    "rating": rating,
                    "comment": text,
                    "seq": self._feedback_seq,
                }
            elif record is not None and rating_changed:
                # Keep the stored comment in step with the current rating.
                record["rating"] = rating
                self._feedback_seq += 1
                record["seq"] = self._feedback_seq
            return True


    def remove_feedback(
        self,
        message_id: str | None = None,
        *,
        session_id: str | None = None,
    ) -> bool:
        # Removing a vote only removes the rating itself. "commented" counts
        # messages that once carried a comment, so a revoke does not decrement
        # it; comment storage/readback belongs to issue #21, not this one.
        shared = getattr(self.backend, "remove_feedback", None) if self.backend is not None else None
        if callable(shared) and message_id:
            try:
                result = shared(session_id or "", message_id)
                if result is not None:
                    return bool(result)
            except Exception:
                pass
        with self._lock:
            feedback_key = f"{session_id}:{message_id}" if session_id and message_id else message_id
            if not feedback_key:
                return False
            previous = self.feedback_by_message.get(feedback_key)
            if not previous:
                return False
            self.feedback[previous] -= 1
            if self.feedback[previous] <= 0:
                self.feedback.pop(previous, None)
            del self.feedback_by_message[feedback_key]
            return True


    def feedback_comments(self) -> list[dict[str, Any]]:
        """Return every stored feedback comment, oldest update first.

        A shared backend owns the records when it is available; local
        records are the fallback for the same outages `add_feedback`
        already tolerates.
        """
        shared = getattr(self.backend, "list_feedback_comments", None) if self.backend is not None else None
        if callable(shared):
            try:
                result = shared()
                if isinstance(result, list):
                    return [dict(item) for item in result if isinstance(item, Mapping)]
            except Exception:
                pass
        with self._lock:
            return sorted(
                (dict(record) for record in self._feedback_comments.values()),
                key=lambda record: int(record.get("seq", 0)),
            )

    def snapshot(self) -> dict:
        shared = getattr(self.backend, "metrics_snapshot", None) if self.backend is not None else None
        if callable(shared):
            try:
                remote = shared()
                if isinstance(remote, Mapping):
                    return dict(remote)
            except Exception:
                pass
        with self._lock:
            return {
                "requests": self.requests,
                "avg_latency_ms": round(self.latency_total_ms / self.requests, 2) if self.requests else 0.0,
                "citation_coverage": round(self.citation_covered / self.requests, 3) if self.requests else 0.0,
                "retrieval_hit_rate": round(self.retrieval_hits / self.requests, 3) if self.requests else 0.0,
                "high_risk_rate": round(self.high_risk / self.requests, 3) if self.requests else 0.0,
                "blocked_queries": self.blocked_queries,
                "structured_successes": self.structured_successes,
                "safety_blocked": self.safety_blocked,
                "hallucination_rate": (
                    round(self.hallucinations / self.hallucination_labeled, 3)
                    if self.hallucination_labeled
                    else None
                ),
                "hallucination_labeled": self.hallucination_labeled,
                "hallucination_label_status": (
                    "labeled" if self.hallucination_labeled else "unlabeled; unavailable"
                ),
                "feedback": dict(self.feedback),
                "feedback_by_message": dict(self.feedback_by_message),
                "evaluation_note": "引用覆盖率仅统计有效引用或有行结构化结果；幻觉率需人工标注后计算。",
            }
