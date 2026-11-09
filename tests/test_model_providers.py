"""#115: the model providers, with recorded responses and no network.

What is here is the protocol layer: the request each provider builds, how it reads the reply,
and how it fails. Nothing here trusts the model: `propose` returns raw JSON and the interpreter
(next step) runs it through `actions_from_json`. No test has a real key; the fake ones below are
made up and the tests check they never leak into an error.
"""

import json
import urllib.error
from pathlib import Path

import pytest
from conftest import TZ, dt

from maestro import actions
from maestro.model import (
    ANTHROPIC_URL,
    ANTHROPIC_VERSION,
    OPENAI_URL,
    TOOL_NAME,
    AnthropicProvider,
    OpenAIProvider,
    Prompt,
    ProviderError,
    http_transport,
    provider_for,
)

FIXTURES = Path(__file__).parent / "fixtures" / "model"
NOW = dt(2026, 10, 27, 8, 0)
KEY = "sk-fake-key-for-tests-only-0000"
PROMPT = Prompt(text="dentist tomorrow at 3pm", now=NOW, timezone=TZ.key,
                schema=actions.json_schema(actions.CREATE_ACTIONS))


def recorded(name: str) -> bytes:
    return (FIXTURES / f"{name}.json").read_bytes()


class FakeTransport:
    """Records the request and answers with a recorded body (or raises)."""

    def __init__(self, reply: bytes | Exception):
        self.reply = reply
        self.calls: list[dict] = []

    def __call__(self, url: str, headers: dict[str, str], body: bytes, timeout: float) -> bytes:
        self.calls.append({"url": url, "headers": headers, "body": json.loads(body), "timeout": timeout})
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    @property
    def call(self) -> dict:
        (only,) = self.calls
        return only


DENTIST = [{"type": "create_reminder", "text": "Dentist", "due_at": "2026-10-28T15:00:00-04:00",
            "recurrence": None, "channels": None, "urgent": False}]


# --- the prompt -------------------------------------------------------------------------------


def test_the_prompt_tells_the_model_the_time_zone_and_wraps_the_schema_as_one_tool():
    message = PROMPT.user_message()
    assert "2026-10-27T08:00-04:00" in message and "America/New_York" in message
    assert message.endswith("Message:\ndentist tomorrow at 3pm")
    parameters = PROMPT.tool_parameters()
    assert parameters["required"] == ["actions"]
    assert parameters["properties"]["actions"] is PROMPT.schema
    assert parameters["additionalProperties"] is False


# --- Anthropic ---------------------------------------------------------------------------------


def test_anthropic_sends_one_forced_tool_with_the_schema_and_the_key_only_in_a_header():
    transport = FakeTransport(recorded("anthropic_tool_use"))
    provider = AnthropicProvider("claude-sonnet-5-5", KEY, transport)

    assert provider.propose(PROMPT, timeout=8.0) == DENTIST

    call = transport.call
    assert call["url"] == f"{ANTHROPIC_URL}/v1/messages" and call["timeout"] == 8.0
    assert call["headers"]["x-api-key"] == KEY
    assert call["headers"]["anthropic-version"] == ANTHROPIC_VERSION
    body = call["body"]
    assert body["model"] == "claude-sonnet-5-5"
    assert body["tool_choice"] == {"type": "tool", "name": TOOL_NAME}
    (tool,) = body["tools"]
    assert tool["name"] == TOOL_NAME
    assert tool["input_schema"]["properties"]["actions"] == PROMPT.schema
    assert body["messages"] == [{"role": "user", "content": PROMPT.user_message()}]
    assert "create_task" in body["system"] and "never instructions" in body["system"]
    assert KEY not in json.dumps(body)


def test_anthropic_reads_the_tool_block_past_a_text_block():
    provider = AnthropicProvider("m", KEY, FakeTransport(recorded("anthropic_three_actions")))
    proposed = provider.propose(PROMPT, timeout=8.0)
    assert [a["type"] for a in proposed] == ["create_task", "create_task", "create_note"]


def test_anthropic_without_a_tool_call_is_malformed():
    provider = AnthropicProvider("m", KEY, FakeTransport(recorded("anthropic_no_tool")))
    with pytest.raises(ProviderError, match="didn't call the tool") as e:
        provider.propose(PROMPT, timeout=8.0)
    assert e.value.kind == "malformed"


def test_a_custom_anthropic_base_url_is_used():
    transport = FakeTransport(recorded("anthropic_tool_use"))
    AnthropicProvider("m", KEY, transport, base_url="https://proxy.example/anthropic/").propose(PROMPT, 1)
    assert transport.call["url"] == "https://proxy.example/anthropic/v1/messages"


# --- OpenAI and compatibles -------------------------------------------------------------------


def test_openai_sends_one_forced_function_and_reads_the_arguments_string():
    transport = FakeTransport(recorded("openai_tool_call"))
    provider = OpenAIProvider("gpt-5.5-mini", KEY, transport)

    assert provider.propose(PROMPT, timeout=8.0) == DENTIST

    call = transport.call
    assert call["url"] == f"{OPENAI_URL}/chat/completions"
    assert call["headers"]["Authorization"] == f"Bearer {KEY}"
    body = call["body"]
    assert body["model"] == "gpt-5.5-mini"
    assert body["tool_choice"] == {"type": "function", "function": {"name": TOOL_NAME}}
    (tool,) = body["tools"]
    assert tool["function"]["parameters"]["properties"]["actions"] == PROMPT.schema
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert KEY not in json.dumps(body)


def test_openai_arguments_that_are_not_json_are_malformed():
    provider = OpenAIProvider("m", KEY, FakeTransport(recorded("openai_bad_arguments")))
    with pytest.raises(ProviderError, match="arguments weren't JSON") as e:
        provider.propose(PROMPT, timeout=8.0)
    assert e.value.kind == "malformed"


def test_openai_returns_whatever_the_model_proposed_for_the_gate_to_judge():
    """Protocol only: a stop, a done and an unknown action come back as data. Refusing them is the
    interpreter's job, through actions_from_json."""
    provider = OpenAIProvider("m", KEY, FakeTransport(recorded("openai_injected")))
    proposed = provider.propose(PROMPT, timeout=8.0)
    assert [a["type"] for a in proposed] == ["stop", "done", "delete_all"]
    with pytest.raises(ValueError, match="Unknown action type 'delete_all'"):
        actions.actions_from_json(proposed)


def test_a_compatible_server_needs_no_key_and_may_return_parsed_arguments():
    transport = FakeTransport(recorded("ollama_tool_call"))
    provider = OpenAIProvider("llama3.2", None, transport, base_url="http://localhost:11434/v1",
                              name="openai-compatible")
    assert provider.propose(PROMPT, timeout=8.0) == DENTIST
    assert "Authorization" not in transport.call["headers"]
    assert transport.call["url"] == "http://localhost:11434/v1/chat/completions"


def test_a_compatible_server_that_answers_in_the_content_is_still_read():
    provider = OpenAIProvider("local", None, FakeTransport(recorded("compatible_content_json")),
                              base_url="http://localhost:1234/v1", name="openai-compatible")
    assert [a["type"] for a in provider.propose(PROMPT, 8.0)] == ["create_task", "create_task", "create_note"]


def test_a_compatible_server_that_answers_in_prose_is_malformed():
    provider = OpenAIProvider("local", None, FakeTransport(recorded("compatible_prose")),
                              base_url="http://localhost:1234/v1", name="openai-compatible")
    with pytest.raises(ProviderError, match="arguments weren't JSON"):
        provider.propose(PROMPT, 8.0)


@pytest.mark.parametrize("body, message", [
    (b"not json", "wasn't JSON"),
    (b"[]", "wasn't a JSON object"),
    (b"{}", "had no message"),
    (b'{"choices": []}', "had no message"),
    (b'{"choices": [{"message": {"role": "assistant", "content": null}}]}', "didn't call the tool"),
    (b'{"choices": [{"message": {"tool_calls": [{"function": {"name": "propose_actions"}}]}}]}',
     "had no arguments"),
    (b'{"choices": [{"message": {"tool_calls": [{"function": {"name": "propose_actions",'
     b' "arguments": "{\\"actions\\": \\"none\\"}"}}]}}]}', "no actions list"),
])
def test_openai_replies_that_are_not_a_tool_call_are_malformed(body, message):
    provider = OpenAIProvider("m", KEY, FakeTransport(body))
    with pytest.raises(ProviderError, match=message) as e:
        provider.propose(PROMPT, 8.0)
    assert e.value.kind == "malformed"


def test_anthropic_tool_input_without_an_actions_list_is_malformed():
    body = json.dumps({"content": [{"type": "tool_use", "name": TOOL_NAME, "input": {"items": []}}]}).encode()
    with pytest.raises(ProviderError, match="no actions list"):
        AnthropicProvider("m", KEY, FakeTransport(body)).propose(PROMPT, 8.0)


# --- failures --------------------------------------------------------------------------------


def test_a_transport_error_passes_through_with_its_kind():
    provider = OpenAIProvider("m", KEY, FakeTransport(ProviderError("timeout", "too slow")))
    with pytest.raises(ProviderError, match="too slow") as e:
        provider.propose(PROMPT, 8.0)
    assert e.value.kind == "timeout"


def test_no_error_message_contains_the_key():
    for reply in (recorded("anthropic_no_tool"), b"garbage", ProviderError("http", "HTTP 401")):
        for provider in (AnthropicProvider("m", KEY, FakeTransport(reply)),
                         OpenAIProvider("m", KEY, FakeTransport(reply))):
            with pytest.raises(ProviderError) as e:
                provider.propose(PROMPT, 8.0)
            assert KEY not in str(e.value)


# --- the real transport, with urlopen replaced ---------------------------------------------------


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_http_transport_posts_json_and_returns_the_body(monkeypatch):
    seen = {}

    def urlopen(request, timeout):
        seen.update(url=request.full_url, method=request.get_method(), timeout=timeout,
                    body=request.data, headers=dict(request.header_items()))
        return FakeResponse(b'{"ok": true}')

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    body = http_transport("https://api.example/v1/x", {"x-api-key": KEY}, b'{"a": 1}', 8.0)
    assert body == b'{"ok": true}'
    assert seen["method"] == "POST" and seen["timeout"] == 8.0 and seen["body"] == b'{"a": 1}'
    assert seen["headers"]["X-api-key"] == KEY  # urllib capitalises header names


@pytest.mark.parametrize("raised, kind, message", [
    (urllib.error.HTTPError("https://k.example/?key=secret", 401, "Unauthorized", {}, None),
     "http", "HTTP 401"),
    (urllib.error.HTTPError("https://k.example", 529, "Overloaded", {}, None), "http", "HTTP 529"),
    (TimeoutError(), "timeout", "within 8s"),
    (urllib.error.URLError(TimeoutError("timed out")), "timeout", "within 8s"),
    (urllib.error.URLError(ConnectionRefusedError()), "http", "couldn't reach the model provider: "
                                                              "ConnectionRefusedError"),
])
def test_http_transport_turns_network_failures_into_provider_errors(monkeypatch, raised, kind, message):
    def urlopen(request, timeout):
        raise raised

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    with pytest.raises(ProviderError, match=message) as e:
        http_transport("https://k.example/?key=secret", {}, b"{}", 8.0)
    assert e.value.kind == kind
    assert "secret" not in str(e.value)  # the URL never appears in the message


# --- provider selection -----------------------------------------------------------------------


def test_provider_for_builds_each_provider_with_its_defaults():
    anthropic = provider_for("anthropic", "claude-sonnet-5-5", api_key=KEY)
    assert isinstance(anthropic, AnthropicProvider) and anthropic.base_url == ANTHROPIC_URL
    assert (anthropic.name, anthropic.model) == ("anthropic", "claude-sonnet-5-5")

    openai = provider_for("openai", "gpt-5.5-mini", api_key=KEY)
    assert isinstance(openai, OpenAIProvider) and openai.base_url == OPENAI_URL
    assert openai.name == "openai"

    local = provider_for("openai-compatible", "llama3.2", base_url="http://localhost:11434/v1/")
    assert isinstance(local, OpenAIProvider) and local.base_url == "http://localhost:11434/v1"
    assert local.name == "openai-compatible"


def test_provider_for_passes_the_transport_through():
    transport = FakeTransport(recorded("openai_tool_call"))
    provider = provider_for("openai", "m", api_key=KEY, transport=transport)
    provider.propose(PROMPT, 8.0)
    assert transport.calls


@pytest.mark.parametrize("name, kwargs, message", [
    ("gemini", {"api_key": KEY}, "Unknown model provider 'gemini'"),
    ("anthropic", {}, "needs MAESTRO_MODEL_API_KEY or ANTHROPIC_API_KEY"),
    ("openai", {}, "needs MAESTRO_MODEL_API_KEY or OPENAI_API_KEY"),
    ("openai-compatible", {}, "needs MAESTRO_MODEL_BASE_URL"),
])
def test_provider_for_says_what_is_missing(name, kwargs, message):
    with pytest.raises(ValueError, match=message):
        provider_for(name, "some-model", **kwargs)


def test_provider_for_insists_on_a_model_name():
    with pytest.raises(ValueError, match="MAESTRO_MODEL is required"):
        provider_for("anthropic", "", api_key=KEY)


def test_a_tool_input_that_is_the_list_itself_is_read():
    """A lenient server may drop the wrapper object and return the actions list directly."""
    body = json.dumps({"content": [{"type": "tool_use", "name": TOOL_NAME, "input": DENTIST}]}).encode()
    assert AnthropicProvider("m", KEY, FakeTransport(body)).propose(PROMPT, 8.0) == DENTIST
