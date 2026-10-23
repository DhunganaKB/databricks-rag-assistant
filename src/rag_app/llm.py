"""Databricks Foundation Model endpoints through the OpenAI-compatible API."""

from __future__ import annotations

import logging
import threading
from typing import Protocol

log = logging.getLogger(__name__)


class ChatModel(Protocol):
    def complete(
        self,
        messages: list[dict],
        *,
        max_tokens: int = 800,
        temperature: float | None = 0.1,
        endpoint: str | None = None,
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
    """Some endpoints (e.g. newer Claude models) reject sampling parameters such as
    `temperature`. The first rejection is remembered per endpoint and the call is retried
    without it, so the same code works with any Foundation Model endpoint."""

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
    ) -> str:
        from openai import BadRequestError

        model = endpoint or self.default_endpoint
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
        content = resp.choices[0].message.content
        if isinstance(content, list):  # some models return typed content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
        return (content or "").strip()
