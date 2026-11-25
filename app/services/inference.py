"""ab-314: a small client for DigitalOcean Serverless Inference (OpenAI-
compatible chat completions). Only DigitalOcean-hosted models are used, so
nothing is passed on to the model's creator; see the README.

Callers get InferenceError for anything that went wrong (no key, timeout,
HTTP error, empty reply) and decide what to fall back to. The request and
reply are never logged - only the status and sizes."""

import logging
from dataclasses import dataclass

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class InferenceError(Exception):
    pass


@dataclass
class InferenceReply:
    content: str
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None


def inference_enabled() -> bool:
    return bool(get_settings().do_inference_key.strip())


async def complete(
    prompt: str,
    *,
    max_tokens: int = 2000,
    client: httpx.AsyncClient | None = None,
    model: str | None = None,
    timeout: float | None = None,
) -> InferenceReply:
    """One user message in, the reply's text out. model: another model the
    key allows (ab-336); the layout model by default."""
    settings = get_settings()
    key = settings.do_inference_key.strip()
    if not key:
        raise InferenceError("No inference key configured")
    model = model or settings.do_inference_model
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_completion_tokens": max_tokens,
    }
    owned = client is None
    client = client or httpx.AsyncClient(timeout=timeout or settings.do_inference_timeout_seconds)
    try:
        response = await client.post(
            f"{settings.do_inference_url.rstrip('/')}/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {key}"},
        )
    except httpx.HTTPError as e:
        logger.warning("Inference call to %s failed: %s", model, type(e).__name__)
        raise InferenceError("The inference call failed") from e
    finally:
        if owned:
            await client.aclose()
    if response.status_code != 200:
        logger.warning("Inference call to %s returned HTTP %s", model, response.status_code)
        raise InferenceError(f"The inference call returned HTTP {response.status_code}")
    try:
        data = response.json()
        content = data["choices"][0]["message"].get("content") or ""
        usage = data.get("usage") or {}
    except (ValueError, KeyError, IndexError, TypeError) as e:
        logger.warning("Inference reply from %s wasn't the expected shape", model)
        raise InferenceError("Unexpected inference reply") from e
    if not content.strip():
        raise InferenceError("Empty inference reply")
    return InferenceReply(
        content=content,
        model=model,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
    )
