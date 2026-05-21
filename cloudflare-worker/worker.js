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

// ── SEC EDGAR — filings list + targeted summarization ───────────────────
//
// EDGAR requires a User-Agent identifying the requester. Set once here.
const SEC_UA = "TickerDesk Dashboard support@tickerdesk.io";

// SEC's ticker→CIK index. ~1MB JSON, refreshed by SEC weekly, mostly
// stable. We cache at the edge for 24h so the worker hits SEC once per
// region per day.
async function fetchSecTickerMap() {
  const r = await fetch(
    "https://www.sec.gov/files/company_tickers.json",
    { headers: { "User-Agent": SEC_UA }, cf: { cacheTtl: 86400 } }
  );
  if (!r.ok) return null;
  return r.json();
}
async function tickerToCIK(ticker) {
  const map = await fetchSecTickerMap();
  if (!map) return null;
  const want = ticker.toUpperCase();
  for (const k of Object.keys(map)) {
    if (map[k].ticker === want) {
      return String(map[k].cik_str).padStart(10, "0");
    }
  }
  return null;
}

/** EDGAR submissions: company's recent filings.
 *  Returns up to `count` most recent filings shaped for the dashboard,
 *  with a heuristic-derived category flag per entry (the "color dot"). */
async function fetchSecFilings(ticker, count) {
  const cik = await tickerToCIK(ticker);
  if (!cik) return { ticker: ticker, error: "Ticker not found in EDGAR index",
                     filings: [] };
  const r = await fetch(
    "https://data.sec.gov/submissions/CIK" + cik + ".json",
    { headers: { "User-Agent": SEC_UA }, cf: { cacheTtl: 300 } }
  );
  if (!r.ok) return { ticker: ticker, cik: cik, error: "edgar " + r.status,
                      filings: [] };
  const j = await r.json();
  const rec = j.filings && j.filings.recent;
  if (!rec) return { ticker: ticker, cik: cik, filings: [] };
  const out = [];
  const max = Math.min(count || 25, (rec.form || []).length);
  for (let i = 0; i < max; i++) {
    const form = rec.form[i];
    const acc = (rec.accessionNumber[i] || "").replace(/-/g, "");
    const items = (rec.items || [])[i] || "";
    const date = rec.filingDate[i];
    const primary = rec.primaryDocument[i];
    const cikInt = parseInt(cik, 10);
    out.push({
      form: form,
      filingDate: date,
      acceptedDate: rec.acceptanceDateTime ? rec.acceptanceDateTime[i] : null,
      accession: rec.accessionNumber[i],
      items: items,                            // 8-K items, comma-separated
      description: rec.primaryDocDescription ? rec.primaryDocDescription[i] : "",
      // Primary doc URL (the actual filing to read/summarize)
      url: "https://www.sec.gov/Archives/edgar/data/" + cikInt + "/" +
           acc + "/" + primary,
      filingIndexUrl: "https://www.sec.gov/cgi-bin/browse-edgar?action=" +
        "getcompany&CIK=" + cik + "&type=" + encodeURIComponent(form) +
        "&dateb=&owner=include&count=10",
      category: filingCategory(form, items),
      // Heuristic label — what we can say without LLM
      headline: filingHeadline(form, items, rec.primaryDocDescription
        ? rec.primaryDocDescription[i] : ""),
      // Whether body summarization with LLM adds material value
      needsAI: needsAISummary(form, items),
    });
  }
  return {
    ticker: ticker,
    cik: cik,
    name: j.name || ticker,
    filings: out,
  };
}

/** Classify a filing for the dashboard's color-flag system.
 *  red:  restatement / late-filer risk
 *  amber: dilution
 *  blue:  M&A / material agreement / earnings release
 *  green: insider buying (Form 4 — net direction parsed downstream)
 *  purple: activist / >5% holder
 *  gold:  10-K / 10-Q (periodic financials)
 *  gray:  routine / boilerplate */
// Form names come back inconsistently from EDGAR — "SC 13D", "SCHEDULE 13D",
// "13D" are all the same thing. Normalize so downstream checks are simple.
function is13D(form) { return /^(SC |SCHEDULE )?13D/i.test(form); }
function is13G(form) { return /^(SC |SCHEDULE )?13G/i.test(form); }

function filingCategory(form, items) {
  const it = (items || "");
  // 13D / 13G come in plain + /A variants — both stay purple; the amendment
  // is just a position-change update, not a restatement red flag.
  if (is13D(form)) return "purple";
  if (is13G(form)) return "purple";                           // passive >5%
  // Real restatement / late-filer red flags
  if (form === "NT 10-K" || form === "NT 10-Q") return "red";
  if (form === "10-K/A" || form === "10-Q/A") return "red";
  if (it.indexOf("4.02") >= 0) return "red";                  // non-reliance
  if (form === "424B5" || form === "424B4" || form === "S-3" ||
      form === "S-1") return "amber";
  if (form === "Form 4" || form === "4") return "green";
  if (it.indexOf("1.01") >= 0 || it.indexOf("2.01") >= 0 ||
      it.indexOf("2.02") >= 0) return "blue";
  if (it.indexOf("5.02") >= 0) return "blue";                 // exec changes
  if (it.indexOf("8.01") >= 0) return "blue";                 // material event
  if (form === "10-K" || form === "10-Q") return "gold";
  if (form === "DEF 14A" || form === "PRE 14A") return "gold";
  return "gray";
}

/** Heuristic, LLM-free one-liner for the filing — what we can derive
 *  from form + items alone, no body parsing. */
function filingHeadline(form, items, desc) {
  const it = (items || "").split(",").map(s => s.trim()).filter(Boolean);
  const ITEM_LABEL = {
    "1.01": "Material Agreement",
    "1.02": "Termination of Material Agreement",
    "2.01": "Acquisition / Disposition Completed",
    "2.02": "Earnings Release",
    "2.05": "Restructuring / Exit Costs",
    "2.06": "Material Impairment",
    "3.01": "NYSE/NASDAQ Listing Notice",
    "3.02": "Unregistered Equity Sale (Dilution)",
    "3.03": "Modification of Rights of Holders",
    "4.01": "Auditor Change",
    "4.02": "Non-Reliance on Prior Financials",
    "5.01": "Change in Control",
    "5.02": "Officer/Director Change",
    "5.03": "Bylaw/Charter Amendment",
    "5.07": "Submission of Matters to Holder Vote",
    "7.01": "Reg FD Disclosure",
    "8.01": "Other Material Event",
    "9.01": "Financial Statements / Exhibits",
  };
  if (form === "Form 4" || form === "4") return "Insider transaction";
  if (form === "10-K") return "Annual report (10-K)";
  if (form === "10-Q") return "Quarterly report (10-Q)";
  if (form === "10-K/A") return "Amended annual report (10-K/A)";
  if (form === "10-Q/A") return "Amended quarterly report (10-Q/A)";
  if (form === "NT 10-K") return "Late filing notice (NT 10-K)";
  if (form === "NT 10-Q") return "Late filing notice (NT 10-Q)";
  if (form === "424B5") return "Prospectus supplement / shelf takedown";
  if (form === "424B4") return "Prospectus supplement";
  if (form === "S-1") return "IPO registration (S-1)";
  if (form === "S-3") return "Shelf registration (S-3)";
  if (is13D(form)) return form.match(/\/A$/)
    ? "13D/A — activist position update"
    : "13D filed — activist / >5% holder";
  if (is13G(form)) return form.match(/\/A$/)
    ? "13G/A — passive holder position update"
    : "13G filed — passive 5%+ holder";
  if (form === "DEF 14A") return "Definitive proxy statement";
  if (form === "8-K" && it.length) {
    if (it.length === 1) return "8-K · " + (ITEM_LABEL[it[0]] || ("Item " + it[0]));
    if (it.length <= 3)  return "8-K · " + it.map(x => ITEM_LABEL[x] || x).join(" + ");
    return "8-K · " + it.length + " items";
  }
  return form + (desc ? " · " + desc : "");
}

/** Which filings benefit enough from body summarization to justify the
 *  LLM call. Most filings (Form 4, 13G, routine 8-Ks, S-8) don't. */
function needsAISummary(form, items) {
  if (/\/A$/.test(form)) return true;                          // amendments
  if (form === "NT 10-K" || form === "NT 10-Q") return true;
  if (form === "8-K") {
    const it = (items || "");
    // Items worth the LLM call: M&A, dispositions, restructuring, dilution,
    // restatements, exec changes, other material events.
    // Skip routine 2.02 (earnings — Polygon News covers it) and 9.01 (exhibits).
    return ["1.01","1.02","2.01","2.05","3.02","4.02","5.02","8.01"]
      .some(code => it.indexOf(code) >= 0);
  }
  if (is13D(form) && !/\/A$/.test(form)) return true;
  return false;
}

/** Anthropic Haiku call — concise 2-3 sentence summary of an SEC filing.
 *  Returns { summary, ok, error?, usage? }. Edge-cached by the caller. */
async function summarizeFiling(env, ticker, form, items, primaryUrl) {
  if (!env.ANTHROPIC_API_KEY) {
    return { ok: false, error: "ANTHROPIC_API_KEY not set in worker." };
  }
  // Fetch the filing's primary document, strip tags, truncate. Filings
  // are HTML; the SEC also returns large embedded tables. Aggressive
  // truncation + tag stripping keeps token cost predictable.
  let body;
  try {
    const r = await fetch(primaryUrl,
      { headers: { "User-Agent": SEC_UA }, cf: { cacheTtl: 604800 } });
    if (!r.ok) return { ok: false, error: "filing fetch " + r.status };
    body = await r.text();
  } catch (e) {
    return { ok: false, error: "filing fetch: " + String(e) };
  }
  // Strip HTML, normalize whitespace, truncate to ~12K tokens (~48K chars)
  let text = body
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&#160;/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  if (text.length > 48000) text = text.slice(0, 48000);
  // Build the prompt
  const ITEM_HINT = items
    ? "\nFiling items: " + items
    : "";
  const prompt =
    "You are summarizing an SEC " + form + " filing for ticker " + ticker +
    "." + ITEM_HINT +
    "\n\nWrite a 2-3 sentence factual summary (under 80 words) of what " +
    "happened. Lead with the most material fact. Use specific dollar " +
    "amounts, percentages, and named parties when present. Skip " +
    "boilerplate. If the filing is purely routine / procedural, return " +
    "exactly: \"Routine procedural filing — no actionable content.\"\n\n" +
    "FILING TEXT:\n" + text;
  try {
    const r = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: {
        "x-api-key":         env.ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
      },
      body: JSON.stringify({
        model: "claude-haiku-4-5",
        max_tokens: 250,
        messages: [{ role: "user", content: prompt }],
      }),
    });
    if (!r.ok) {
      const errText = await r.text();
      return { ok: false, error: "anthropic " + r.status + ": " +
                                  errText.slice(0, 200) };
    }
    const j = await r.json();
    const summary = j.content && j.content[0] && j.content[0].text || "";
    return {
      ok:      true,
      summary: summary.trim(),
      usage:   j.usage || null,
      model:   j.model,
    };
  } catch (e) {
    return { ok: false, error: "anthropic call: " + String(e) };
  }
}

/** Live UOA snapshot for a single underlying — pulls Polygon's
 *  /v3/snapshot/options/{ticker} which returns every active contract
 *  for that underlying with current day's volume, OI, last quote, IV,
 *  greeks. Filters to "interesting" flow: vol/OI > 1.5 (volume
 *  exceeding open interest, the classic UOA tell), volume >= 100, and
 *  delta-weighted notional premium >= 25k. Returns top 25 by premium.
 *
 *  This is what powers the drilldown panel's "live flow" subsection —
 *  so users see flow as of ~now, not as of the last 75-min batch.
 *  Edge cached 30s to limit Polygon hits while a user clicks around.
 */
async function fetchLiveUOA(env, ticker, debug) {
  if (!env.POLYGON_API_KEY) {
    return { error: "POLYGON_API_KEY not configured", contracts: [] };
  }
  // Polygon's options snapshot supports limit + filter params but its
  // `sort` parameter only accepts: ticker, expiration_date, strike_price,
  // contract_type. There's NO sort-by-volume — so we pull a wide
  // result set (250 contracts) with explicit expiry/strike windows and
  // do the volume ranking client-side.
  // expiration_date.gte = today  → don't get rid of today-expiring
  //                                  contracts (0DTE flow is real flow)
  const today = new Date().toISOString().slice(0, 10);
  const url = "https://api.polygon.io/v3/snapshot/options/" +
              encodeURIComponent(ticker) +
              "?limit=250" +
              "&expiration_date.gte=" + today +
              "&apiKey=" + env.POLYGON_API_KEY;
  let r;
  try {
    r = await fetch(url, {
      signal: AbortSignal.timeout(10000),
      cf: { cacheTtl: 30 },
    });
  } catch (e) {
    return { error: "Polygon snapshot timeout", contracts: [] };
  }
  if (!r.ok) {
    let body = "";
    try { body = await r.text(); } catch (e) {}
    return { error: "Polygon snapshot HTTP " + r.status,
             body: body.slice(0, 500), contracts: [] };
  }
  const j = await r.json();
  const results = (j && j.results) || [];
  // Walk all returned contracts and rank. Track WHY each one drops so
  // we can return diagnostic counts when debug=1 (cuts down on guessing).
  const rows = [];
  let underlyingPrice = null;
  let stats = { total: results.length, no_details: 0, low_vol: 0,
                low_voi: 0, low_prem: 0, kept: 0 };
  for (const c of results) {
    const d   = c.details || {};
    const day = c.day     || {};
    const lq  = c.last_quote || {};
    const greeks = c.greeks || {};
    if (!d.contract_type || !d.strike_price || !d.expiration_date) {
      stats.no_details++; continue;
    }
    if (underlyingPrice == null && c.underlying_asset &&
        typeof c.underlying_asset.price === "number") {
      underlyingPrice = c.underlying_asset.price;
    }
    const vol = day.volume || 0;
    const oi  = c.open_interest || 0;
    if (vol < 100) { stats.low_vol++; continue; }
    const voi = oi > 0 ? vol / oi : (vol > 0 ? 999 : 0);
    if (voi < 1.5) { stats.low_voi++; continue; }
    const mid = (lq.ask && lq.bid) ? (lq.ask + lq.bid) / 2 :
                (lq.last || day.close || 0);
    const premium = mid * vol * 100;
    if (premium < 25000) { stats.low_prem++; continue; }
    stats.kept++;
    const dte = (() => {
      const exp = new Date(d.expiration_date + "T16:00:00-04:00");
      return Math.max(0, Math.round((exp - Date.now()) / 86400000));
    })();
    rows.push({
      contract:    d.ticker || (d.contract_type + d.strike_price),
      type:        d.contract_type,
      strike:      d.strike_price,
      expiry:      d.expiration_date,
      dte:         dte,
      volume:      vol,
      open_interest: oi,
      vol_oi:      Math.round(voi * 10) / 10,
      premium:     Math.round(premium),
      iv:          c.implied_volatility || null,
      delta:       greeks.delta || null,
      mid:         Math.round(mid * 100) / 100,
      bid:         lq.bid || null,
      ask:         lq.ask || null,
    });
  }
  rows.sort(function (a, b) { return b.premium - a.premium; });
  const out = {
    ticker:           ticker,
    underlying_price: underlyingPrice,
    contracts:        rows.slice(0, 25),
    total_flagged:    rows.length,
    fetched:          new Date().toISOString(),
  };
  if (debug) {
    out._stats = stats;
    out._polygon_status = j && j.status;
    out._polygon_msg = (j && j.message) || (j && j.error) || null;
    // Sample one raw row so we can see the actual shape
    if (results.length) {
      const s = results[0];
      out._sample = {
        details: s.details, day: s.day, oi: s.open_interest,
        underlying: s.underlying_asset, has_last_quote: !!s.last_quote,
      };
    }
  }
  return out;
}

/** Strip a Polygon news article down to just the fields the dashboard
 *  renders — drops large fields like the full article body and trims
 *  the response from Polygon (~3KB per article) to ~600 bytes. */
function simplifyArticle(a) {
  if (!a) return null;
  return {
    id:        a.id,
    title:     a.title || "",
    url:       a.article_url || "",
    publisher: a.publisher ? (a.publisher.name || "") : "",
    favicon:   a.publisher ? (a.publisher.favicon_url || "") : "",
    published: a.published_utc || "",
    tickers:   a.tickers || [],
    description: a.description ? String(a.description).slice(0, 400) : "",
    keywords:  a.keywords || [],
    image:     a.image_url || "",
    insights: (a.insights || []).map(function (ins) {
      return {
        ticker:    ins.ticker,
        sentiment: ins.sentiment,                 // positive | negative | neutral
        reasoning: ins.sentiment_reasoning
          ? String(ins.sentiment_reasoning).slice(0, 300)
          : "",
      };
    }),
  };
}

/** News headlines via the Polygon News API. `ticker` either a specific
 *  symbol or "general" for the firehose. Cached at the edge for 5 minutes
 *  since news cadence is well under that. */
async function fetchPolygonNews(env, ticker, limit) {
  if (!env.POLYGON_API_KEY) {
    return { error: "POLYGON_API_KEY not configured in worker environment.",
             articles: [] };
  }
  const params = new URLSearchParams({
    limit: String(Math.min(Math.max(parseInt(limit) || 50, 1), 1000)),
    order: "desc",
    sort:  "published_utc",
    apiKey: env.POLYGON_API_KEY,
  });
  if (ticker && ticker !== "general") {
    params.set("ticker", ticker.toUpperCase());
  }
  try {
    const r = await fetch(
      "https://api.polygon.io/v2/reference/news?" + params.toString(),
      {
        cf: { cacheTtl: 300 },
        signal: AbortSignal.timeout(8000),    // 8s — Polygon News has
                                              // had upstream outages
      }
    );
    if (!r.ok) {
      return { error: "polygon " + r.status, status: r.status, articles: [] };
    }
    const j = await r.json();
    const articles = (j.results || []).map(simplifyArticle).filter(Boolean);
    return { count: articles.length, articles: articles };
  } catch (e) {
    const msg = e && e.name === "TimeoutError"
      ? "Polygon News upstream timeout (their endpoint is slow/down)"
      : String(e);
    return { error: msg, articles: [] };
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
      // SEC filings list — ?filings=TICKER returns the last ~25 filings
      // with form, date, items, primary doc URL, and a heuristic
      // category flag the dashboard uses to color-code each row.
      const filingsParam = url.searchParams.get("filings");
      if (filingsParam) {
        const cache = caches.default;
        const hit = await cache.match(request);
        if (hit) return hit;
        const tk = filingsParam.trim().toUpperCase();
        if (!tk || !/^[A-Z.\-]{1,8}$/.test(tk)) {
          return Response.json(
            { error: `Invalid ticker: "${tk}"`, filings: [] },
            { status: 400, headers: cors });
        }
        const count = Math.min(
          Math.max(parseInt(url.searchParams.get("limit") || 25, 10), 1), 50);
        const data = await fetchSecFilings(tk, count);
        const resp = Response.json(data, {
          headers: { ...cors, "Cache-Control": "public, max-age=300" },
        });
        ctx.waitUntil(cache.put(request, resp.clone()));
        return resp;
      }
      // SEC filing summary — ?filing-summary=ACCESSION&ticker=T&form=F
      //   &items=ITEMS&url=PRIMARY_URL
      // Calls Anthropic Haiku on the body text. Edge-cached for 7 days
      // (filings are immutable post-publish, so summary is too).
      // Caller is expected to gate this by checking needsAISummary first.
      const summaryParam = url.searchParams.get("filing-summary");
      if (summaryParam) {
        const cache = caches.default;
        const hit = await cache.match(request);
        if (hit) return hit;
        const tk    = (url.searchParams.get("ticker") || "").toUpperCase();
        const form  = url.searchParams.get("form")  || "";
        const items = url.searchParams.get("items") || "";
        const prim  = url.searchParams.get("url")   || "";
        if (!tk || !form || !prim) {
          return Response.json(
            { ok: false, error: "Missing ticker, form, or url param" },
            { status: 400, headers: cors });
        }
        // Defence in depth: only call Anthropic for forms we expect to need it
        if (!needsAISummary(form, items)) {
          return Response.json(
            { ok: false, error: "Filing type not eligible for AI summary",
              form: form, items: items },
            { headers: cors });
        }
        const data = await summarizeFiling(env, tk, form, items, prim);
        // Only cache successful results (so failures don't poison the cache)
        if (data.ok) {
          const resp = Response.json(data, {
            headers: { ...cors, "Cache-Control": "public, max-age=604800" },
          });
          ctx.waitUntil(cache.put(request, resp.clone()));
          return resp;
        }
        return Response.json(data, { headers: cors });
      }
      // Live UOA flash — ?uoa-flash=NVDA returns top 25 unusual contracts
      // for that underlying as of <30 sec ago, sourced from Polygon's
      // options snapshot endpoint. Used by the ticker drilldown panel to
      // show what's flowing RIGHT NOW vs the latest 75-min batch.
      const uoaFlashParam = url.searchParams.get("uoa-flash");
      if (uoaFlashParam) {
        const cache = caches.default;
        const hit = await cache.match(request);
        if (hit) return hit;
        const tk = uoaFlashParam.trim().toUpperCase();
        if (!tk || !/^[A-Z.\-]{1,8}$/.test(tk)) {
          return Response.json(
            { error: `Invalid ticker: "${tk}"`, contracts: [] },
            { status: 400, headers: cors });
        }
        const debug = url.searchParams.get("debug") === "1";
        const data = await fetchLiveUOA(env, tk, debug);
        const resp = Response.json(data, {
          headers: {
            ...cors,
            // Skip caching when debug=1 so we get a fresh diagnostic
            "Cache-Control": debug
              ? "no-store"
              : "public, max-age=30",
          },
        });
        if (!debug) ctx.waitUntil(cache.put(request, resp.clone()));
        return resp;
      }
      // News headlines — ?news=general for firehose or ?news=AAPL for a
      // specific ticker. Optional ?limit=N (1..1000, default 50/200).
      const newsParam = url.searchParams.get("news");
      if (newsParam) {
        const cache = caches.default;
        const hit = await cache.match(request);
        if (hit) return hit;
        const tk = newsParam.trim();
        if (tk !== "general" && !/^[A-Z.\-]{1,8}$/i.test(tk)) {
          return Response.json(
            { error: `Invalid ticker: "${tk}"`, articles: [] },
            { status: 400, headers: cors });
        }
        const defaultLimit = tk === "general" ? 200 : 50;
        const data = await fetchPolygonNews(
          env, tk,
          url.searchParams.get("limit") || defaultLimit
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
