"""LiteLLM gateway: key pool, automatic failover and response caching.

LiteLLM provides the provider abstraction, the uniform exception mapping
(429 -> RateLimitError, 5xx -> InternalServerError / ServiceUnavailableError ...)
and the response cache. The loop in `Gateway.complete` adds what a plain router
cannot give us: deterministic key priority, per-request attribution
(`key_id`, `fallback_attempts`) and a per-request failure-simulation hook.
"""

from __future__ import annotations

import asyncio
import logging
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


def _err_label(exc: BaseException) -> str:
    status = getattr(exc, "status_code", None)
    return f"{type(exc).__name__}" + (f"({status})" if status else "")


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
                temperature=s.temperature,
                timeout=s.request_timeout_seconds,
                num_retries=0,  # retries across *keys* are ours; no same-key retry
            )
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
                log.warning("key failed key_id=%s error=%s -> failing over", key_id, _err_label(exc))
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
