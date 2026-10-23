from types import SimpleNamespace

import httpx
import openai

from rag_app.llm import DatabricksLLM


class _Completions:
    def __init__(self, reject_temperature: bool):
        self.reject = reject_temperature
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
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _llm(reject: bool) -> tuple[DatabricksLLM, _Completions]:
    llm = DatabricksLLM.__new__(DatabricksLLM)
    completions = _Completions(reject)
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
