"""MLflow Tracing: one trace per question, with a timed span for every step.

    chat                       (AGENT)      user, session, request_id
     ├─ short_term / long_term (MEMORY)
     ├─ condense               (CHAIN)  ─ Completions (CHAT_MODEL: prompt, answer, tokens)
     ├─ retrieve               (RETRIEVER)  passages with doc, chunk id and score
     ├─ generate               (CHAIN)  ─ Completions (CHAT_MODEL)
     └─ save_turn
    maintain                   (TASK)       background: title, summary, memory extraction

Enabled when MLFLOW_EXPERIMENT_ID is set (in Databricks Apps: the `traces` experiment
resource). Otherwise tracing is switched off and the decorators cost nothing, which is
what unit tests rely on. Traces are exported in the background (async logging), so they
never slow down an answer, and a tracing failure never fails a request.

User feedback (thumbs up/down) is attached to the trace of the answer it rates, so the
MLflow UI can filter "all answers users disliked" and show what was retrieved for them.
"""

from __future__ import annotations

import logging
import os
import time
import warnings

import mlflow
from mlflow.entities import AssessmentSource, SpanType

log = logging.getLogger(__name__)

__all__ = ["SpanType", "current_trace_id", "enabled", "log_user_feedback", "setup", "tag_trace", "trace"]

trace = mlflow.trace
start_span = mlflow.start_span

# Off until setup() is called with an experiment: no exporter, no network calls.
mlflow.tracing.disable()
_enabled = False


def enabled() -> bool:
    return _enabled


def setup(experiment_id: str) -> bool:
    """Send traces to the given MLflow experiment; returns whether tracing is on."""
    global _enabled
    if not experiment_id:
        mlflow.tracing.disable()
        _enabled = False
        return False
    os.environ.setdefault("MLFLOW_ENABLE_ASYNC_TRACE_LOGGING", "true")
    mlflow.set_tracking_uri("databricks")
    mlflow.set_experiment(experiment_id=experiment_id)
    mlflow.openai.autolog()  # every model call becomes a child span with prompt, answer and tokens
    # Claude returns [reasoning, text] content parts; recording them makes pydantic warn on every call
    warnings.filterwarnings("ignore", message="Pydantic serializer warnings", category=UserWarning)
    mlflow.tracing.enable()
    _enabled = True
    log.info("MLflow tracing enabled: experiment %s", experiment_id)
    return True


def current_trace_id() -> str | None:
    return mlflow.get_active_trace_id() if _enabled else None


def tag_trace(*, user: str, session_id: str, request_id: str | None = None, **tags) -> None:
    """Make the current trace searchable by user, conversation and our request id."""
    if not _enabled:
        return
    try:
        mlflow.update_current_trace(
            user=user,
            session_id=session_id,
            client_request_id=request_id,
            tags={k: str(v) for k, v in tags.items() if v is not None},
        )
    except Exception as exc:  # observability must never break a request
        log.warning("could not tag trace: %s", exc)


def log_user_feedback(
    trace_id: str, user: str, rating: int, comment: str | None, attempts: int = 5, base_delay: float = 1.0
) -> None:
    """Attach a thumbs up/down to the answer's trace (runs as a background task).

    Traces are exported asynchronously, so feedback given within a second or two of the
    answer can arrive before its trace: NOT_FOUND is retried with backoff (1s, 2s, 4s, ...)."""
    if not _enabled:
        return
    for attempt in range(1, attempts + 1):
        try:
            mlflow.log_feedback(
                trace_id=trace_id,
                name="user_feedback",
                value=rating > 0,
                source=AssessmentSource(source_type="HUMAN", source_id=user),
                rationale=comment or None,
            )
            return
        except Exception as exc:
            if attempt < attempts and "not found" in str(exc).lower():
                time.sleep(base_delay * 2 ** (attempt - 1))
                continue
            log.warning("could not attach feedback to trace %s: %s", trace_id, exc)
            return
