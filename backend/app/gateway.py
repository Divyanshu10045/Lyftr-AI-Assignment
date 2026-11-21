"""LiteLLM gateway: key pool, automatic failover and response caching.

LiteLLM provides the provider abstraction, the uniform exception mapping
(429 -> RateLimitError, 5xx -> InternalServerError / ServiceUnavailableError ...)
and the response cache. The loop in `Gateway.complete` adds what a plain router
cannot give us: deterministic key priority, per-request attribution
(`key_id`, `fallback_attempts`) and a per-request failure-simulation hook.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import litellm
from litellm.types.utils import ModelResponse, Usage

from .config import Settings
from .simulate import build_error

log = logging.getLogger("gateway")

LIVE = "LIVE_INFERENCE"
CACHE = "LLM_CACHE"

_SECRET_QUERY_RE = re.compile(r"(?i)((?:api[_-]?key|key|token|access_token)=)[^&\s\"']+")
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+")
_GEMINI_KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]{10,}")
_OPENAI_KEY_RE = re.compile(r"sk-[0-9A-Za-z_\-]{10,}")

# Keep LiteLLM quiet: it must never print request details (and so never a key).
litellm.suppress_debug_info = True
litellm.telemetry = False
logging.getLogger("LiteLLM").setLevel(logging.ERROR)
logging.getLogger("litellm").setLevel(logging.ERROR)


class AllProvidersUnavailable(Exception):
    """Every key in the pool failed (rate-limited, quota exhausted, 5xx, ...)."""

    def __init__(self, attempts: int):
        super().__init__("all providers unavailable")
        self.attempts = attempts


@dataclass
class LLMResult:
    reply: str
    resolution_source: str
    model: str
    input_tokens: int
    output_tokens: int
    key_id: str | None
    fallback_attempts: int


def _redact(text: str) -> str:
    text = " ".join(text.split())
    text = _SECRET_QUERY_RE.sub(r"\1<redacted>", text)
    text = _BEARER_RE.sub("Bearer <redacted>", text)
    text = _GEMINI_KEY_RE.sub("<redacted>", text)
    text = _OPENAI_KEY_RE.sub("<redacted>", text)
    return text[:500] + ("..." if len(text) > 500 else "")


def _provider_detail(exc: BaseException) -> str:
    pieces: list[str] = []
    seen: set[str] = set()
    candidates = [
        getattr(exc, "message", None) or str(exc),
        getattr(exc, "body", None),
        getattr(getattr(exc, "response", None), "text", None),
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        if isinstance(candidate, str):
            piece = candidate
        else:
            try:
                piece = json.dumps(candidate, ensure_ascii=False, default=str)
            except Exception:
                piece = str(candidate)
        if piece and piece not in seen:
            seen.add(piece)
            pieces.append(piece)
    return _redact(" ".join(pieces))


def _err_label(exc: BaseException) -> str:
    status = getattr(exc, "status_code", None)
    provider = getattr(exc, "llm_provider", None)
    label = type(exc).__name__
    if provider:
        label += f" provider={provider}"
    if status is not None:
        label += f" status={status}"
    detail = _provider_detail(exc)
    if detail:
        label += f" detail={detail}"
    return label


def _supports_param(model: str, param: str) -> bool:
    supported = litellm.get_supported_openai_params(model)
    return supported is None or param in supported


class Gateway:
    def __init__(self, settings: Settings):
        self.settings = settings
        # key ids are positional: key_1, key_2, ... Never the key itself.
        self._keys: list[tuple[str, str]] = [
            (f"key_{i}", key) for i, key in enumerate(settings.api_keys, start=1)
        ]
        self._cooldown_until: dict[str, float] = {}
        self.cache_enabled = settings.cache_enabled
        if self.cache_enabled:
            # Fresh in-memory cache. Key = model + the full message list
            # (+ sampling params); the API key is NOT part of the cache key, so a
            # response generated with key_1 is a hit for any later request.
            litellm.disable_cache()  # drop any previous instance -> always start empty
            litellm.enable_cache(type="local")
        else:
            litellm.disable_cache()

    # ------------------------------------------------------------------ info
    @property
    def key_ids(self) -> list[str]:
        return [key_id for key_id, _ in self._keys]

    @property
    def keys_configured(self) -> int:
        return len(self._keys)

    # ---------------------------------------------------------------- warm-up
    async def warm_up(self) -> None:
        """Exercise the cache-hit path once so the first real hit is not slow.

        LiteLLM lazily imports a few modules the first time it serves a cached
        response (~150 ms). Doing it at startup keeps "cache hit faster than a
        live call" true from the very first request.
        """
        if not self.cache_enabled:
            return
        kwargs: dict[str, Any] = dict(
            model="openai/cache-warmup", messages=[{"role": "user", "content": "warm-up"}]
        )
        try:
            response = ModelResponse(
                model="cache-warmup",
                choices=[{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )
            await litellm.cache.async_add_cache(response, **kwargs)
            await litellm.acompletion(
                api_key="warm-up", api_base="http://127.0.0.1:1/v1", num_retries=0, **kwargs
            )
        except Exception as exc:  # warm-up is best effort
            log.warning("cache warm-up skipped: %s", _err_label(exc))

    # --------------------------------------------------------------- key order
    def _ordered_keys(self) -> list[tuple[str, str]]:
        """Healthy keys first (in configured order); keys cooling down go last.

        A key that really failed (429 / quota / 5xx) is parked for
        KEY_COOLDOWN_SECONDS so we stop wasting a round trip on it, but it is
        still tried as a last resort so we never give up while a key might work.
        """
        now = time.monotonic()
        healthy = [k for k in self._keys if self._cooldown_until.get(k[0], 0.0) <= now]
        cooling = [k for k in self._keys if k not in healthy]
        return healthy + cooling

    def _park(self, key_id: str) -> None:
        if self.settings.key_cooldown_seconds > 0:
            self._cooldown_until[key_id] = time.monotonic() + self.settings.key_cooldown_seconds

    # ----------------------------------------------------------------- complete
    async def complete(
        self, messages: list[dict[str, str]], simulate: dict[str, int] | None = None
    ) -> LLMResult:
        """Answer `messages`, failing over across the key pool.

        `simulate` maps key_id -> HTTP status for this one request only. A
        simulated request skips the cache lookup so the failover path is always
        exercised, even for a message that was answered before.
        """
        simulate = simulate or {}
        s = self.settings
        failed = 0

        for key_id, key in self._ordered_keys():
            call: dict[str, Any] = dict(
                model=s.model,
                messages=messages,
                api_key=key,
                timeout=s.request_timeout_seconds,
                num_retries=0,  # retries across *keys* are ours; no same-key retry
            )
            if _supports_param(s.model, "temperature"):
                call["temperature"] = s.temperature
            if s.api_base:
                call["api_base"] = s.api_base
            if s.max_output_tokens:
                call["max_tokens"] = s.max_output_tokens
            if simulate:
                call["cache"] = {"no-cache": True}  # skip the read, still store the result

            try:
                if key_id in simulate:
                    raise build_error(simulate[key_id], s.model)
                response = await litellm.acompletion(**call)
                text = (response.choices[0].message.content or "").strip()
                if not text:
                    raise ValueError("provider returned an empty completion")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failed += 1
                log.warning(
                    "key failed key_id=%s model=%s error=%s -> failing over",
                    key_id,
                    s.model,
                    _err_label(exc),
                )
                if key_id not in simulate:  # simulated failures are per-request only
                    self._park(key_id)
                continue

            hit = response._hidden_params.get("cache_hit") is True
            if self.cache_enabled and not hit:
                # LiteLLM writes the entry in a background task; write it now as
                # well so an immediate identical request is already a hit.
                cache_kwargs = {k: v for k, v in call.items() if k != "cache"}
                try:
                    await litellm.cache.async_add_cache(response, **cache_kwargs)
                except Exception as exc:
                    log.warning("cache write skipped: %s", _err_label(exc))

            usage = getattr(response, "usage", None)
            return LLMResult(
                reply=text,
                resolution_source=CACHE if hit else LIVE,
                model=s.model,
                input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
                key_id=None if hit else key_id,
                fallback_attempts=0 if hit else failed,
            )

        raise AllProvidersUnavailable(failed)
