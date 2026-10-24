import pytest
from databricks.sdk.errors import InternalError, PermissionDenied, TooManyRequests

from rag_app import retry
from rag_app.retry import is_transient, with_retry


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(retry.time, "sleep", lambda s: None)


def _flaky(errors, result="ok"):
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if errors:
            raise errors.pop(0)
        return result

    return fn, calls


def test_transient_error_is_retried_then_succeeds():
    # the error seen in production: "InternalError: Something went wrong, please try again later"
    fn, calls = _flaky([InternalError("Something went wrong, please try again later")])
    assert with_retry(fn, "vector search") == "ok" and calls["n"] == 2


def test_throttling_is_retried():
    fn, calls = _flaky([TooManyRequests("slow down"), TooManyRequests("slow down")])
    assert with_retry(fn, "x") == "ok" and calls["n"] == 3


def test_permanent_error_is_not_retried():
    fn, calls = _flaky([PermissionDenied("no access")])
    with pytest.raises(PermissionDenied):
        with_retry(fn, "x")
    assert calls["n"] == 1


def test_gives_up_after_attempts():
    fn, calls = _flaky([InternalError("a"), InternalError("b"), InternalError("c")])
    with pytest.raises(InternalError):
        with_retry(fn, "x", attempts=3)
    assert calls["n"] == 3


def test_is_transient():
    assert is_transient(InternalError("x")) and not is_transient(ValueError("x"))
