// TickerDesk live-news ingestor.
// Holds a websocket to Alpaca's real-time news stream (Benzinga-sourced) and
// writes each headline into the Supabase `news_live` table. Browsers subscribe
// to that table via Supabase Realtime, so headlines reach the Live tape in
// ~1-2s end-to-end. Run this as a small always-on worker (Railway/Fly/Docker).
//
// Required env:
//   ALPACA_KEY              Alpaca API key id
//   ALPACA_SECRET           Alpaca API secret
//   SUPABASE_URL            https://<project>.supabase.co
//   SUPABASE_SERVICE_ROLE   Supabase service_role key (SERVER-SIDE ONLY)

import WebSocket from "ws";

const {
  ALPACA_KEY,
  ALPACA_SECRET,
  SUPABASE_URL,
  SUPABASE_SERVICE_ROLE,
} = process.env;

if (!ALPACA_KEY || !ALPACA_SECRET || !SUPABASE_URL || !SUPABASE_SERVICE_ROLE) {
  console.error("Missing env. Need ALPACA_KEY, ALPACA_SECRET, SUPABASE_URL, " +
    "SUPABASE_SERVICE_ROLE.");
  process.exit(1);
}

const ALPACA_NEWS_WS = "wss://stream.data.alpaca.markets/v1beta1/news";
const REST = SUPABASE_URL.replace(/\/+$/, "") + "/rest/v1/news_live";
const SB_HEADERS = {
  "apikey": SUPABASE_SERVICE_ROLE,
  "Authorization": "Bearer " + SUPABASE_SERVICE_ROLE,
  "Content-Type": "application/json",
};

// Benzinga headlines arrive HTML-entity-encoded ("What&#39;s"). Decode so the
// tape renders clean text (the frontend re-escapes on render). &amp; last to
// avoid double-decoding.
function decodeEntities(s) {
  if (!s) return "";
  return String(s)
    .replace(/&#(\d+);/g, (_, d) => String.fromCodePoint(+d))
    .replace(/&#x([0-9a-fA-F]+);/g, (_, h) => String.fromCodePoint(parseInt(h, 16)))
    .replace(/&quot;/g, '"').replace(/&apos;/g, "'")
    .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&");
}

async function insertHeadline(n) {
  const row = {
    id:           String(n.id),
    headline:     decodeEntities(n.headline || ""),
    summary:      decodeEntities(n.summary || ""),
    source:       n.source || "",
    url:          n.url || "",
    symbols:      Array.isArray(n.symbols) ? n.symbols : [],
    published_at: n.created_at || n.updated_at || new Date().toISOString(),
  };
  if (!row.id || !row.headline) return;
  try {
    const res = await fetch(REST, {
      method: "POST",
      headers: { ...SB_HEADERS,
        "Prefer": "resolution=ignore-duplicates,return=minimal" },
      body: JSON.stringify(row),
    });
    if (res.ok || res.status === 409) {
      console.log("→", row.published_at, "[" + row.symbols.join(",") + "]",
        row.headline.slice(0, 90));
    } else {
      console.error("insert", res.status, (await res.text()).slice(0, 200));
    }
  } catch (e) { console.error("insert error:", e.message); }
}

async function prune() {
  const cutoff = new Date(Date.now() - 3 * 24 * 3600 * 1000).toISOString();
  try {
    await fetch(REST + "?published_at=lt." + encodeURIComponent(cutoff), {
      method: "DELETE",
      headers: { ...SB_HEADERS, "Prefer": "return=minimal" },
    });
  } catch (e) { console.error("prune error:", e.message); }
}

let backoff = 1000;
function connect() {
  const ws = new WebSocket(ALPACA_NEWS_WS);

  ws.on("open", () => {
    console.log("WS open → authenticating");
    ws.send(JSON.stringify({ action: "auth", key: ALPACA_KEY, secret: ALPACA_SECRET }));
  });

  ws.on("message", (buf) => {
    let msgs;
    try { msgs = JSON.parse(buf.toString()); } catch { return; }
    if (!Array.isArray(msgs)) msgs = [msgs];
    for (const m of msgs) {
      if (m.T === "success" && m.msg === "authenticated") {
        console.log("authenticated → subscribing to all news");
        ws.send(JSON.stringify({ action: "subscribe", news: ["*"] }));
        backoff = 1000;
      } else if (m.T === "subscription") {
        console.log("subscribed:", JSON.stringify(m.news || []));
      } else if (m.T === "n") {
        insertHeadline(m);
      } else if (m.T === "error") {
        console.error("alpaca error:", JSON.stringify(m));
      }
    }
  });

  ws.on("close", () => {
    console.warn("WS closed; reconnecting in", backoff, "ms");
    setTimeout(connect, backoff);
    backoff = Math.min(backoff * 2, 30000);
  });
  ws.on("error", (e) => { console.error("WS error:", e.message); ws.close(); });
}

connect();
prune();
setInterval(prune, 60 * 60 * 1000);
