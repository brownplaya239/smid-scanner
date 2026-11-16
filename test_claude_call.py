"""Tests for the Claude request/response handling (no network, no API key).

Python side: `claude_call.py` (scanner, setup builder, alt-data report).
Worker side: `cloudflare-worker/claude.mjs`, run through Node when it is
installed (the Haiku filing summary, Trade Desk "Ask" box, screenshot import).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_call import (
    FALLBACK_BETA,
    ClaudeNoAnswer,
    answer_text,
    ask_claude,
    build_request,
)

ROOT = Path(__file__).resolve().parent


def _msg(content, stop_reason="end_turn", model="claude-sonnet-5-5", category=None):
    blocks = [SimpleNamespace(**b) for b in content]
    details = SimpleNamespace(type="refusal", category=category) if category else None
    usage = SimpleNamespace(input_tokens=10, output_tokens=20)
    return SimpleNamespace(
        content=blocks, stop_reason=stop_reason, stop_details=details, model=model, usage=usage
    )


class _FakeStream:
    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._message


class _FakeClient:
    """Records the kwargs passed to client.beta.messages.stream(...)."""

    def __init__(self, message):
        self.calls: list[dict] = []
        messages = SimpleNamespace(stream=self._stream)
        self.beta = SimpleNamespace(messages=messages)
        self._message = message

    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeStream(self._message)


# ── response parsing ─────────────────────────────────────────────────────────


def test_answer_skips_leading_thinking_block():
    msg = _msg(
        [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": '[{"ticker": "AAA"}'},
            {"type": "text", "text": "]"},
        ]
    )
    assert answer_text(msg) == '[{"ticker": "AAA"}]'


def test_refusal_raises_even_with_partial_text():
    msg = _msg([{"type": "text", "text": "partial"}], stop_reason="refusal", category="cyber")
    with pytest.raises(ClaudeNoAnswer) as e:
        answer_text(msg)
    assert e.value.reason == "refusal"
    assert "cyber" in e.value.detail


def test_thinking_only_reply_is_empty_response():
    msg = _msg([{"type": "thinking", "thinking": "", "signature": "s"}], stop_reason="max_tokens")
    with pytest.raises(ClaudeNoAnswer) as e:
        answer_text(msg)
    assert e.value.reason == "empty_response"
    assert "max_tokens" in e.value.detail


def test_whitespace_only_text_is_empty_response():
    with pytest.raises(ClaudeNoAnswer):
        answer_text(_msg([{"type": "text", "text": "  \n"}]))


# ── request construction ─────────────────────────────────────────────────────


@pytest.mark.parametrize("model", ["claude-sonnet-5-5", "claude-opus-5-5"])
def test_request_shape_with_server_fallback(model):
    req = build_request(model=model, prompt="hi", max_tokens=32000, effort="medium")
    assert req["model"] == model
    assert req["max_tokens"] == 32000
    assert req["output_config"] == {"effort": "medium"}
    assert req["messages"] == [{"role": "user", "content": "hi"}]
    assert req["betas"] == [FALLBACK_BETA]
    assert req["fallbacks"] == "default"
    # Rejected by the new models: no thinking toggle/budget, sampling or prefill.
    for banned in ("thinking", "temperature", "top_p", "top_k"):
        assert banned not in req
    assert req["messages"][-1]["role"] == "user"


def test_no_fallback_param_for_models_without_it():
    req = build_request(model="claude-haiku-5-5", prompt="x", max_tokens=100, effort="low")
    assert "fallbacks" not in req and "betas" not in req


def test_ask_claude_streams_and_returns_text():
    client = _FakeClient(
        _msg(
            [{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": "ok"}]
        )
    )
    out = ask_claude(client, model="claude-opus-5-5", prompt="p", max_tokens=64000, effort="high")
    assert out == "ok"
    assert client.calls[0]["model"] == "claude-opus-5-5"
    assert client.calls[0]["output_config"] == {"effort": "high"}


def test_ask_claude_refusal_propagates_as_no_answer():
    client = _FakeClient(_msg([], stop_reason="refusal", category="bio"))
    with pytest.raises(ClaudeNoAnswer) as e:
        ask_claude(client, model="claude-sonnet-5-5", prompt="p", max_tokens=10, effort="low")
    assert e.value.reason == "refusal"


def test_call_sites_use_new_models():
    """No call site still names a retired model or reads content[0]."""
    for name, model in [
        ("scanner.py", '"claude-sonnet-5-5"'),
        ("setup_builder.py", '"claude-opus-5-5"'),
        ("alt_data.py", '"claude-sonnet-5-5"'),
    ]:
        src = (ROOT / name).read_text(encoding="utf-8")
        assert model in src, name
        assert ".content[0]" not in src, name
        assert "claude-sonnet-4" not in src and "claude-opus-4" not in src, name
    worker = (ROOT / "cloudflare-worker" / "worker.js").read_text(encoding="utf-8")
    assert "claude-haiku-4" not in worker
    assert "content[0]" not in worker


# ── worker (Node) ────────────────────────────────────────────────────────────

_WORKER_SCRIPT = r"""
import { haikuBody, claudeText, HAIKU_MODEL } from %s;
const out = {
  model: HAIKU_MODEL,
  body: haikuBody({ maxTokens: 300, effort: "low", content: "x" }),
  bodySys: haikuBody({ maxTokens: 500, effort: "medium", system: "S", content: "q" }),
  thinkingFirst: claudeText({ stop_reason: "end_turn", content: [
    { type: "thinking", thinking: "", signature: "s" },
    { type: "text", text: " answer " }] }),
  refusal: claudeText({ stop_reason: "refusal",
    stop_details: { type: "refusal", category: "cyber" },
    content: [{ type: "text", text: "partial" }] }),
  empty: claudeText({ stop_reason: "max_tokens",
    content: [{ type: "thinking", thinking: "", signature: "s" }] }),
  garbage: claudeText(null),
};
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_worker_claude_module():
    url = (ROOT / "cloudflare-worker" / "claude.mjs").as_uri()
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", _WORKER_SCRIPT % json.dumps(url)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    out = json.loads(proc.stdout)
    assert out["model"] == "claude-haiku-5-5"
    body = out["body"]
    assert body["model"] == "claude-haiku-5-5"
    assert body["max_tokens"] == 300
    assert body["thinking"] == {"type": "disabled"}
    assert body["output_config"] == {"effort": "low"}
    assert body["messages"] == [{"role": "user", "content": "x"}]
    for banned in ("temperature", "top_p", "top_k", "fallbacks", "system"):
        assert banned not in body
    assert out["bodySys"]["system"] == "S"
    assert out["thinkingFirst"] == {"ok": True, "text": "answer"}
    assert out["refusal"]["ok"] is False and out["refusal"]["reason"] == "refusal"
    assert "cyber" in out["refusal"]["error"]
    assert out["empty"]["ok"] is False and out["empty"]["reason"] == "empty_response"
    assert out["garbage"]["ok"] is False


# ── call-site fallback paths ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        (_msg([], stop_reason="refusal", category="cyber"), "refusal"),
        (_msg([{"type": "thinking", "thinking": "", "signature": "s"}]), "empty_response"),
    ],
)
def test_scanner_falls_back_without_llm(monkeypatch, message, reason):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-real-key")
    import scanner

    client = _FakeClient(message)
    monkeypatch.setattr(scanner.anthropic, "Anthropic", lambda **_: client)
    out = scanner.run_claude_analysis(
        [{"ticker": "AAA", "vol_ratio": 2.0, "change_pct": 3.0}], "SMID"
    )
    assert client.calls[0]["model"] == "claude-sonnet-5-5"
    assert out[0]["ticker"] == "AAA"
    assert out[0]["_llm_unavailable"] is True
    assert out[0]["_llm_failure"] == reason


def test_setup_builder_returns_no_setups_on_refusal(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-real-key")
    import setup_builder

    client = _FakeClient(_msg([], stop_reason="refusal", category="bio", model="claude-opus-5-5"))
    monkeypatch.setattr(setup_builder.anthropic, "Anthropic", lambda **_: client)
    assert setup_builder.run_claude_setup_analysis([{"ticker": "AAA"}]) == []
    assert client.calls[0]["model"] == "claude-opus-5-5"
    assert client.calls[0]["output_config"] == {"effort": "high"}
