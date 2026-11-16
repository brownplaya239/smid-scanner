// Shared request/response handling for the worker's Claude Haiku calls
// (SEC filing summary, Trade Desk "Ask" box, portfolio screenshot import).
//
// Claude Haiku 5.5 differs from Haiku 4.5 in ways the old
// `j.content[0].text` read does not survive:
//   - It thinks by default, and a reply can start with `thinking` blocks,
//     so the answer is the joined `text` blocks, never content[0].
//   - Thinking counts toward max_tokens, so a small budget can be used up
//     before any answer. These three calls are short, single-shot jobs that
//     ran without thinking on Haiku 4.5, so we keep that: thinking disabled
//     (allowed on Haiku 5.5 at effort high or below) and an explicit effort.
//   - Its safety classifiers can decline with stop_reason "refusal" (HTTP
//     200). Haiku 5.5 has no server-side fallback, so a refusal is reported
//     to the caller as a normal failure.
//   - temperature/top_p/top_k and assistant prefill are rejected; none of
//     these calls send them.

export const HAIKU_MODEL = "claude-haiku-5-5";

/** Request body for one Haiku call.
 *  @param {{maxTokens:number, effort:"low"|"medium"|"high", content:any, system?:string}} o */
export function haikuBody(o) {
  const body = {
    model: HAIKU_MODEL,
    max_tokens: o.maxTokens,
    thinking: { type: "disabled" },
    output_config: { effort: o.effort },
    messages: [{ role: "user", content: o.content }],
  };
  if (o.system) body.system = o.system;
  return body;
}

/** Pull the answer out of a Messages API JSON response.
 *  Returns { ok: true, text } or { ok: false, reason, error }, where reason
 *  is "refusal" or "empty_response". Partial text before a refusal is
 *  discarded. */
export function claudeText(j) {
  if (j && j.stop_reason === "refusal") {
    const cat = j.stop_details && j.stop_details.category;
    return { ok: false, reason: "refusal",
             error: "Claude declined this request" +
                    (cat ? " (" + cat + ")" : "") + "." };
  }
  const blocks = (j && Array.isArray(j.content)) ? j.content : [];
  const text = blocks
    .filter(function (b) { return b && b.type === "text"; })
    .map(function (b) { return b.text || ""; })
    .join("")
    .trim();
  if (!text) {
    return { ok: false, reason: "empty_response",
             error: "Claude returned no answer (stop: " +
                    ((j && j.stop_reason) || "unknown") + ")." };
  }
  return { ok: true, text: text };
}
