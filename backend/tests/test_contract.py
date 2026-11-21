"""API contract: shapes, validation, history, sessions, CORS, error format."""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

from .conftest import sid

META_FIELDS = {
    "resolution_source", "model", "latency_ms", "input_tokens",
    "output_tokens", "key_id", "fallback_attempts",
}


def chat(client, session_id, message, headers=None, **extra):
    return client.post("/chat", json={"session_id": session_id, "message": message, **extra}, headers=headers or {})


# ----------------------------------------------------------------- /health
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["keys_configured"] == 2
    assert body["cache_enabled"] is True


def test_health_degraded_without_keys(make_client):
    r = make_client(api_keys=[]).get("/health")
    assert r.status_code == 503 and r.json()["status"] == "degraded"


# ------------------------------------------------------------------- /chat
def test_chat_happy_path_matches_contract(client):
    s = sid()
    r = chat(client, s, "What is the capital of France?")
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == s
    assert body["reply"].strip()
    meta = body["metadata"]
    assert META_FIELDS <= set(meta)
    assert meta["resolution_source"] == "LIVE_INFERENCE"
    assert meta["model"] == "openai/fake-model"
    assert isinstance(meta["latency_ms"], int) and meta["latency_ms"] > 0
    assert isinstance(meta["input_tokens"], int) and meta["input_tokens"] > 0
    assert isinstance(meta["output_tokens"], int) and meta["output_tokens"] > 0
    assert meta["key_id"] == "key_1"
    assert meta["fallback_attempts"] == 0


def test_secrets_never_leave_the_server(client):
    r = chat(client, sid(), "hello")
    assert "good-1" not in r.text and "good-2" not in r.text
    assert "good-1" not in client.get("/health").text


# -------------------------------------------------------------- validation
def test_validation_errors_are_422_json_and_not_stored(client):
    s = sid()
    cases = [
        {"session_id": s},                                     # missing message
        {"message": "hi"},                                     # missing session_id
        {"session_id": s, "message": ""},                      # empty message
        {"session_id": s, "message": "   \n\t "},              # whitespace only
        {"session_id": s, "message": "x" * 4001},              # too long
        {"session_id": "", "message": "hi"},                   # empty session id
        {"session_id": "s" * 129, "message": "hi"},           # session id too long
        {"session_id": 42, "message": "hi"},                   # wrong type
    ]
    for payload in cases:
        r = client.post("/chat", json=payload)
        assert r.status_code == 422, payload
        assert "detail" in r.json(), payload
    assert client.get(f"/sessions/{s}/messages").status_code == 404


def test_malformed_json_is_422(client):
    r = client.post("/chat", content="{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 422 and "detail" in r.json()


def test_message_is_trimmed_and_length_limit_applies_after_trimming(client):
    s = sid()
    assert chat(client, s, "  " + "x" * 4000 + "  ").status_code == 200  # exactly 4000 after trim
    history = client.get(f"/sessions/{s}/messages").json()["messages"]
    assert history[0]["content"] == "x" * 4000


# --------------------------------------------------------- history/sessions
def test_history_endpoint_shape_order_and_no_system_prompt(client):
    s = sid()
    chat(client, s, "first")
    chat(client, s, "second", system_prompt="Talk like a pirate")
    r = client.get(f"/sessions/{s}/messages")
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == s
    msgs = body["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert [m["content"] for m in msgs if m["role"] == "user"] == ["first", "second"]
    assert all(m["role"] != "system" for m in msgs)
    assert "metadata" not in msgs[0]
    assert {"resolution_source", "input_tokens", "output_tokens"} <= set(msgs[1]["metadata"])


def test_unknown_session_is_404(client):
    r = client.get("/sessions/does-not-exist/messages")
    assert r.status_code == 404 and r.json() == {"detail": "session not found"}


def test_bot_remembers_earlier_messages_and_sessions_stay_separate(client):
    a, b = sid(), sid()
    chat(client, a, "my name is Ada")
    assert "Ada" in chat(client, a, "what is my name?").json()["reply"]
    assert "Ada" not in chat(client, b, "what is my name?").json()["reply"]


def test_tokens_are_per_request_not_cumulative(client):
    a, b = sid(), sid()
    first_a = chat(client, a, "alpha bravo charlie").json()["metadata"]
    first_b = chat(client, b, "delta echo foxtrot").json()["metadata"]
    assert first_a["input_tokens"] == first_b["input_tokens"]  # same length, new sessions

    second_a = chat(client, a, "golf hotel india").json()["metadata"]
    # the prompt grows with history, the completion does not accumulate
    assert second_a["input_tokens"] > first_a["input_tokens"]
    assert second_a["output_tokens"] == first_a["output_tokens"]


def test_per_session_system_prompt_overrides_and_persists(client):
    s = sid()
    assert chat(client, s, "hello", system_prompt="You are a pirate").json()["reply"].startswith("Arr!")
    # not repeated -> still applied for the rest of the session
    assert chat(client, s, "again").json()["reply"].startswith("Arr!")
    # other sessions use the default prompt
    assert not chat(client, sid(), "hello").json()["reply"].startswith("Arr!")


def test_history_survives_restart(make_client, tmp_path):
    url = f"sqlite:///{tmp_path}/persist.db"
    s = sid()
    first = make_client(database_url=url)
    chat(first, s, "remember me")
    first.__exit__(None, None, None)

    second = make_client(database_url=url)
    msgs = second.get(f"/sessions/{s}/messages").json()["messages"]
    assert [m["content"] for m in msgs if m["role"] == "user"] == ["remember me"]
    assert "Ada" in (chat(second, s, "my name is Ada").json()["reply"] + "Ada")  # still usable


def test_schema_created_on_startup_and_matches_contract(make_client, tmp_path):
    make_client(database_url=f"sqlite:///{tmp_path}/schema.db")
    cols = {row[1] for row in sqlite3.connect(tmp_path / "schema.db").execute("PRAGMA table_info(messages)")}
    assert {"id", "session_id", "role", "content", "metadata", "created_at"} <= cols


# -------------------------------------------------------------------- CORS
def test_cors_allows_frontend_origin_and_failure_header(client):
    r = client.options(
        "/chat",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-simulate-failure",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "x-simulate-failure" in r.headers["access-control-allow-headers"].lower()


def test_cors_rejects_other_origins(client):
    r = client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


# ------------------------------------------------------------------ errors
def test_unexpected_errors_are_json_without_stack_trace(make_client, monkeypatch):
    client = make_client(raise_server_exceptions=False)

    def boom(*a, **k):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(client.app.state.db, "save_exchange", boom)
    r = chat(client, sid(), "hello")
    assert r.status_code == 500
    assert r.json() == {"detail": "internal server error"}
    assert "secret internal detail" not in r.text and "Traceback" not in r.text
