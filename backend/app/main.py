"""FastAPI application: GET /health, POST /chat, GET /sessions/{id}/messages."""

from __future__ import annotations

import asyncio
import logging
import math
import time
import weakref
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import Settings
from .db import Database
from .gateway import AllProvidersUnavailable, Gateway
from .schemas import ChatRequest, ChatResponse, HistoryResponse
from .simulate import parse_failure_header

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    db = Database(settings.db_path)
    gateway = Gateway(settings)

    if gateway.keys_configured < 2:
        log.warning("LLM_API_KEYS holds %d key(s); at least 2 are needed for failover", gateway.keys_configured)
    if settings.enable_failure_simulation:
        log.warning("ENABLE_FAILURE_SIMULATION is on: the X-Simulate-Failure header is honoured")

    # One lock per active session keeps two quick messages in the same chat in order.
    session_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()

    def lock_for(session_id: str) -> asyncio.Lock:
        lock = session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            session_locks[session_id] = lock
        return lock

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await gateway.warm_up()
        log.info(
            "ready: model=%s keys=%d key_ids=%s cache=%s db=%s",
            settings.model,
            gateway.keys_configured,
            ",".join(gateway.key_ids),
            gateway.cache_enabled,
            settings.db_path,
        )
        yield
        db.close()

    app = FastAPI(title="LLM Chatbot with Key Failover and Response Caching", version="1.0.0", lifespan=lifespan)
    app.state.settings, app.state.db, app.state.gateway = settings, db, gateway

    origins = settings.allowed_origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if "*" in origins else origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],  # includes X-Simulate-Failure
    )

    # Every error is JSON with a `detail` field, never a stack trace or an HTML page.
    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception):
        log.exception("unhandled error")
        return JSONResponse({"detail": "internal server error"}, status_code=500)

    # ------------------------------------------------------------------ routes
    @app.get("/health")
    async def health():
        db_ok = await asyncio.to_thread(db.ping)
        keys = gateway.keys_configured
        ok = db_ok and keys > 0
        body = {
            "status": "ok" if ok else "degraded",
            "keys_configured": keys,
            "cache_enabled": gateway.cache_enabled,
            "model": settings.model,
            "database": "ok" if db_ok else "unreachable",
            "failure_simulation": settings.enable_failure_simulation,
        }
        return JSONResponse(body, status_code=200 if ok else 503)

    @app.post("/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest, x_simulate_failure: str | None = Header(default=None)):
        started = time.perf_counter()
        simulate = (
            parse_failure_header(x_simulate_failure, gateway.key_ids)
            if settings.enable_failure_simulation
            else {}
        )

        async with lock_for(req.session_id):
            stored_prompt, history = await asyncio.to_thread(
                db.load_context, req.session_id, settings.max_history_messages
            )
            system_prompt = req.system_prompt or stored_prompt or settings.default_system_prompt
            messages = (
                [{"role": "system", "content": system_prompt}]
                + history
                + [{"role": "user", "content": req.message}]
            )

            try:
                result = await gateway.complete(messages, simulate)
            except AllProvidersUnavailable:
                # Nothing is written: a failed request never enters the history.
                raise HTTPException(status_code=503, detail="all providers unavailable")

            # Whole milliseconds, rounded up so a (sub-ms) cache hit never reads "0 ms".
            latency_ms = max(1, math.ceil((time.perf_counter() - started) * 1000))
            metadata = {
                "resolution_source": result.resolution_source,
                "model": result.model,
                "latency_ms": latency_ms,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "key_id": result.key_id,
                "fallback_attempts": result.fallback_attempts,
            }
            await asyncio.to_thread(
                db.save_exchange, req.session_id, req.message, result.reply, metadata, req.system_prompt
            )

        return ChatResponse(session_id=req.session_id, reply=result.reply, metadata=metadata)

    @app.get("/sessions/{session_id}/messages", response_model=HistoryResponse)
    async def session_messages(session_id: str):
        messages = await asyncio.to_thread(db.get_messages, session_id)
        if messages is None:
            raise HTTPException(status_code=404, detail="session not found")
        return HistoryResponse(session_id=session_id, messages=messages)

    return app


def get_app() -> FastAPI:  # `uvicorn app.main:get_app --factory`
    return create_app()
