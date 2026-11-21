"""Failover, the X-Simulate-Failure hook, and response caching."""

from __future__ import annotations

import asyncio

from litellm.types.utils import ModelResponse, Usage

from app.config import Settings
from app.gateway import Gateway, _err_label

from .conftest import sid
from .test_contract import chat


def hdr(value):
    return {"X-Simulate-Failure": value}


# ------------------------------------------------------ simulated failover
def test_key_1_429_fails_over_to_key_2(client):
    r = chat(client, sid(), "q1", headers=hdr("key_1=429"))
    assert r.status_code == 200
    meta = r.json()["metadata"]
    assert meta["key_id"] == "key_2"
    assert meta["fallback_attempts"] == 1
    assert meta["resolution_source"] == "LIVE_INFERENCE"
    assert r.json()["reply"].startswith("Echo:")  # a real answer, not an error wrapped in a 200


def test_key_1_500_fails_over_to_key_2(client):
    meta = chat(client, sid(), "q2", headers=hdr("key_1=500")).json()["metadata"]
    assert meta["key_id"] == "key_2" and meta["fallback_attempts"] == 1


def test_502_and_503_also_fail_over(client):
    for status in ("502", "503"):
        meta = chat(client, sid(), f"q-{status}", headers=hdr(f"key_1={status}")).json()["metadata"]
        assert meta["key_id"] == "key_2"


def test_simulation_is_per_request_only(client):
    chat(client, sid(), "a", headers=hdr("key_1=429"))
    meta = chat(client, sid(), "b").json()["metadata"]
    assert meta["key_id"] == "key_1" and meta["fallback_attempts"] == 0


def test_all_keys_failing_gives_clean_503_and_nothing_is_stored(client):
    for value in ("*=429", "key_1=429,key_2=500", "*=503"):
        s = sid()
        r = chat(client, s, "will fail", headers=hdr(value))
        assert r.status_code == 503, value
        assert r.json() == {"detail": "all providers unavailable"}
        assert client.get(f"/sessions/{s}/messages").status_code == 404


def test_failed_request_does_not_touch_existing_history(client):
    s = sid()
    chat(client, s, "hello")
    chat(client, s, "doomed", headers=hdr("*=429"))
    msgs = client.get(f"/sessions/{s}/messages").json()["messages"]
    assert [m["content"] for m in msgs if m["role"] == "user"] == ["hello"]


def test_simulation_bypasses_cache_so_failover_is_always_visible(client):
    s1, s2 = sid(), sid()
    assert chat(client, s1, "same question").json()["metadata"]["resolution_source"] == "LIVE_INFERENCE"
    # cached now - but a simulated request must still demonstrate failover
    meta = chat(client, s2, "same question", headers=hdr("key_1=429")).json()["metadata"]
    assert meta["resolution_source"] == "LIVE_INFERENCE" and meta["key_id"] == "key_2"
    # ...and `*=429` is a 503 even for an already-cached message
    assert chat(client, sid(), "same question", headers=hdr("*=429")).status_code == 503


def test_header_ignored_when_simulation_disabled(make_client):
    client = make_client(enable_failure_simulation=False)
    meta = chat(client, sid(), "x", headers=hdr("*=429")).json()["metadata"]
    assert meta["key_id"] == "key_1" and meta["fallback_attempts"] == 0


def test_malformed_header_is_ignored(client):
    r = chat(client, sid(), "y", headers=hdr("garbage,key_9=429,key_1=418,=,key_1="))
    assert r.status_code == 200 and r.json()["metadata"]["key_id"] == "key_1"


# ------------------------------------------------------- real HTTP failures
def test_real_429_from_provider_fails_over_then_key_is_parked(make_client, provider):
    client = make_client(api_keys=["rate-429", "good-2"])
    first = chat(client, sid(), "one").json()["metadata"]
    assert (first["key_id"], first["fallback_attempts"]) == ("key_2", 1)
    assert provider.calls == ["rate-429", "good-2"]

    second = chat(client, sid(), "two").json()["metadata"]
    # key_1 is cooling down: we go straight to key_2 instead of re-hitting a dead key
    assert (second["key_id"], second["fallback_attempts"]) == ("key_2", 0)
    assert provider.calls[2:] == ["good-2"]


def test_both_configured_keys_are_loaded(client):
    gw = client.app.state.gateway
    assert gw.key_ids == ["key_1", "key_2"]
    assert client.get("/health").json()["keys_configured"] == 2


def test_gemini_3_request_omits_temperature(monkeypatch):
    captured = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        return ModelResponse(
            model=kwargs["model"],
            choices=[{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

    monkeypatch.setattr("app.gateway.litellm.acompletion", fake_acompletion)
    settings = Settings(api_keys=["good-1"], model="gemini/gemini-3.5-flash-lite", cache_enabled=False)
    result = asyncio.run(Gateway(settings).complete([{"role": "user", "content": "hi"}]))

    assert result.reply == "ok"
    assert "temperature" not in captured


def test_real_404_from_provider_fails_over_without_disabling_other_key(make_client, provider):
    client = make_client(api_keys=["not-found-404", "good-2"])
    first = chat(client, sid(), "one").json()["metadata"]
    assert (first["key_id"], first["fallback_attempts"]) == ("key_2", 1)
    assert provider.calls == ["not-found-404", "good-2"]

    second = chat(client, sid(), "two").json()["metadata"]
    assert (second["key_id"], second["fallback_attempts"]) == ("key_2", 0)
    assert provider.calls[2:] == ["good-2"]


def test_provider_error_logs_status_and_details_without_keys(make_client, caplog):
    caplog.set_level("WARNING", logger="gateway")
    client = make_client(api_keys=["not-found-404", "good-2"])
    chat(client, sid(), "one")

    text = caplog.text
    assert "key_id=key_1" in text
    assert "status=404" in text
    assert "fake 404 model not found" in text
    assert "not-found-404" not in text


def test_error_labels_redact_key_material():
    class ProviderError(Exception):
        status_code = 404
        llm_provider = "gemini"
        message = (
            "404 for https://generativelanguage.googleapis.com/v1beta/models/x:generateContent"
            "?key=AIzaSUPERSECRET123456 Authorization: Bearer sk-SUPERSECRET654321"
        )

    label = _err_label(ProviderError())
    assert "status=404" in label
    assert "provider=gemini" in label
    assert "AIzaSUPERSECRET123456" not in label
    assert "sk-SUPERSECRET654321" not in label
    assert label.count("<redacted>") == 2


def test_quota_and_5xx_errors_fail_over(make_client):
    for bad in ("quota-exhausted", "boom-500", "down-503"):
        client = make_client(api_keys=[bad, "good-2"])
        meta = chat(client, sid(), "hi").json()["metadata"]
        assert (meta["key_id"], meta["fallback_attempts"]) == ("key_2", 1), bad


def test_parked_keys_are_still_tried_as_a_last_resort(make_client):
    client = make_client(api_keys=["rate-429", "good-2"], key_cooldown_seconds=60)
    chat(client, sid(), "one")  # parks key_1
    gw = client.app.state.gateway
    gw._cooldown_until["key_2"] = 10**12  # park key_2 as well -> both cooling down
    r = chat(client, sid(), "two")
    assert r.status_code == 200 and r.json()["metadata"]["key_id"] == "key_2"


def test_every_real_key_failing_is_a_clean_503(make_client):
    client = make_client(api_keys=["rate-429", "quota-500"])
    r = chat(client, sid(), "hi")
    assert r.status_code == 503 and r.json() == {"detail": "all providers unavailable"}


def test_no_keys_configured_is_a_clean_503(make_client):
    r = chat(make_client(api_keys=[]), sid(), "hi")
    assert r.status_code == 503 and r.json() == {"detail": "all providers unavailable"}


# ------------------------------------------------------------------ caching
def test_same_first_message_in_new_session_is_a_cache_hit(client, provider):
    live = chat(client, sid(), "What is the capital of France?").json()
    provider.calls.clear()
    hit = chat(client, sid(), "What is the capital of France?").json()

    assert live["metadata"]["resolution_source"] == "LIVE_INFERENCE"
    assert hit["metadata"]["resolution_source"] == "LLM_CACHE"
    assert hit["reply"] == live["reply"]  # identical reply text
    assert hit["metadata"]["key_id"] is None
    assert hit["metadata"]["fallback_attempts"] == 0
    assert hit["metadata"]["input_tokens"] == live["metadata"]["input_tokens"]  # stored usage
    assert hit["metadata"]["output_tokens"] == live["metadata"]["output_tokens"]
    assert hit["metadata"]["latency_ms"] < live["metadata"]["latency_ms"]
    assert provider.calls == []  # the provider was not called at all


def test_immediate_repeat_is_already_a_hit(client):
    chat(client, "s-race-1", "race me")
    assert chat(client, "s-race-2", "race me").json()["metadata"]["resolution_source"] == "LLM_CACHE"


def test_different_message_is_never_served_from_cache(client):
    chat(client, sid(), "What is the capital of France?")
    other = chat(client, sid(), "What is the capital of Spain?").json()
    assert other["metadata"]["resolution_source"] == "LIVE_INFERENCE"
    assert "Spain" in other["reply"]


def test_cache_key_covers_history_and_system_prompt(client):
    # same new message, different history -> different prompt -> must be live
    a, b = sid(), sid()
    chat(client, a, "context one")
    chat(client, b, "context two")
    assert chat(client, a, "follow up").json()["metadata"]["resolution_source"] == "LIVE_INFERENCE"
    assert chat(client, b, "follow up").json()["metadata"]["resolution_source"] == "LIVE_INFERENCE"
    # different system prompt -> live
    chat(client, sid(), "hello there")
    pirate = chat(client, sid(), "hello there", system_prompt="You are a pirate").json()
    assert pirate["metadata"]["resolution_source"] == "LIVE_INFERENCE"
    assert pirate["reply"].startswith("Arr!")


def test_identical_conversations_hit_the_cache_across_sessions(client):
    a, b = sid(), sid()
    chat(client, a, "hello")
    chat(client, a, "how are you")
    chat(client, b, "hello")  # cache hit
    r = chat(client, b, "how are you").json()
    assert r["metadata"]["resolution_source"] == "LLM_CACHE"


def test_cache_can_be_disabled(make_client):
    client = make_client(cache_enabled=False)
    assert client.get("/health").json()["cache_enabled"] is False
    chat(client, sid(), "same")
    assert chat(client, sid(), "same").json()["metadata"]["resolution_source"] == "LIVE_INFERENCE"


def test_cache_hit_still_works_when_the_original_key_is_down(make_client):
    client = make_client(api_keys=["good-1", "good-2"])
    chat(client, sid(), "cached while healthy")
    gw = client.app.state.gateway
    gw._keys = [("key_1", "rate-429"), ("key_2", "quota-500")]  # every key now broken
    r = chat(client, sid(), "cached while healthy")
    assert r.status_code == 200 and r.json()["metadata"]["resolution_source"] == "LLM_CACHE"
