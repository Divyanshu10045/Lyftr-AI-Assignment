"""`X-Simulate-Failure` test hook.

Header format: comma-separated `<key_id>=<status>` pairs, `*` meaning every key.
    X-Simulate-Failure: key_1=429
    X-Simulate-Failure: key_1=429,key_2=500
    X-Simulate-Failure: *=429
It only has an effect when ENABLE_FAILURE_SIMULATION=true.
"""

from __future__ import annotations

import litellm

SUPPORTED_STATUSES = {429, 500, 502, 503}


def parse_failure_header(value: str | None, key_ids: list[str]) -> dict[str, int]:
    """Return {key_id: status}. Unknown ids and unsupported statuses are ignored."""
    if not value:
        return {}
    plan: dict[str, int] = {}
    for part in value.split(","):
        name, sep, status = part.strip().partition("=")
        name, status = name.strip(), status.strip()
        if not sep or not status.isdigit() or int(status) not in SUPPORTED_STATUSES:
            continue
        targets = key_ids if name == "*" else ([name] if name in key_ids else [])
        for key_id in targets:
            plan[key_id] = int(status)
    return plan


def build_error(status: int, model: str) -> Exception:
    """The same exception class LiteLLM raises for a real provider response."""
    kwargs = dict(message=f"simulated provider failure ({status})", model=model, llm_provider="simulated")
    if status == 429:
        return litellm.RateLimitError(**kwargs)
    if status == 502:
        return litellm.BadGatewayError(**kwargs)
    if status == 503:
        return litellm.ServiceUnavailableError(**kwargs)
    return litellm.InternalServerError(**kwargs)
