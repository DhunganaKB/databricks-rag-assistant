"""Databricks Foundation Model endpoints through the OpenAI-compatible API."""

from __future__ import annotations

import logging
import threading
from typing import Protocol

from rag_app import tracing

log = logging.getLogger(__name__)


class TruncatedOutput(RuntimeError):
    """The model stopped at max_tokens (finish_reason=length): the text is incomplete."""


class ChatModel(Protocol):
    def complete(
        self,
        messages: list[dict],
        *,
        max_tokens: int = 800,
        temperature: float | None = 0.1,
        endpoint: str | None = None,
        require_complete: bool = False,
    ) -> str: ...


class _DatabricksAuth:
    """httpx auth hook: adds fresh Databricks credentials to every request. Works with any
    auth the SDK supports (PAT locally, OAuth service principal in Databricks Apps)."""

    def __init__(self, workspace_client):
        self.w = workspace_client

    def __call__(self, request):
        request.headers.update(self.w.config.authenticate())
        return request


def _openai_client(workspace_client):
    """A plain OpenAI client pointed at the workspace's serving endpoints.

    Equivalent to the SDK's deprecated get_open_ai_client(), without the databricks-openai
    package (which pulls in the full MLflow stack: ~140 packages, 600+ MB)."""
    import httpx
    from openai import OpenAI

    host = workspace_client.config.host.rstrip("/")
    return OpenAI(
        base_url=f"{host}/serving-endpoints",
        api_key="unused",  # replaced by the auth hook on every request
        http_client=httpx.Client(auth=_DatabricksAuth(workspace_client), timeout=120),
    )


class DatabricksLLM:
    """Works with any Foundation Model endpoint:

    - Some endpoints (e.g. newer Claude models) reject `temperature`. The first rejection is
      remembered per endpoint and the call is retried without it.
    - Some models return a `reasoning` part before the text, and reasoning tokens count
      against max_tokens. Only text parts are returned; if the model still stops at
      max_tokens, `require_complete=True` raises TruncatedOutput instead of returning a
      cut-off text (used for data that gets stored, such as summaries and memories)."""

    def __init__(self, workspace_client, default_endpoint: str, timeout_s: float = 60.0):
        self._client = _openai_client(workspace_client)
        self.default_endpoint = default_endpoint
        self.timeout_s = timeout_s
        self._no_temperature: set[str] = set()
        self._lock = threading.Lock()

    def complete(
        self,
        messages: list[dict],
        *,
        max_tokens: int = 800,
        temperature: float | None = 0.1,
        endpoint: str | None = None,
        require_complete: bool = False,
    ) -> str:
        model = endpoint or self.default_endpoint
        # one CHAT_MODEL span per call: prompt, answer and token usage in the MLflow trace
        with tracing.start_span(name=model, span_type=tracing.SpanType.CHAT_MODEL) as span:
            span.set_inputs({"messages": messages, "max_tokens": max_tokens})
            text, usage = self._complete(model, messages, max_tokens, temperature, require_complete)
            span.set_outputs(text)
            if usage:
                span.set_attribute(tracing.CHAT_USAGE, usage)
            return text

    def _complete(
        self, model: str, messages: list[dict], max_tokens: int, temperature: float | None, require_complete: bool
    ) -> tuple[str, dict | None]:
        from openai import BadRequestError

        kwargs = {"model": model, "messages": messages, "max_tokens": max_tokens, "timeout": self.timeout_s}
        if temperature is not None and model not in self._no_temperature:
            kwargs["temperature"] = temperature
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except BadRequestError as exc:
            if "temperature" not in kwargs or "temperature" not in str(exc).lower():
                raise
            with self._lock:
                self._no_temperature.add(model)
            log.info("endpoint %s does not accept temperature; retrying without it", model)
            kwargs.pop("temperature")
            resp = self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        content = choice.message.content
        if isinstance(content, list):  # typed content parts, e.g. [reasoning, text]
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
        text = (content or "").strip()
        if choice.finish_reason == "length":
            if require_complete:
                raise TruncatedOutput(f"{model} stopped at max_tokens={max_tokens}")
            log.warning("%s stopped at max_tokens=%s; answer may be incomplete", model, max_tokens)
        u = getattr(resp, "usage", None)  # not every endpoint reports usage
        usage = (
            {"input_tokens": u.prompt_tokens, "output_tokens": u.completion_tokens, "total_tokens": u.total_tokens}
            if u
            else None
        )
        return text, usage
