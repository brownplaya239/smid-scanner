"""One place that sends a prompt to Claude and gets the answer text back.

Used by the scanner, the setup builder and the alt-data report. The current
Claude models (Opus 5.5, Sonnet 5.5) changed in ways the old one-liner
`client.messages.create(...).content[0].text` does not survive:

- A reply can start with one or more `thinking` blocks before the answer, so
  the answer is the joined `text` blocks, never `content[0]`.
- Thinking counts toward `max_tokens`, so budgets are larger than before and
  the request is streamed (large non-streamed requests hit HTTP timeouts).
- Safety classifiers can decline with `stop_reason == "refusal"` (HTTP 200).
  Server-side fallback (`fallbacks="default"`) re-runs a declined request on
  the model Anthropic recommends for that refusal category; if the whole chain
  still declines, or no answer text comes back, `ClaudeNoAnswer` is raised so
  each caller can take its existing "no LLM result" path.
- How hard the model thinks is set with `output_config.effort`.
"""

from __future__ import annotations

from typing import Any

# Opt-in for server-side refusal fallbacks ("default" routing by category).
# Claude API only; Opus 5.5 and Sonnet 5.5 accept it, Haiku 5.5 does not.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
SERVER_FALLBACK_MODELS = frozenset({"claude-opus-5-5", "claude-sonnet-5-5"})


class ClaudeNoAnswer(Exception):
    """Claude returned HTTP 200 but no usable answer text.

    `reason` is a short machine-readable tag ("refusal" or "empty_response")
    that callers pass on to their fallback paths and logs.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


def build_request(*, model: str, prompt: str, max_tokens: int, effort: str) -> dict[str, Any]:
    """The keyword arguments for `client.beta.messages.stream(...)`.

    No `thinking` field: Opus 5.5 always thinks and Sonnet 5.5 thinks
    adaptively by default; `effort` is the control. No temperature/top_p/top_k
    and no assistant prefill (both are rejected by these models).
    """
    req: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "output_config": {"effort": effort},
        "messages": [{"role": "user", "content": prompt}],
    }
    if model in SERVER_FALLBACK_MODELS:
        req["betas"] = [FALLBACK_BETA]
        req["fallbacks"] = "default"
    return req


def answer_text(message: Any) -> str:
    """The answer text of a finished message, or raise `ClaudeNoAnswer`.

    Joins every `text` block (skipping `thinking` and `fallback` blocks).
    A refusal is checked first: any partial text before a mid-stream
    refusal must be discarded, not parsed.
    """
    stop_reason = getattr(message, "stop_reason", None)
    if stop_reason == "refusal":
        details = getattr(message, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        raise ClaudeNoAnswer("refusal", f"category={category}")
    text = "".join(
        getattr(block, "text", "") or ""
        for block in (getattr(message, "content", None) or [])
        if getattr(block, "type", None) == "text"
    ).strip()
    if not text:
        raise ClaudeNoAnswer("empty_response", f"stop_reason={stop_reason}")
    return text


def ask_claude(client: Any, *, model: str, prompt: str, max_tokens: int, effort: str) -> str:
    """Send one user prompt, stream the reply, return its answer text.

    Anthropic API errors (auth, rate limit, 5xx) propagate unchanged, as they
    did with `messages.create`; `ClaudeNoAnswer` covers refusals and empty
    answers.
    """
    req = build_request(model=model, prompt=prompt, max_tokens=max_tokens, effort=effort)
    with client.beta.messages.stream(**req) as stream:
        message = stream.get_final_message()
    usage = getattr(message, "usage", None)
    if usage:
        it = getattr(usage, "input_tokens", 0) or 0
        ot = getattr(usage, "output_tokens", 0) or 0
        print(f"  Tokens — input:{it}  output:{ot}  stop:{getattr(message, 'stop_reason', None)}")
    served_by = getattr(message, "model", None)
    if served_by and served_by != model:
        print(f"  Note: {model} declined; answer served by fallback model {served_by}")
    return answer_text(message)
