"""Where the local window's size comes from, and what happens when nobody knows.

A subagent runs in its own process. That process never launched the local
server, so it holds no launched value and asks the server instead — and
llama-cpp-python's `/v1/models` carries no `meta.n_ctx_train`, so the probe reads
nothing. The old code took that silence as 4096 and said nothing, which turned a
running 65k model into a 4k one for every worker: tasks failed as
`LocalContextTooLargeError ... 17968 chars > 4416 window (ctx_len=4096)` against
a window that did not exist, while the owner's own status page read 65536.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def manager():
    from ouroboros.local_model import LocalModelManager

    mgr = LocalModelManager.__new__(LocalModelManager)
    mgr._context_length = 0
    mgr._port = 8766
    return mgr


def test_the_launched_value_wins_over_everything(manager, monkeypatch):
    """This process started the server, so it KNOWS. No probe, no setting."""
    manager._context_length = 32768
    monkeypatch.setattr(type(manager), "health_check",
                        lambda self: pytest.fail("must not probe when the value is known"))
    assert manager.get_context_length() == 32768


def test_a_server_that_reports_a_window_is_believed(manager, monkeypatch):
    monkeypatch.setattr(type(manager), "health_check",
                        lambda self: {"ok": True, "context_length": 16384})
    assert manager.get_context_length() == 16384


def test_a_silent_server_falls_back_to_what_the_owner_declared(manager, monkeypatch):
    """The heart of it. llama-cpp-python reports nothing, and the launcher passed
    the owner's setting as `--n_ctx` — so that setting IS the running window."""
    monkeypatch.setattr(type(manager), "health_check", lambda self: {"ok": True, "context_length": 0})
    monkeypatch.setattr(type(manager), "_owner_declared_context_length", staticmethod(lambda: 65536))
    assert manager.get_context_length() == 65536


def test_an_unreachable_server_still_uses_the_declared_value(manager, monkeypatch):
    monkeypatch.setattr(type(manager), "health_check",
                        lambda self: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(type(manager), "_owner_declared_context_length", staticmethod(lambda: 65536))
    assert manager.get_context_length() == 65536


def test_the_last_resort_applies_only_when_every_source_is_silent(manager, monkeypatch):
    from ouroboros.local_model import _CONTEXT_LENGTH_LAST_RESORT

    monkeypatch.setattr(type(manager), "health_check", lambda self: {"ok": True, "context_length": 0})
    monkeypatch.setattr(type(manager), "_owner_declared_context_length", staticmethod(lambda: 0))
    assert manager.get_context_length() == _CONTEXT_LENGTH_LAST_RESORT


def test_the_last_resort_is_disclosed_rather_than_silent(manager, monkeypatch, caplog):
    """A window that small refuses ordinary work. If it is a guess, say so —
    the previous silence is exactly what made this cost three debugging rounds."""
    monkeypatch.setattr(type(manager), "health_check", lambda self: {"ok": True, "context_length": 0})
    monkeypatch.setattr(type(manager), "_owner_declared_context_length", staticmethod(lambda: 0))
    with caplog.at_level("WARNING"):
        manager.get_context_length()
    assert any("unknown from every source" in r.message for r in caplog.records)


def test_llama_cpp_really_does_omit_the_field_the_probe_reads(manager, monkeypatch):
    """The premise: `/v1/models` from llama-cpp-python has no `meta`. If a future
    version adds it, the probe starts answering and this fallback stops mattering."""
    payload = {"data": [{"id": "m", "object": "model", "owned_by": "me", "permissions": []}]}

    class _Resp:
        @staticmethod
        def raise_for_status() -> None: ...
        @staticmethod
        def json(): return payload

    class _Session:
        trust_env = True
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **k): return _Resp()

    import requests
    monkeypatch.setattr(requests, "Session", _Session)
    assert manager.health_check()["context_length"] == 0
