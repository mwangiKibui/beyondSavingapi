"""ab-314: the DigitalOcean inference client, against a fake transport."""

import httpx
import pytest

from app.core.config import get_settings
from app.services.inference import InferenceError, complete, inference_enabled


@pytest.fixture
def key(monkeypatch):
    def set_key(value):
        monkeypatch.setattr(get_settings(), "do_inference_key", value)

    return set_key


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_sends_the_prompt_and_returns_text_and_usage(key):
    key("sk-test")
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = request.read()
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"ok": 1}'}}], "usage": {"prompt_tokens": 12, "completion_tokens": 5}},
        )

    reply = await complete("hello", client=client(handler))
    assert reply.content == '{"ok": 1}'
    assert (reply.prompt_tokens, reply.completion_tokens) == (12, 5)
    assert reply.model == get_settings().do_inference_model
    assert seen["url"].endswith("/chat/completions")
    assert seen["auth"] == "Bearer sk-test"
    assert b'"hello"' in seen["body"]


async def test_no_key_means_ai_off(key):
    key("  ")
    assert inference_enabled() is False
    with pytest.raises(InferenceError):
        await complete("hello", client=client(lambda r: httpx.Response(200)))


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(403, json={"id": "Forbidden"}),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{"message": {"content": "   "}}]}),
        httpx.Response(200, text="not json"),
    ],
)
async def test_any_failure_is_an_inference_error(key, response):
    key("sk-test")
    with pytest.raises(InferenceError):
        await complete("hello", client=client(lambda r: response))


async def test_a_network_failure_is_an_inference_error(key):
    key("sk-test")

    def handler(request):
        raise httpx.ConnectTimeout("slow")

    with pytest.raises(InferenceError):
        await complete("hello", client=client(handler))


async def test_ab345_an_empty_reply_is_its_own_error(key):
    from app.services.inference import EmptyReply

    key("sk-test")
    response = httpx.Response(200, json={"choices": [{"message": {"content": "  "}}]})
    with pytest.raises(EmptyReply):
        await complete("hello", client=client(lambda r: response))
