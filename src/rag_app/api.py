"""FastAPI application: JSON API + static chat UI with a citation viewer.

Identity: Databricks Apps sets X-Forwarded-Email after SSO; the user id is taken only
from that header (never from the request body). Locally, LOCAL_USER_ID is used.

`create_app()` builds real dependencies from the environment; tests pass fakes instead.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from rag_app import __version__, tracing
from rag_app.agent import RagAgent, SessionNotFound
from rag_app.config import Settings
from rag_app.documents import DocumentService, annotate_page
from rag_app.models import ChatRequest, ChatResponse, CitationDetail, FeedbackRequest

log = logging.getLogger("rag_app")
STATIC = Path(__file__).resolve().parents[2] / "static"
ID_RE = re.compile(r"^[A-Za-z0-9-]{8,80}$")


def _check_id(value: str, what: str) -> str:
    if not ID_RE.match(value or ""):
        raise HTTPException(400, f"invalid {what}")
    return value


def _build_real(settings: Settings):
    from databricks.sdk import WorkspaceClient

    from rag_app.llm import DatabricksLLM
    from rag_app.retriever import VectorSearchRetriever
    from rag_app.store import build_store

    w = WorkspaceClient()
    try:
        tracing.setup(settings.mlflow_experiment_id)
    except Exception:  # observability is optional: the app still serves answers without it
        log.exception("MLflow tracing could not be enabled; continuing without traces")
    store = build_store(settings, w)
    agent = RagAgent(
        settings, store, VectorSearchRetriever(w, settings.vs_index), DatabricksLLM(w, settings.llm_endpoint)
    )
    return agent, DocumentService(w, settings)


def create_app(
    settings: Settings | None = None, agent: RagAgent | None = None, documents: DocumentService | None = None
) -> FastAPI:
    state: dict = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        s = settings or Settings.from_env()
        a, d = (agent, documents) if agent is not None else _build_real(s)
        state.update(settings=s, agent=a, documents=d)
        log.info(
            "ready: index=%s llm=%s history_turns=%s store=%s",
            s.vs_index,
            s.llm_endpoint,
            s.history_turns,
            s.store_backend,
        )
        yield

    app = FastAPI(title="RAG Knowledge Assistant", version=__version__, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        started = time.time()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("unhandled error rid=%s path=%s", rid, request.url.path)
            response = JSONResponse({"detail": "internal error", "request_id": rid}, status_code=500)
        response.headers["x-request-id"] = rid
        response.headers["x-content-type-options"] = "nosniff"
        response.headers["referrer-policy"] = "same-origin"
        response.headers["x-frame-options"] = "SAMEORIGIN"
        if request.url.path.startswith("/api/"):
            log.info(
                "%s %s %s %dms rid=%s",
                request.method,
                request.url.path,
                response.status_code,
                (time.time() - started) * 1000,
                rid,
            )
        return response

    def user_of(request: Request) -> str:
        # Set by Databricks Apps after SSO (never trusted from the request body). Service
        # principals (e.g. the deployment smoke test) may only carry X-Forwarded-User.
        user = (
            request.headers.get("x-forwarded-email")
            or request.headers.get("x-forwarded-preferred-username")
            or request.headers.get("x-forwarded-user")
        )
        if user:
            return user.strip().lower()
        if state["settings"].local_user_id:
            return state["settings"].local_user_id
        raise HTTPException(401, "no authenticated user")

    def maintain_task(user: str, session_id: str) -> None:
        """Title, rolling summary, long-term memory extraction. Failures are logged, never raised."""
        try:
            done = state["agent"].maintain(user, session_id)
            log.info("maintenance session=%s %s", session_id, done)
        except Exception:
            log.exception("background maintenance failed for session %s", session_id)

    # -- pages ------------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/health")
    def health():
        s = state["settings"]
        return {
            "status": "ok",
            "version": __version__,
            "index": s.vs_index,
            "llm": s.llm_endpoint,
            "history_turns": s.history_turns,
            "store": s.store_backend,
            "memory_schema": s.pg_schema if s.store_backend == "postgres" else None,
            "long_term_memory": s.long_term_memory,
            "tracing": tracing.enabled(),
        }

    @app.get("/api/me")
    def me(request: Request):
        return {
            "user_id": user_of(request),
            "assistant_name": state["settings"].assistant_name,
            "history_turns": state["settings"].history_turns,
            "long_term_memory": state["settings"].long_term_memory,
        }

    # -- chat -------------------------------------------------------------------
    @app.post("/api/chat", response_model=ChatResponse)
    def chat(body: ChatRequest, request: Request, background: BackgroundTasks):
        user = user_of(request)
        if body.session_id:
            _check_id(body.session_id, "session_id")
        try:
            result = state["agent"].chat(user, body.message, body.session_id, body.client_message_id)
        except SessionNotFound:
            raise HTTPException(404, "session not found") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        if not result.replayed:
            background.add_task(maintain_task, user, result.session_id)
        return result

    @app.get("/api/sessions")
    def sessions(request: Request):
        return state["agent"].store.list_sessions(user_of(request))

    @app.get("/api/sessions/{session_id}/messages")
    def session_messages(session_id: str, request: Request):
        user = user_of(request)
        _check_id(session_id, "session_id")
        if not state["agent"].store.get_session(session_id, user):
            raise HTTPException(404, "session not found")
        return state["agent"].store.messages(session_id, user)

    @app.delete("/api/sessions/{session_id}")
    def delete_session(session_id: str, request: Request):
        _check_id(session_id, "session_id")
        if not state["agent"].store.delete_session(session_id, user_of(request)):
            raise HTTPException(404, "session not found")
        return {"deleted": session_id}

    @app.post("/api/feedback")
    def feedback(body: FeedbackRequest, request: Request, background: BackgroundTasks):
        user = user_of(request)
        trace_id = state["agent"].store.add_feedback(user, body.request_id, body.rating, body.comment)
        if trace_id:  # also attach it to the answer's MLflow trace, after responding
            background.add_task(tracing.log_user_feedback, trace_id, user, body.rating, body.comment)
        return {"ok": True}

    # -- long-term memory (user-visible and user-deletable) ---------------------------------
    @app.get("/api/memories")
    def memories(request: Request):
        return state["agent"].store.list_memories(user_of(request))

    @app.delete("/api/memories/{memory_id}")
    def forget_one(memory_id: str, request: Request):
        deleted = state["agent"].store.delete_memory(user_of(request), _check_id(memory_id, "memory_id"))
        if not deleted:
            raise HTTPException(404, "memory not found")
        return {"deleted": deleted}

    @app.delete("/api/memories")
    def forget_all(request: Request):
        return {"deleted": state["agent"].store.delete_memory(user_of(request), None)}

    # -- citations & documents ----------------------------------------------------
    @app.get("/api/citations/{chunk_id}", response_model=CitationDetail)
    def citation(chunk_id: str, request: Request):
        user_of(request)
        detail = state["documents"].citation_detail(_check_id(chunk_id, "chunk_id"))
        if detail is None:
            raise HTTPException(404, "citation not found")
        return detail

    @app.get("/api/citations/{chunk_id}/pages/{page}/annotated.png")
    def annotated(chunk_id: str, page: int, request: Request):
        """The cited page with the chunk's bounding boxes drawn in (for non-browser clients)."""
        user_of(request)
        detail = state["documents"].citation_detail(_check_id(chunk_id, "chunk_id"))
        region = next((p for p in (detail.pages if detail else []) if p.page == page), None)
        if region is None:
            raise HTTPException(404, "no boxes for this page")
        image = state["documents"].page_image(detail.doc_id, page)
        if image is None:
            raise HTTPException(404, "page image not found")
        return Response(
            annotate_page(image, region.boxes),
            media_type="image/png",
            headers={"cache-control": "private, max-age=3600"},
        )

    @app.get("/api/documents/{doc_id}/pages/{page}/image")
    def page_image(doc_id: str, page: int, request: Request):
        user_of(request)
        image = state["documents"].page_image(_check_id(doc_id, "doc_id"), page)
        if image is None:
            raise HTTPException(404, "page image not found")
        return Response(image, media_type="image/jpeg", headers={"cache-control": "private, max-age=3600"})

    @app.get("/api/documents/{doc_id}/text")
    def document_text(doc_id: str, request: Request):
        user_of(request)
        doc = state["documents"].document_text(_check_id(doc_id, "doc_id"))
        if doc is None:
            raise HTTPException(404, "document not found")
        return {"doc_id": doc["doc_id"], "title": doc["title"], "path": doc["path"], "source_text": doc["source_text"]}

    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
app = create_app()
