from types import SimpleNamespace

import httpx
import openai

from rag_app.llm import DatabricksLLM


class _Completions:
    def __init__(self, reject_temperature: bool, finish_reason: str = "stop"):
        self.reject = reject_temperature
        self.finish_reason = finish_reason
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.reject and "temperature" in kwargs:
            req = httpx.Request("POST", "https://x/serving-endpoints/chat/completions")
            raise openai.BadRequestError(
                "Model does not support the temperature parameter.",
                response=httpx.Response(400, request=req),
                body=None,
            )
        msg = SimpleNamespace(content=[{"type": "reasoning", "text": "hmm"}, {"type": "text", "text": " OK "}])
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=self.finish_reason)])


def _llm(reject: bool, finish_reason: str = "stop") -> tuple[DatabricksLLM, _Completions]:
    llm = DatabricksLLM.__new__(DatabricksLLM)
    completions = _Completions(reject, finish_reason)
    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    llm.default_endpoint, llm.timeout_s = "m", 5
    llm._no_temperature = set()
    import threading

    llm._lock = threading.Lock()
    return llm, completions


def test_temperature_rejection_is_retried_and_remembered():
    llm, calls = _llm(reject=True)
    assert llm.complete([{"role": "user", "content": "hi"}]) == "OK"  # text parts joined, reasoning dropped
    assert "temperature" in calls.calls[0] and "temperature" not in calls.calls[1]
    llm.complete([{"role": "user", "content": "again"}])
    assert "temperature" not in calls.calls[2]  # remembered: no second failing call
    assert len(calls.calls) == 3


def test_temperature_sent_when_supported():
    llm, calls = _llm(reject=False)
    llm.complete([{"role": "user", "content": "hi"}], temperature=0.0)
    assert calls.calls[0]["temperature"] == 0.0


def test_truncated_output_rejected_when_completeness_required():
    """Reasoning tokens count against max_tokens; a cut-off summary must never be stored."""
    import pytest

    from rag_app.llm import TruncatedOutput

    llm, _ = _llm(reject=False, finish_reason="length")
    with pytest.raises(TruncatedOutput):
        llm.complete([{"role": "user", "content": "summarize"}], require_complete=True)
    assert llm.complete([{"role": "user", "content": "answer"}]) == "OK"  # answers: returned, logged as warning


def test_token_usage_is_reported_for_the_trace_span():
    llm, completions = _llm(reject=False)
    original = completions.create

    def with_usage(**kwargs):
        resp = original(**kwargs)
        resp.usage = SimpleNamespace(prompt_tokens=120, completion_tokens=30, total_tokens=150)
        return resp

    completions.create = with_usage
    text, usage = llm._complete("m", [{"role": "user", "content": "hi"}], 100, 0.1, False)
    assert text == "OK" and usage == {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150}
    completions.create = original  # endpoints that report no usage
    assert llm._complete("m", [], 100, 0.1, False) == ("OK", None)
