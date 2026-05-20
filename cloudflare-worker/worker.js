/**
 * Cloudflare Worker — Ad-hoc ticker lookup endpoint
 *
 * Receives a POST {ticker} from the GitHub Pages web form, validates it,
 * and triggers the ticker-lookup.yml GitHub Actions workflow. The workflow
 * generates the research PDF and archives it to the GitHub Pages report site.
 *
 * Also serves GET ?quotes=SPY,QQQ,... — a CORS-enabled proxy returning
 * live-ish Yahoo Finance quotes for the dashboard's market ticker tape.
 * The fetch is done server-side here because Yahoo's endpoints send no
 * CORS headers, so a browser cannot call them directly. Responses are
 * edge-cached ~30s so rapid polling doesn't hammer Yahoo.
 *
 * Required environment variable (Cloudflare dashboard → Settings → Variables):
 *   PAT  — GitHub fine-grained PAT with Actions: read/write on the repo
 *
 * REPO is hardcoded below — it is not secret, and hardcoding eliminates the
 * "REPO secret missing/wrong -> 404" failure mode.
 */

const REPO = "brownplaya239/smid-scanner";

/** One Yahoo Finance quote — current price, % change vs the prior close, and
 *  the intraday 5-min OHLC bars for the dashboard's candlestick cards. */
async function fetchYahooQuote(sym) {
  const r2 = function (x) { return Math.round(x * 100) / 100; };
  try {
    const r = await fetch(
      "https://query1.finance.yahoo.com/v8/finance/chart/" +
        encodeURIComponent(sym) + "?range=1d&interval=5m",
      { headers: { "User-Agent": "Mozilla/5.0" }, cf: { cacheTtl: 30 } }
    );
    if (!r.ok) return { symbol: sym, price: null, change: null, bars: [] };
    const j = await r.json();
    const res = j && j.chart && j.chart.result && j.chart.result[0];
    const m = res && res.meta;
    if (!m) return { symbol: sym, price: null, change: null, bars: [] };
    const price = typeof m.regularMarketPrice === "number"
      ? m.regularMarketPrice : null;
    const prev = m.chartPreviousClose || m.previousClose || null;
    const change = (price != null && prev)
      ? Math.round((price / prev - 1) * 10000) / 100 : null;
    let bars = [];
    const q = res.indicators && res.indicators.quote && res.indicators.quote[0];
    if (q && Array.isArray(q.close)) {
      for (let i = 0; i < q.close.length; i++) {
        const o = q.open[i], h = q.high[i], l = q.low[i], c = q.close[i];
        if (typeof o === "number" && typeof h === "number" &&
            typeof l === "number" && typeof c === "number") {
          bars.push({ o: r2(o), h: r2(h), l: r2(l), c: r2(c) });
        }
      }
    }
    return { symbol: sym, price: price, change: change,
             prevClose: prev, bars: bars };
  } catch (e) {
    return { symbol: sym, price: null, change: null, bars: [] };
  }
}

/** Daily OHLC candles for a single ticker — used by the universal ticker
 *  drilldown panel. `range` accepts Yahoo values (1mo, 3mo, 6mo, 1y, 2y, 5y);
 *  `interval` accepts 1d / 1wk / 1mo. Cached at the edge for 5 minutes since
 *  daily bars only change once per session. */
async function fetchYahooCandles(sym, range, interval) {
  const r2 = function (x) { return Math.round(x * 100) / 100; };
  const allowedRange = new Set(["1mo","3mo","6mo","1y","2y","5y","ytd","max"]);
  const allowedInt   = new Set(["1d","1wk","1mo"]);
  range    = allowedRange.has(range)   ? range    : "3mo";
  interval = allowedInt.has(interval)  ? interval : "1d";
  try {
    const r = await fetch(
      "https://query1.finance.yahoo.com/v8/finance/chart/" +
        encodeURIComponent(sym) +
        "?range=" + range + "&interval=" + interval,
      { headers: { "User-Agent": "Mozilla/5.0" }, cf: { cacheTtl: 300 } }
    );
    if (!r.ok) return { symbol: sym, bars: [], error: "yahoo " + r.status };
    const j = await r.json();
    const res = j && j.chart && j.chart.result && j.chart.result[0];
    if (!res) return { symbol: sym, bars: [], error: "no chart result" };
    const m = res.meta || {};
    const ts = res.timestamp || [];
    const q = res.indicators && res.indicators.quote && res.indicators.quote[0];
    const out = [];
    if (q && Array.isArray(q.close)) {
      for (let i = 0; i < q.close.length; i++) {
        const o = q.open[i], h = q.high[i], l = q.low[i], c = q.close[i];
        if (typeof o === "number" && typeof h === "number" &&
            typeof l === "number" && typeof c === "number") {
          out.push({
            t: ts[i] || null,                       // epoch seconds
            o: r2(o), h: r2(h), l: r2(l), c: r2(c),
            v: (q.volume && q.volume[i]) || 0,
          });
        }
      }
    }
    return {
      symbol:   sym,
      bars:     out,
      price:    typeof m.regularMarketPrice === "number" ? m.regularMarketPrice : null,
      prevClose: m.chartPreviousClose || m.previousClose || null,
      currency: m.currency || null,
    };
  } catch (e) {
    return { symbol: sym, bars: [], error: String(e) };
  }
}

export default {
  async fetch(request, env, ctx) {
    const cors = {
      "Access-Control-Allow-Origin":  "*",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
    };

    // CORS preflight
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: cors });
    }

    // GET — market-quote proxy (?quotes=SPY,QQQ,...), daily-candle proxy
    // (?candles=NVDA&range=3mo&interval=1d), or health check
    if (request.method === "GET") {
      const url = new URL(request.url);
      const quotesParam  = url.searchParams.get("quotes");
      const candlesParam = url.searchParams.get("candles");
      if (quotesParam) {
        const cache = caches.default;
        const hit = await cache.match(request);
        if (hit) return hit;
        const syms = quotesParam.split(",")
          .map(function (s) { return s.trim(); })
          .filter(Boolean).slice(0, 12);
        const quotes = await Promise.all(syms.map(fetchYahooQuote));
        const resp = Response.json({ quotes: quotes }, {
          headers: { ...cors, "Cache-Control": "public, max-age=30" },
        });
        ctx.waitUntil(cache.put(request, resp.clone()));
        return resp;
      }
      if (candlesParam) {
        const cache = caches.default;
        const hit = await cache.match(request);
        if (hit) return hit;
        const sym = candlesParam.trim().toUpperCase();
        if (!sym || !/^[A-Z.\-]{1,8}$/.test(sym)) {
          return Response.json(
            { error: `Invalid ticker: "${sym}"` },
            { status: 400, headers: cors });
        }
        const data = await fetchYahooCandles(
          sym,
          url.searchParams.get("range")    || "3mo",
          url.searchParams.get("interval") || "1d"
        );
        const resp = Response.json(data, {
          headers: { ...cors, "Cache-Control": "public, max-age=300" },
        });
        ctx.waitUntil(cache.put(request, resp.clone()));
        return resp;
      }
      return new Response(
        "OK - ad-hoc ticker worker alive. POST {\"ticker\":\"NVDA\"} to trigger a lookup.",
        { headers: { ...cors, "Content-Type": "text/plain" } }
      );
    }

    if (request.method !== "POST") {
      return new Response("Method not allowed", { status: 405, headers: cors });
    }

    // Parse + validate ticker; pick the workflow by report type
    let ticker = "", report = "adhoc";
    try {
      const data = await request.json();
      ticker = String(data.ticker || "").toUpperCase().trim();
      report = String(data.report || "adhoc").toLowerCase();
    } catch {
      return Response.json({ ok: false, error: "Invalid JSON body" },
                           { status: 400, headers: cors });
    }
    if (!ticker || !/^[A-Z.\-]{1,8}$/.test(ticker)) {
      return Response.json({ ok: false, error: `Invalid ticker symbol: "${ticker}"` },
                           { status: 400, headers: cors });
    }
    const WORKFLOWS = { adhoc: "ticker-lookup.yml", altdata: "alt-data.yml" };
    const workflow = WORKFLOWS[report] || WORKFLOWS.adhoc;

    if (!env.PAT) {
      return Response.json(
        { ok: false, error: "Worker misconfigured: PAT secret is not set in Cloudflare." },
        { status: 500, headers: cors });
    }

    // Best-effort fast validity check — reject obviously-invalid tickers
    // instantly, before spending a GitHub Actions run. If the probe itself
    // fails (Yahoo blocks the edge IP), fall through — scanner.py still
    // produces a clean "ticker not found" report as the backstop.
    try {
      const probe = await fetch(
        "https://query1.finance.yahoo.com/v8/finance/chart/" +
          encodeURIComponent(ticker) + "?range=5d&interval=1d",
        { headers: { "User-Agent": "Mozilla/5.0" } }
      );
      if (probe.ok) {
        const pj = await probe.json();
        const res = pj && pj.chart && pj.chart.result;
        const hasData = res && res[0] && res[0].timestamp && res[0].timestamp.length > 0;
        if (!hasData) {
          return Response.json(
            { ok: false, error: `"${ticker}" is not a valid ticker — no price ` +
              `data found. Check the symbol and try again.` },
            { status: 400, headers: cors });
        }
      }
    } catch (e) {
      // probe failed — proceed; the workflow handles invalid tickers too
    }

    // Trigger the GitHub workflow_dispatch — ref is "master" (this repo's branch)
    const ghResp = await fetch(
      `https://api.github.com/repos/${REPO}/actions/workflows/${workflow}/dispatches`,
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
    let hint = "";
    if (ghResp.status === 404) {
      hint = " — 404 means the PAT cannot access the repo or lacks Actions:write. " +
             "Regenerate a fine-grained PAT scoped to smid-scanner with Actions: Read and write.";
    } else if (ghResp.status === 401) {
      hint = " — 401 means the PAT value is wrong or expired.";
    }
    return Response.json(
      { ok: false, error: `GitHub dispatch failed (${ghResp.status})${hint}` },
      { status: 502, headers: cors }
    );
  },
};
