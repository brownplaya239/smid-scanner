/**
 * Cloudflare Worker — Ad-hoc ticker lookup endpoint
 *
 * Receives a POST {ticker} from the GitHub Pages web form, validates it,
 * and triggers the ticker-lookup.yml GitHub Actions workflow. The workflow
 * generates the research PDF and delivers it to the #onepager-adhoc Discord
 * channel (via DISCORD_TICKER_WEBHOOK_URL, configured as a GitHub secret).
 *
 * This replaces the earlier Discord-slash-command design — no ed25519
 * signature verification needed for a plain web form.
 *
 * Required environment variables (Cloudflare dashboard → Settings → Variables):
 *   PAT  — GitHub fine-grained PAT with Actions: read/write on the repo
 *   REPO — "brownplaya239/smid-scanner"
 */

export default {
  async fetch(request, env) {
    const cors = {
      "Access-Control-Allow-Origin":  "*",
      "Access-Control-Allow-Methods": "POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
    };

    // CORS preflight
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: cors });
    }

    // Health check
    if (request.method === "GET") {
      return new Response(
        "OK - ad-hoc ticker worker alive. POST {\"ticker\":\"NVDA\"} to trigger a lookup.",
        { headers: { ...cors, "Content-Type": "text/plain" } }
      );
    }

    if (request.method !== "POST") {
      return new Response("Method not allowed", { status: 405, headers: cors });
    }

    // Parse + validate ticker
    let ticker = "";
    try {
      const data = await request.json();
      ticker = String(data.ticker || "").toUpperCase().trim();
    } catch {
      return Response.json({ ok: false, error: "Invalid JSON body" },
                           { status: 400, headers: cors });
    }
    if (!ticker || !/^[A-Z.\-]{1,8}$/.test(ticker)) {
      return Response.json({ ok: false, error: `Invalid ticker symbol: "${ticker}"` },
                           { status: 400, headers: cors });
    }

    // Trigger the GitHub workflow_dispatch — note ref is "master" (this repo's branch)
    const ghResp = await fetch(
      `https://api.github.com/repos/${env.REPO}/actions/workflows/ticker-lookup.yml/dispatches`,
      {
        method: "POST",
        headers: {
          "Authorization":        `Bearer ${env.PAT}`,
          "Accept":               "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent":           "smid-scanner-adhoc-web",
          "Content-Type":         "application/json",
        },
        body: JSON.stringify({ ref: "master", inputs: { ticker } }),
      }
    );

    if (ghResp.ok) {
      return Response.json({ ok: true, ticker }, { headers: cors });
    }
    const detail = await ghResp.text();
    return Response.json(
      { ok: false, error: `GitHub dispatch failed (${ghResp.status}): ${detail}` },
      { status: 502, headers: cors }
    );
  },
};
