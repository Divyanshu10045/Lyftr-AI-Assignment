"""A tiny OpenAI-compatible server used by the tests (and for local demos).

The behaviour is chosen by the API key it receives, so the real LiteLLM code
path (HTTP call -> exception mapping -> failover -> cache) is exercised:

    key contains "429"   -> HTTP 429 (rate limited)
    key contains "quota" -> HTTP 429 (quota exhausted)
    key contains "500"   -> HTTP 500
    key contains "503"   -> HTTP 503
    anything else        -> a deterministic answer
"""

from __future__ import annotations

import re
import threading
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

LATENCY_SECONDS = 0.03  # makes "cache hit faster than live" unambiguous


class FakeProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []  # API key of every request that reached the "provider"
        self.app = FastAPI()
        self.app.post("/v1/chat/completions")(self._completions)
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    @staticmethod
    def _answer(messages: list[dict]) -> str:
        system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        user_msgs = [m["content"] for m in messages if m["role"] == "user"]
        last = user_msgs[-1]
        prefix = "Arr! " if "pirate" in system.lower() else ""
        if re.search(r"what('s| is) my name", last, re.I):
            for earlier in reversed(user_msgs[:-1]):
                m = re.search(r"my name is (\w+)", earlier, re.I)
                if m:
                    return f"{prefix}Your name is {m.group(1)}."
            return f"{prefix}I don't know your name yet."
        return f"{prefix}Echo: {last}"

    async def _completions(self, request: Request):
        body = await request.json()
        key = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        self.calls.append(key)
        time.sleep(LATENCY_SECONDS)
        for needle, status in (("429", 429), ("quota", 429), ("500", 500), ("503", 503)):
            if needle in key:
                return JSONResponse({"error": {"message": f"fake {needle}", "type": "fake"}}, status_code=status)
        messages = body["messages"]
        reply = self._answer(messages)
        prompt_tokens = sum(len(m["content"].split()) for m in messages) + 3
        completion_tokens = len(reply.split())
        return {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    def start(self, port: int = 0) -> None:
        import socket

        if port == 0:
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
        self.port = port
        config = uvicorn.Config(self.app, host="127.0.0.1", port=port, log_level="error")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.time() + 10
        while not self._server.started and time.time() < deadline:
            time.sleep(0.02)

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"


if __name__ == "__main__":  # `python -m tests.fake_provider` for manual demos
    provider = FakeProvider()
    provider.start(9100)
    print(f"fake provider on {provider.base_url} (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        provider.stop()
