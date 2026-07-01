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
// ── Polygon snapshot (Stocks Starter tier) ─────────────────────────────
// Hits /v2/snapshot/locale/us/markets/stocks/tickers/{ticker} for the
// last-trade price + yesterday's official close. Same 15-min OPRA
// delay as Yahoo's passthrough but cleaner data — no stale-cache
// chartPreviousClose bug, deterministic timestamps, single source of
// truth on the previous-day close.
//
// Returned shape mirrors fetchYahooQuote so the call site can merge
// or pick the fresher of the two by last_trade_ts.
async function fetchPolygonSnapshot(sym, env) {
  if (!env || !env.POLYGON_API_KEY) return null;
  try {
    const r = await fetch(
      "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/" +
        encodeURIComponent(sym) + "?apiKey=" + env.POLYGON_API_KEY,
      { signal: AbortSignal.timeout(4000) }
    );
    if (!r.ok) return null;
    const j = await r.json();
    const t = j && j.ticker;
    if (!t) return null;
    const lastTrade = t.lastTrade || {};
    const prevDay   = t.prevDay   || {};
    const day       = t.day       || {};
    // Last-trade price; fall back to the day's most recent VWAP / close
    // if the lastTrade field is empty (illiquid names mid-session).
    const price = lastTrade.p != null ? lastTrade.p
                : day.c       != null ? day.c
                : day.vw      != null ? day.vw : null;
    const prev = prevDay.c != null ? prevDay.c : null;
    // sip_timestamp / .t — nanoseconds since epoch
    const tradeNs = lastTrade.t || 0;
    const lastTradeMs = tradeNs ? Math.round(tradeNs / 1e6) : null;
    const change = (price != null && prev != null && prev > 0)
      ? Math.round((price / prev - 1) * 10000) / 100 : null;
    return {
      symbol:        sym,
      price:         price,
      prevClose:     prev,
      change:        change,
      last_trade_ts: lastTradeMs
                       ? new Date(lastTradeMs).toISOString() : null,
      last_trade_ms: lastTradeMs,
      source:        "polygon",
    };
  } catch (_) { return null; }
}

// Current US-equity market phase from the ET wall clock (handles EDT/EST
// via Intl; Yahoo's chart-endpoint meta omits marketState, so we can't
// rely on it). pre = 04:00–09:30, regular = 09:30–16:00, post =
// 16:00–20:00, else closed. Weekends = closed.
function etMarketPhase() {
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York", hour12: false,
      weekday: "short", hour: "2-digit", minute: "2-digit",
    }).formatToParts(new Date());
    const get = function (t) {
      return (parts.find(function (x) { return x.type === t; }) || {}).value;
    };
    const wd = get("weekday");
    if (wd === "Sat" || wd === "Sun") return "closed";
    let hh = parseInt(get("hour"), 10);
    if (hh === 24) hh = 0;                       // hour12:false midnight edge
    const t = hh * 60 + parseInt(get("minute"), 10);
    if (t >= 240 && t < 570)  return "pre";      // 04:00–09:30
    if (t >= 570 && t < 960)  return "regular";  // 09:30–16:00
    if (t >= 960 && t < 1200) return "post";     // 16:00–20:00
    return "closed";
  } catch (_) { return "regular"; }              // safe default = RTH behavior
}
async function fetchYahooQuote(sym) {
  const r2 = function (x) { return Math.round(x * 100) / 100; };
  // Two parallel calls so we can derive a TRUSTWORTHY previous close.
  //   1d/5m  → intraday bars + current regularMarketPrice (real-time
  //            field, drives the spark + price display).
  //   5d/1d  → 5 most-recent daily bars; bars[-2].close is yesterday's
  //            real close. Yahoo's meta.chartPreviousClose / .previousClose
  //            return stale/garbage values for some symbols (MRVL was
  //            returning $219.43 vs. today's day-low of $252 — quote
  //            engine cache out of sync). Trusting one field gave
  //            +29% daily-return chips. Derive prev from the actual
  //            daily history instead.
  const sym2 = encodeURIComponent(sym);
  const intradayUrl = "https://query1.finance.yahoo.com/v8/finance/chart/" +
    sym2 + "?range=1d&interval=5m&includePrePost=true";
  const dailyUrl    = "https://query1.finance.yahoo.com/v8/finance/chart/" +
    sym2 + "?range=5d&interval=1d";
  const headers = { "User-Agent": "Mozilla/5.0" };
  try {
    const [r, rd] = await Promise.all([
      fetch(intradayUrl, { headers: headers, cf: { cacheTtl: 30 } }),
      fetch(dailyUrl,    { headers: headers, cf: { cacheTtl: 60 } }),
    ]);
    if (!r.ok) return { symbol: sym, price: null, change: null, bars: [] };
    const j = await r.json();
    const res = j && j.chart && j.chart.result && j.chart.result[0];
    const m = res && res.meta;
    if (!m) return { symbol: sym, price: null, change: null, bars: [] };
    const price = typeof m.regularMarketPrice === "number"
      ? m.regularMarketPrice : null;
    // Derive prev close from the daily-bars history. Take the close of
    // the bar BEFORE the most-recent one (most-recent = today's
    // partial-or-completed bar). Yahoo includes today as a bar in
    // range=5d/interval=1d once the session opens.
    let prev = null;
    try {
      if (rd.ok) {
        const jd = await rd.json();
        const resd = jd && jd.chart && jd.chart.result && jd.chart.result[0];
        const qd = resd && resd.indicators
                && resd.indicators.quote && resd.indicators.quote[0];
        const closes = (qd && Array.isArray(qd.close)) ? qd.close : null;
        if (closes && closes.length >= 2) {
          // Walk back from the end skipping nulls (the current bar may
          // not have closed yet — close[-1] could be null intraday).
          // bars[-1] = today, bars[-2] = yesterday's real close.
          for (let i = closes.length - 2; i >= 0; i--) {
            if (typeof closes[i] === "number" && closes[i] > 0) {
              prev = closes[i]; break;
            }
          }
        }
      }
    } catch (_) { /* fall through to meta fallback below */ }
    // Fallback to meta fields if the daily call failed entirely.
    // (Better to display a possibly-stale change than a NaN chip.)
    if (prev == null) {
      prev = m.chartPreviousClose || m.previousClose || null;
    }
    // NO sanity guard on the prev close — earnings gaps of +30%+ are
    // real (MRVL +29% on a blowout print) and the daily-bars derivation
    // above is the authoritative source: bars[-2].close = yesterday's
    // settlement. Suppressing legitimate big movers would have a worse
    // failure mode than displaying them.
    const change = (price != null && prev != null)
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
    // Freshness metadata so the client can disclose the 15m delay
    // truthfully rather than implying real-time. regularMarketTime is
    // the Yahoo-reported last-trade timestamp (epoch seconds);
    // multiply ×1000 for JS ms. source flags the upstream provider
    // (Yahoo passthrough = Polygon Stocks Starter tier in practice,
    // delayed 15 min by license). delay_minutes is advisory — clients
    // should display "Delayed 15m · last trade HH:MM ET" using these.
    const lastTradeMs = (typeof m.regularMarketTime === "number"
      && m.regularMarketTime > 0)
      ? m.regularMarketTime * 1000 : null;
    // ── Extended-hours (pre/post) awareness ──────────────────────────
    // During PRE / POST sessions Yahoo's regularMarketPrice is still the
    // prior regular close, so chips looked flat pre-market. Use the
    // latest pre/post print (last non-null close in the includePrePost
    // series) and recompute change off the SAME split-adjusted prev, so
    // every quote consumer (Desk chips, Watchlist, My Desk, drilldown)
    // reflects the real extended-hours move. RTH / closed are untouched.
    let effPrice = price, effChange = change, effLastMs = lastTradeMs, effPrev = prev;
    let extended = false;
    const phase = etMarketPhase();
    if (phase === "pre" || phase === "post") {
      // Prior REGULAR close basis: chartPreviousClose is correct pre/post
      // (the daily-bar derivation can miss yesterday when today's bar
      // isn't created yet pre-open). Falls back to the daily-derived prev.
      const xprev = (typeof m.chartPreviousClose === "number"
        && m.chartPreviousClose > 0) ? m.chartPreviousClose : prev;
      if (q && Array.isArray(q.close) && xprev) {
        const ts = Array.isArray(res.timestamp) ? res.timestamp : null;
        for (let i = q.close.length - 1; i >= 0; i--) {
          if (typeof q.close[i] === "number" && q.close[i] > 0) {
            effPrice  = r2(q.close[i]);
            effPrev   = r2(xprev);
            effChange = Math.round((q.close[i] / xprev - 1) * 10000) / 100;
            if (ts && typeof ts[i] === "number") effLastMs = ts[i] * 1000;
            extended  = true;
            break;
          }
        }
      }
    }
    // Stale-detection: if the last trade is older than 25 min wall
    // time AND it's a trading session, surface that so the client can
    // dim the chip. 25 min = 15-min license delay + 10-min generous
    // latency buffer. During off-hours we don't flag — quotes go
    // stale by design.
    const stale = effLastMs
      ? (Date.now() - effLastMs > 25 * 60 * 1000) : false;
    return { symbol: sym, price: effPrice, change: effChange,
             prevClose: effPrev, bars: bars,
             last_trade_ts: effLastMs
               ? new Date(effLastMs).toISOString() : null,
             // Wall-clock age of the last trade — lets the chip render
             // "as of 1:58 PM ET (3m ago)" or "Stale 28m" so the user
             // never confuses 15-min-delayed with real-time.
             last_trade_age_sec: effLastMs
               ? Math.max(0, Math.round((Date.now() - effLastMs) / 1000))
               : null,
             stale: stale,
             extended: extended,
             market_state: phase,
             source: extended ? "yahoo-ext" : "yahoo",
             delay_minutes: extended ? 0 : 15,
             fetched_at: new Date().toISOString() };
  } catch (e) {
    return { symbol: sym, price: null, change: null, bars: [],
             last_trade_ts: null, source: "yahoo",
             delay_minutes: 15,
             fetched_at: new Date().toISOString() };
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

/** Portfolio screenshot OCR — Claude Haiku 4.5 vision.
 *  Accepts a POST body { image_b64, media_type } (media_type one of
 *  image/png|jpeg|webp|gif) and returns
 *  { ok, positions: [{ ticker, weight, name }], usage }.
 *  weight = the position's market/dollar value when visible (so the
 *  imported watchlist reflects real sizing for benchmarking), else null.
 *  Everything the model can't read confidently is simply omitted — we
 *  never invent tickers. The client shows a review list before committing. */
const OCR_MEDIA_OK = {
  "image/png": 1, "image/jpeg": 1, "image/webp": 1, "image/gif": 1,
};
async function handlePortfolioOCR(request, env, cors) {
  if (!env.ANTHROPIC_API_KEY) {
    return Response.json(
      { ok: false, error: "Vision not configured (ANTHROPIC_API_KEY unset)." },
      { status: 500, headers: cors });
  }
  let payload;
  try { payload = await request.json(); }
  catch (e) {
    return Response.json({ ok: false, error: "Bad JSON body." },
      { status: 400, headers: cors });
  }
  let b64 = (payload && payload.image_b64) || "";
  const mediaType = (payload && payload.media_type) || "";
  // Tolerate a full data URL ("data:image/png;base64,AAAA…").
  const dataUrlMatch = /^data:([^;]+);base64,(.*)$/s.exec(b64);
  let mt = mediaType;
  if (dataUrlMatch) { mt = dataUrlMatch[1]; b64 = dataUrlMatch[2]; }
  if (!b64) {
    return Response.json({ ok: false, error: "No image provided." },
      { status: 400, headers: cors });
  }
  if (!OCR_MEDIA_OK[mt]) {
    return Response.json(
      { ok: false, error: "Unsupported image type: " + (mt || "unknown") +
        " (use PNG, JPEG, WEBP or GIF)." },
      { status: 400, headers: cors });
  }
  // Size guard — base64 inflates ~33%. ~9.3M chars ≈ 7MB decoded, and
  // Anthropic caps images at ~5MB, so reject early with a clear message.
  if (b64.length > 9_300_000) {
    return Response.json(
      { ok: false, error: "Image too large (max ~5MB). Crop or downscale it." },
      { status: 413, headers: cors });
  }
  const instr =
    "You are reading a screenshot of a stock brokerage account or " +
    "portfolio/watchlist (e.g. Interactive Brokers, Fidelity, Schwab, " +
    "Robinhood). Extract every distinct tradeable position so the user can " +
    "add the underlying tickers to a watchlist. For each, capture:\n" +
    "  - ticker: the US ticker symbol (uppercase, letters/dots/dashes, 1-6 " +
    "chars). For an OPTION line, use the UNDERLYING stock ticker only — e.g. " +
    "\"ONDS Aug21'26 9 CALL\" -> \"ONDS\", \"TE Sep18'26 12 CALL\" -> \"TE\". " +
    "ETFs (e.g. IBIT) are valid tickers, keep them. If only a company name " +
    "is shown and you are confident of its ticker, use it; otherwise omit.\n" +
    "  - weight: the position's market value / dollar value if a 'Market " +
    "Value' / 'Mkt Value' column is visible (number only, no $ or commas). " +
    "If no dollar value is shown, use null.\n" +
    "  - name: the company name if visible, else null.\n" +
    "IGNORE cash rows (TOTAL CASH, USD CASH, EUR CASH), account totals, " +
    "subtotals, and header/summary figures (Net Liquidity, Buying Power, " +
    "P&L). Do NOT guess or hallucinate tickers you cannot read. If the same " +
    "underlying appears more than once (e.g. shares plus an option), include " +
    "it once. Return ONLY a JSON object of the exact shape " +
    "{\"positions\":[{\"ticker\":\"NVDA\",\"weight\":12000,\"name\":\"NVIDIA\"}]} " +
    "with no prose, no markdown fences.";
  let j;
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
        max_tokens: 1500,
        messages: [{
          role: "user",
          content: [
            { type: "image",
              source: { type: "base64", media_type: mt, data: b64 } },
            { type: "text", text: instr },
          ],
        }],
      }),
    });
    if (!r.ok) {
      const errText = await r.text();
      return Response.json(
        { ok: false, error: "vision " + r.status + ": " + errText.slice(0, 200) },
        { status: 502, headers: cors });
    }
    j = await r.json();
  } catch (e) {
    return Response.json({ ok: false, error: "vision call: " + String(e) },
      { status: 502, headers: cors });
  }
  // Parse the model's JSON. It should be a bare object, but strip any
  // stray fences / prose defensively before JSON.parse.
  const raw = (j.content && j.content[0] && j.content[0].text || "").trim();
  let positions = [];
  try {
    const cleaned = raw.replace(/^```(?:json)?/i, "").replace(/```$/i, "").trim();
    const start = cleaned.indexOf("{");
    const end = cleaned.lastIndexOf("}");
    const slice = (start >= 0 && end > start) ? cleaned.slice(start, end + 1) : cleaned;
    const parsed = JSON.parse(slice);
    const arr = Array.isArray(parsed) ? parsed : (parsed.positions || []);
    for (const p of arr) {
      if (!p || typeof p !== "object") continue;
      const tk = String(p.ticker || "").toUpperCase().trim();
      if (!/^[A-Z][A-Z.\-]{0,5}$/.test(tk)) continue;
      let wt = null;
      if (p.weight != null && p.weight !== "") {
        const n = parseFloat(String(p.weight).replace(/[$,\s]/g, ""));
        if (isFinite(n) && n > 0) wt = n;
      }
      positions.push({ ticker: tk,
        weight: wt,
        name: p.name ? String(p.name).slice(0, 60) : null });
    }
  } catch (e) {
    return Response.json(
      { ok: false, error: "Could not parse positions from the image.",
        raw: raw.slice(0, 300) },
      { status: 502, headers: cors });
  }
  // Dedupe by ticker (keep first / largest weight seen).
  const byTk = {};
  for (const p of positions) {
    const cur = byTk[p.ticker];
    if (!cur) { byTk[p.ticker] = p; }
    else if ((p.weight || 0) > (cur.weight || 0)) { byTk[p.ticker] = p; }
  }
  return Response.json(
    { ok: true, positions: Object.values(byTk), usage: j.usage || null },
    { headers: cors });
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
    // price_basis makes the mid-price provenance explicit. Polygon
    // Options Starter frequently returns NO bid/ask, so the "mid" can
    // fall back to last trade or the day close — label which one so the
    // UI never implies a true bid/ask midpoint when there isn't one.
    let mid, priceBasis;
    if (lq.ask && lq.bid) {
      mid = (lq.ask + lq.bid) / 2; priceBasis = "quote_mid";
    } else if (lq.last) {
      mid = lq.last; priceBasis = "last_trade";
    } else if (day.close) {
      mid = day.close; priceBasis = "day_close";
    } else {
      mid = 0; priceBasis = "none";
    }
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
      price_basis: priceBasis,
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
    // Data-provenance metadata — honest about the upstream feed.
    source:           "polygon_options_starter",
    delay_minutes:    15,
    // Per-contract premium uses mid from quote when available, else
    // last trade, else day close — see each row's price_basis field.
    price_basis_note: "Per-contract mid: quote_mid > last_trade > day_close (see row.price_basis)",
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

/** ATM straddle → implied-move snapshot for a single underlying.
 *
 *  How it works:
 *    1. Pulls /v3/snapshot/options/{ticker} (same endpoint as live-UOA)
 *    2. Picks the nearest expiry (>= today) — this is the most relevant
 *       contract for earnings/event positioning. If a specific
 *       earnings-date hint is supplied via ?earningsDate=, we pick the
 *       FIRST expiry on-or-after that date instead so the user sees
 *       the move the market is pricing INTO the print.
 *    3. Walks the chain for that expiry, picks the call+put pair
 *       closest to the underlying spot (ATM).
 *    4. implied_move_pct = (atm_call_mid + atm_put_mid) / spot * 100
 *       — the classic ATM-straddle approximation of 1σ expected move
 *       over the contract's life. Honest math, no IV blends.
 *    5. iv_pct = avg of the call+put implied vols from the snapshot
 *       (Polygon emits per-contract IV on the snapshot).
 *
 *  Why "IV level" not "IV rank":
 *  IV rank requires 52-week IV history per ticker which Polygon Starter
 *  doesn't ship and which our cache doesn't yet backfill. So we report
 *  raw IV ("47%") plus a coarse Low/Normal/High label keyed off the
 *  absolute level. When we accumulate enough daily snapshots to build a
 *  true rank, the endpoint can swap in a percentile.
 *
 *  Edge cached 5 min — IV doesn't move that fast and this is called
 *  from earnings cards (potentially dozens per page load).
 */
async function fetchIvSnapshot(env, ticker, earningsDate) {
  if (!env.POLYGON_API_KEY) {
    return { error: "POLYGON_API_KEY not configured", ticker: ticker };
  }
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
      cf: { cacheTtl: 300 },
    });
  } catch (e) {
    return { error: "Polygon snapshot timeout", ticker: ticker };
  }
  if (!r.ok) {
    let body = "";
    try { body = await r.text(); } catch (e) {}
    return { error: "Polygon snapshot HTTP " + r.status,
             body: body.slice(0, 200), ticker: ticker };
  }
  const j = await r.json();
  const results = (j && j.results) || [];
  if (!results.length) {
    return { ticker: ticker, error: "No active options chain",
             fetched: new Date().toISOString() };
  }
  // Underlying spot
  let spot = null;
  for (const c of results) {
    if (c.underlying_asset && typeof c.underlying_asset.price === "number") {
      spot = c.underlying_asset.price; break;
    }
  }
  if (!spot) {
    return { ticker: ticker, error: "Underlying price unavailable",
             fetched: new Date().toISOString() };
  }
  // Group by expiry
  const byExpiry = {};
  for (const c of results) {
    const d = c.details || {};
    if (!d.expiration_date || !d.strike_price || !d.contract_type) continue;
    const e = d.expiration_date;
    (byExpiry[e] = byExpiry[e] || []).push(c);
  }
  // Choose the target expiry
  const expiries = Object.keys(byExpiry).sort();
  if (!expiries.length) {
    return { ticker: ticker, error: "No usable expiries", spot: spot };
  }
  let pick = expiries[0];
  if (earningsDate) {
    const later = expiries.find(function (e) { return e >= earningsDate; });
    if (later) pick = later;
  }
  const chain = byExpiry[pick];
  // Find ATM call + ATM put
  const closest = function (typ) {
    let best = null, bestDist = Infinity;
    for (const c of chain) {
      const d = c.details || {};
      if (d.contract_type !== typ) continue;
      const dist = Math.abs((d.strike_price || 0) - spot);
      if (dist < bestDist) { best = c; bestDist = dist; }
    }
    return best;
  };
  const call = closest("call");
  const put  = closest("put");
  if (!call || !put) {
    return { ticker: ticker, error: "No ATM straddle on " + pick,
             spot: spot, expiry: pick };
  }
  // Returns {mid, basis} so we can disclose how each leg was priced.
  // Polygon Starter often omits bid/ask, so the straddle mid can fall
  // back to day close or last trade — surface which.
  const midOf = function (c) {
    const lq = c.last_quote || {};
    if (lq.ask && lq.bid && lq.ask + lq.bid > 0) {
      return { mid: (lq.ask + lq.bid) / 2, basis: "quote_mid" };
    }
    const dy = c.day || {};
    if (dy.close) return { mid: dy.close, basis: "day_close" };
    if (lq.last) return { mid: lq.last, basis: "last_trade" };
    return { mid: 0, basis: "none" };
  };
  const callPx = midOf(call), putPx = midOf(put);
  const callMid = callPx.mid, putMid = putPx.mid;
  // Combined basis: if both legs used a true quote mid, report quote_mid;
  // otherwise report the weaker leg's basis so the user isn't misled.
  const BASIS_RANK = { quote_mid: 0, last_trade: 1, day_close: 2, none: 3 };
  const priceBasis = (BASIS_RANK[callPx.basis] >= BASIS_RANK[putPx.basis])
    ? callPx.basis : putPx.basis;
  const straddle = callMid + putMid;
  const impliedMovePct = spot > 0 ? (straddle / spot) * 100 : null;
  const ivCall = (typeof call.implied_volatility === "number")
    ? call.implied_volatility : null;
  const ivPut  = (typeof put.implied_volatility === "number")
    ? put.implied_volatility  : null;
  const ivs = [ivCall, ivPut].filter(function (v) { return v != null; });
  const ivPct = ivs.length
    ? (ivs.reduce(function (s, v) { return s + v; }, 0) / ivs.length) * 100
    : null;
  // Coarse level label keyed off absolute IV (NOT rank — we don't have
  // 52w history yet). Thresholds chosen for liquid US single-name
  // equity options where 30% IV is roughly average. Reset these once
  // we have ticker-specific history.
  const ivLevel = ivPct == null ? null
    : ivPct >= 70 ? "high"
    : ivPct >= 40 ? "elevated"
    : ivPct >= 20 ? "normal"
    : "low";
  // DTE for the picked expiry — helps the UI label "weekly" vs "monthly"
  const expMs = Date.parse(pick + "T16:00:00-04:00");
  const dte = expMs ? Math.max(0, Math.round(
    (expMs - Date.now()) / 86400000)) : null;
  return {
    ticker:           ticker,
    spot:             Math.round(spot * 100) / 100,
    expiry:           pick,
    dte:              dte,
    atm_call_strike:  call.details.strike_price,
    atm_put_strike:   put.details.strike_price,
    call_mid:         Math.round(callMid * 100) / 100,
    put_mid:          Math.round(putMid  * 100) / 100,
    straddle:         Math.round(straddle * 100) / 100,
    implied_move_pct: impliedMovePct == null ? null
                      : Math.round(impliedMovePct * 10) / 10,
    iv_pct:           ivPct == null ? null
                      : Math.round(ivPct * 10) / 10,
    iv_level:         ivLevel,
    // Honest disclosure of how the IV level is bucketed — keeps the
    // client copy from over-claiming IV-rank semantics. Static while
    // we lack history.
    iv_level_method:  "absolute (Low <20 · Normal 20-40 · Elevated 40-70 · High ≥70)",
    fetched:          new Date().toISOString(),
    // Data-provenance metadata — honest about the upstream feed +
    // how the straddle mid was priced (quote_mid / day_close /
    // last_trade — Polygon Starter often lacks bid/ask).
    source:           "polygon_options_starter",
    delay_minutes:    15,
    price_basis:      priceBasis,
  };
}

/** Live Flow feed — polled "notable flow" across a curated universe.
 *
 *  Fans out to /v3/snapshot/options/{TICKER} for the top liquid options
 *  underlyings (default list below; can be augmented via ?tickers=).
 *  Filters each chain to contracts whose aggregate-day premium >=
 *  min_premium (default $1M) and whose last trade landed within
 *  freshness_min minutes. Returns ranked-most-recent-first list of
 *  matching contracts, each tagged with its last-trade ET timestamp.
 *
 *  Important honesty disclosure: this is NOT true real-time. Polygon
 *  Options Starter delivers 15-min-delayed quotes/trades. The endpoint
 *  itself runs every poll (no caching of "since X" state), but the
 *  underlying data lags the tape. For true real-time the worker would
 *  need WebSocket via Polygon Advanced tier — separate roadmap.
 *
 *  Edge cached 30s so multiple concurrent users sharing the universe
 *  don't multiply Polygon API calls. Per-poll cost = ~50 snapshots.
 */
// Live Flow universe — ~180 names. Mega-caps for context (NVDA-sized
// $1M+ sweeps), PLUS the SMID/high-beta names where UOA actually has
// edge (a $200K bet on a $3B mcap is unusual; the same notional on
// NVDA is rounding). Aim for the cohort the batch UOA scanner finds
// signal in. Capped well below Polygon's per-poll throughput so a
// 60-sec cadence doesn't hit rate limits.
const LF_DEFAULT_UNIVERSE = [
  // Mega-cap indices + leaders
  "SPY","QQQ","IWM","DIA",
  // Recent high-volume listings (added on request — covered for live flow
  // immediately; swing/momentum grades lag until ~50 sessions of history).
  "SPCX",
  // Mag 7 / AI bellwethers
  "NVDA","AAPL","MSFT","GOOGL","AMZN","META","TSLA",
  // Semis + AI infra
  "AMD","AVGO","MU","MRVL","INTC","TSM","ASML","LRCX","AMAT","KLAC",
  "ARM","SMCI","COHR","ANET","CRWV","ALAB","NBIS","AEVA","NVTS",
  "ONTO","ENTG","WOLF","NXPI","ON","TXN","ADI","MCHP","QCOM","SWKS",
  // Software / cloud / data
  "ORCL","CRM","SNOW","PLTR","NOW","DDOG","NET","CRWD","PANW","ZS",
  "MDB","S","ESTC","HUBS","TEAM","WDAY","INTU","ADBE","SHOP","FROG",
  "GTLB","DOCN","BOX","AI","BBAI","SOUN","PATH","UPST","APP","RDDT",
  // Hardware / connectivity
  "DELL","HPE","CSCO","NTAP","STX","WDC","PSTG","JNPR",
  // Megacap finance + brokerage
  "JPM","BAC","GS","MS","WFC","C","SCHW","V","MA","AXP","BLK","COIN",
  "HOOD","SOFI","NU","UPST","AFRM","SQ","PYPL",
  // Energy + miners (high-beta UOA targets)
  "XOM","CVX","COP","SLB","OXY","DVN","FANG","EOG","HAL","BKR",
  "MRO","CLR","CCJ","UEC","UUUU","LEU","NXE",
  // Healthcare / pharma / biotech high-beta
  "LLY","UNH","JNJ","NVO","PFE","MRK","BMY","GILD","AMGN","REGN",
  "VRTX","BIIB","MRNA","BNTX","CRSP","BEAM","NTLA","EDIT","RNA",
  // Retail / consumer / discretionary
  "WMT","COST","HD","NKE","SBUX","MCD","DIS","TGT","LOW","TJX",
  "ULTA","CMG","DASH","ABNB","UBER","LYFT","CVNA","CHWY","DKNG",
  "MELI","BABA","JD","PDD","NIO","XPEV","LI","RIVN","LCID",
  // Crypto / blockchain proxies (high vol/oi spikes common)
  "MSTR","RIOT","MARA","CLSK","WULF","HUT","IREN","BITF","CIFR",
  "BITX","IBIT","ETHA",
  // Quantum / space / defense (smaller mcap, UOA-prone)
  "RKLB","ASTS","JOBY","ACHR","BLDE","RGTI","IONQ","QUBT","QBTS",
  "BA","LMT","RTX","NOC","GD","LHX","HII","KTOS","LDOS","AVAV","RKT",
  // Nuclear / power / clean energy momentum
  "OKLO","SMR","NXT","FSLR","ENPH","RUN","SEDG","TSLR","VST","CEG",
  "TLN","NRG","NEE","BE",
  // Healthcare / wellness consumer (UOA targets)
  "HIMS","LFMD","OSCR","RDDT","NU",
  // Other high-beta single names that show up in batch UOA frequently
  "GME","BBBY","AMC","RBLX","NFLX","ROKU","SPOT","SNAP","PINS","ETSY",
  "TWLO","ZM","DOCN","FROG","WOLF","ENVX","JOBY","NKLA","FUBO","OPEN",
];

// Bounded-concurrency map: run fn over items with at most `limit` in
// flight. Firing all ~180 option-snapshot calls at once made Polygon
// throttle the burst, and Promise.all waits for the slowest — so one
// rate-limited straggler dragged the whole /live-flow response to the
// 8s timeout. Pacing to ~20 keeps us under the rate limit while still
// finishing in a few waves. Order preserved; per-item errors isolated.
async function mapPool(items, limit, fn) {
  const out = new Array(items.length);
  let next = 0;
  async function worker() {
    while (next < items.length) {
      const i = next++;
      out[i] = await fn(items[i], i);
    }
  }
  const n = Math.max(1, Math.min(limit, items.length));
  await Promise.all(Array.from({ length: n }, worker));
  return out;
}
async function fetchLiveFlow(env, opts) {
  opts = opts || {};
  // Universe — default to LF_DEFAULT_UNIVERSE (~180 names), capped at
  // 200 to keep per-poll cost bounded. Watchlist additions append to
  // the front (passed via opts.tickers) so user names are never dropped.
  const tickers = (opts.tickers && opts.tickers.length
    ? opts.tickers : LF_DEFAULT_UNIVERSE).slice(0, 200);
  // EITHER-OR filter: pass a contract if its premium clears minPrem OR
  // its volume / open-interest ratio clears minVolOi. The vol/oi gate
  // is what catches small-cap UOA — a $80K premium fill that's also
  // 12× the existing open interest is a textbook "someone knows
  // something" signal. The legacy code required premium-only, which
  // silently dropped every small-cap UOA print.
  const minPrem  = Math.max(0, +(opts.minPremium || 100_000));
  const minVolOi = Math.max(0, +(opts.minVolOi   || 2.0));
  const freshnessMin = Math.max(1, +(opts.freshnessMin || 90));
  const limit = Math.min(200, Math.max(10, +(opts.limit || 100)));
  if (!env.POLYGON_API_KEY) {
    return { error: "POLYGON_API_KEY not configured", flows: [] };
  }
  const today = new Date().toISOString().slice(0, 10);
  const freshnessCutoff = Date.now() - freshnessMin * 60 * 1000;
  // Fan out to Polygon snapshots. Concurrent fetches handled by the
  // Workers runtime (no manual queue needed for ~50 parallel HTTP calls).
  // Per-ticker errors don't fail the whole batch — just contribute zero
  // contracts to the feed.
  async function snap(tk) {
    try {
      const r = await fetch(
        "https://api.polygon.io/v3/snapshot/options/" +
          encodeURIComponent(tk) +
          "?limit=250&expiration_date.gte=" + today +
          "&apiKey=" + env.POLYGON_API_KEY,
        { signal: AbortSignal.timeout(4000) }
      );
      if (!r.ok) return [];
      const j = await r.json();
      return (j.results || []).map(function (c) { return [tk, c]; });
    } catch (_) { return []; }
  }
  // Paced fan-out (≤20 concurrent) instead of all-at-once — avoids the
  // Polygon throttle that was pinning latency at the 8s timeout.
  const allArrays = await mapPool(tickers, 20, snap);
  const all = [].concat.apply([], allArrays);
  // Per-contract filter + transform to the feed-friendly shape.
  const flows = [];
  for (const [tk, c] of all) {
    const d = c.details || {};
    const day = c.day || {};
    const lq = c.last_quote || {};
    const lt = c.last_trade || {};
    if (!d.contract_type || !d.strike_price || !d.expiration_date) continue;
    const vol = day.volume || 0;
    const oi  = c.open_interest || 0;
    // Liquidity floor — drop true noise (~10 lots / no chain). 200 is
    // the same floor uoa_scanner.py uses for its batch screen so the
    // live + batch surfaces are visually consistent.
    if (vol < 200 || oi < 10) continue;
    const last = lt.price || day.close || 0;
    const premium = vol * last * 100;
    const voi = oi > 0 ? vol / oi : 0;
    // EITHER threshold passes the contract through. Drops only if both
    // gates fail — i.e., the contract is small-premium AND
    // small-vol/oi (which is just non-event flow).
    if (premium < minPrem && voi < minVolOi) continue;
    // Use sip_timestamp (nanoseconds since epoch) for last-trade time.
    // Polygon Options Starter clamps this to 15-min-delayed, but the
    // RELATIVE recency between rows is still meaningful — newer last
    // trades show as more recent in the feed.
    const sip = lt.sip_timestamp;
    const tradeMs = sip ? Math.round(sip / 1e6) : null;
    if (tradeMs && tradeMs < freshnessCutoff) continue;
    // Direction inference — combine type + bid/ask context.
    let direction = "mixed";
    if (lq.bid && lq.ask && (lq.bid + lq.ask) > 0) {
      const mid = (lq.bid + lq.ask) / 2;
      // last price >= mid → ask-side aggression = bull on call, bear on put
      const askSide = last >= mid;
      if (d.contract_type === "call") {
        direction = askSide ? "bullish" : "bearish";
      } else if (d.contract_type === "put") {
        direction = askSide ? "bearish" : "bullish";
      }
    } else {
      // No quote — default by contract type (weak signal but better than null)
      direction = (d.contract_type === "call") ? "bullish" : "bearish";
    }
    const dte = (function () {
      const exp = new Date(d.expiration_date + "T16:00:00-04:00");
      return Math.max(0, Math.round((exp - Date.now()) / 86400000));
    })();
    // voi computed above for the threshold gate; reused for the tier.
    //
    // Tier — what KIND of unusual is this? Order matters: each row
    // gets the strongest tier it qualifies for.
    //   golden  : premium >= $250K AND vol/oi >= 5 AND dte <= 45
    //             — textbook UOA: real money + new positioning + near
    //             enough to act before the catalyst.
    //   big     : premium >= $1M — classic "mega-cap institutional sweep"
    //             (used to be the only thing the live feed surfaced;
    //             still useful but not the only signal).
    //   unusual : vol/oi >= 5 — small-cap edge. The whole reason this
    //             feed exists: a $120K fill on a $3B mcap that's 14×
    //             the contract's existing OI = someone has conviction.
    //   active  : passed the EITHER gate but didn't hit any above. Day
    //             flow worth a glance.
    let tier = "active";
    if (premium >= 250_000 && voi >= 5 && dte <= 45) tier = "golden";
    else if (premium >= 1_000_000)                   tier = "big";
    else if (voi >= 5)                                tier = "unusual";
    flows.push({
      ticker:        tk,
      contract:      d.ticker || "",
      type:          d.contract_type,
      strike:        d.strike_price,
      expiry:        d.expiration_date,
      dte:           dte,
      volume:        vol,
      open_interest: oi,
      vol_oi:        Math.round(voi * 10) / 10,
      premium:       Math.round(premium),
      last_price:    last,
      bid:           lq.bid || null,
      ask:           lq.ask || null,
      iv:            c.implied_volatility || null,
      direction:     direction,
      last_trade_ts: tradeMs,
      tier:          tier,
      // Flag types — sweep / golden / earnings-positioned aren't known
      // at this endpoint level (no flow analysis here). The client can
      // cross-ref against uoa_latest.json on its end.
      spot:          (c.underlying_asset && c.underlying_asset.price) || null,
    });
  }
  // Newest fills first; tie-break by premium descending.
  flows.sort(function (a, b) {
    if (b.last_trade_ts !== a.last_trade_ts) {
      return (b.last_trade_ts || 0) - (a.last_trade_ts || 0);
    }
    return (b.premium || 0) - (a.premium || 0);
  });
  return {
    fetched:        new Date().toISOString(),
    // Provenance metadata — present even when flows is empty, so the
    // client can always disclose the feed's source + delay honestly.
    source:         "polygon_options_starter",
    delay_minutes:  15,
    price_basis:    "last_trade",
    universe_size:  tickers.length,
    scanned:        all.length,
    min_premium:    minPrem,
    min_vol_oi:     minVolOi,
    freshness_min:  freshnessMin,
    // Per-tier counts so the UI can show "12 golden / 4 big / 31 unusual"
    // without re-iterating client-side.
    tier_counts: flows.reduce(function (acc, f) {
      acc[f.tier] = (acc[f.tier] || 0) + 1; return acc;
    }, {}),
    license_note:   "Polygon Options Starter — quotes delayed 15 min. " +
                    "True real-time requires Advanced tier upgrade.",
    flows:          flows.slice(0, limit),
  };
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

// ── Stripe Checkout integration ───────────────────────────────────
// Creates a Stripe Checkout Session for a Pro or Premium subscription
// and returns the hosted-checkout URL. The frontend redirects the
// browser there; Stripe handles all card collection + PCI.
//
// Required env (Cloudflare worker secrets):
//   STRIPE_SECRET_KEY       sk_live_... or sk_test_...
//   STRIPE_PRO_PRICE_ID     price_... for Pro $29/mo
//   STRIPE_PREMIUM_PRICE_ID price_... for Premium $99/mo
//   STRIPE_WEBHOOK_SECRET   whsec_... for signature verification
//
// On success (post-checkout), Stripe fires customer.subscription.created
// to /stripe/webhook which updates profiles.subscription_tier.

async function handleStripeCheckout(request, env, cors) {
  if (!env.STRIPE_SECRET_KEY) {
    return Response.json(
      { ok: false, error: "Stripe not configured (STRIPE_SECRET_KEY missing in worker secrets)" },
      { status: 500, headers: cors }
    );
  }
  let body;
  try {
    body = await request.json();
  } catch {
    return Response.json({ ok: false, error: "Invalid JSON" },
                         { status: 400, headers: cors });
  }
  const plan = String(body.plan || "").toLowerCase();
  const userIdClaim = String(body.user_id || "");
  const email = String(body.email || "");
  if (!plan || !userIdClaim) {
    return Response.json(
      { ok: false, error: "plan and user_id are required" },
      { status: 400, headers: cors }
    );
  }
  // Validate that the caller actually owns the user_id they're claiming.
  // Without this, anyone could POST a checkout session pointing at
  // someone else's user_id and (if they paid) effectively "donate" a
  // subscription to a stranger. Require a Bearer token from the
  // Authorization header, validate it via Supabase /auth/v1/user, and
  // ensure the validated user.id matches body.user_id.
  const authHeader = request.headers.get("Authorization") || "";
  const bearer = authHeader.startsWith("Bearer ") ? authHeader.slice(7) : "";
  if (!bearer || !env.SUPABASE_URL) {
    return Response.json(
      { ok: false, error: "Sign in required to start checkout." },
      { status: 401, headers: cors }
    );
  }
  let userId = userIdClaim;
  try {
    const userResp = await fetch(
      env.SUPABASE_URL + "/auth/v1/user",
      { headers: {
          "Authorization": "Bearer " + bearer,
          "apikey": env.SUPABASE_ANON_KEY || env.SUPABASE_SERVICE_KEY || "",
      }}
    );
    if (!userResp.ok) {
      return Response.json(
        { ok: false, error: "Sign-in expired. Please sign in again." },
        { status: 401, headers: cors }
      );
    }
    const userBody = await userResp.json();
    if (!userBody || !userBody.id || userBody.id !== userIdClaim) {
      return Response.json(
        { ok: false, error: "Auth mismatch — user_id does not match signed-in user." },
        { status: 403, headers: cors }
      );
    }
    userId = userBody.id;
  } catch (e) {
    return Response.json(
      { ok: false, error: "Auth validation failed: " + e.message },
      { status: 502, headers: cors }
    );
  }
  const priceId = plan === "pro" ? env.STRIPE_PRO_PRICE_ID
                : plan === "premium" ? env.STRIPE_PREMIUM_PRICE_ID
                : null;
  if (!priceId) {
    return Response.json(
      { ok: false, error: `Unknown plan: "${plan}". Use "pro" or "premium".` },
      { status: 400, headers: cors }
    );
  }
  // Build form-urlencoded body for Stripe API (it expects this, not JSON)
  const params = new URLSearchParams();
  params.set("mode", "subscription");
  params.set("line_items[0][price]", priceId);
  params.set("line_items[0][quantity]", "1");
  params.set("client_reference_id", userId);
  if (email) params.set("customer_email", email);
  params.set("success_url",
    "https://tickerdesk.io/?subscribed=1&plan=" + plan);
  params.set("cancel_url", "https://tickerdesk.io/?subscribe_cancel=1");
  // Allow promotion codes so we can run discounts later
  params.set("allow_promotion_codes", "true");
  // Store plan + user_id in metadata so the webhook can update Supabase
  params.set("metadata[user_id]", userId);
  params.set("metadata[plan]", plan);
  params.set("subscription_data[metadata][user_id]", userId);
  params.set("subscription_data[metadata][plan]", plan);

  try {
    const r = await fetch("https://api.stripe.com/v1/checkout/sessions", {
      method: "POST",
      headers: {
        "Authorization": "Bearer " + env.STRIPE_SECRET_KEY,
        "Content-Type": "application/x-www-form-urlencoded",
      },
      body: params.toString(),
    });
    if (!r.ok) {
      const txt = await r.text();
      return Response.json(
        { ok: false, error: "Stripe API error: " + txt.slice(0, 500) },
        { status: 502, headers: cors }
      );
    }
    const session = await r.json();
    return Response.json(
      { ok: true, url: session.url, session_id: session.id },
      { headers: cors }
    );
  } catch (e) {
    return Response.json(
      { ok: false, error: "Stripe call failed: " + String(e) },
      { status: 502, headers: cors }
    );
  }
}

// ── Stripe Customer Portal ────────────────────────────────────────
// Self-serve billing management for subscribers: cancel, change card,
// update payment method, view invoices, switch plans. Creates a
// billing_portal.Session and returns the URL for the frontend to
// redirect to. Stripe handles the entire portal UI.
//
// Auth: requires a Supabase Bearer JWT (Authorization header) so we
// can confidently tie this request to a specific user. We then trust
// the user_id from /auth/v1/user — body.email is optional, only used
// as a fallback search hint if profiles.stripe_customer_id is null.
//
// Lookup chain to find the customer:
//   1. Read profiles.stripe_customer_id (cached on first checkout via
//      the webhook handler below). One DB hit, zero Stripe API calls.
//   2. If null, fall back to Stripe customers/search by email AND
//      write the resolved id back to profiles so the next portal
//      open is fast + correct.
//   3. If both fail, return 404 with a helpful message — most likely
//      the user is on a promo/trial path and has no Stripe customer
//      record yet (nothing to manage in the portal).
async function handleStripePortal(request, env, cors) {
  if (!env.STRIPE_SECRET_KEY) {
    return Response.json(
      { ok: false, error: "Stripe not configured" },
      { status: 500, headers: cors }
    );
  }
  let body;
  try { body = await request.json(); }
  catch { return Response.json({ ok: false, error: "Invalid JSON" },
                                { status: 400, headers: cors }); }
  // JWT-required: this opens billing UI for a real account, so we
  // refuse to look up customers without a verified sign-in.
  const authHeader = request.headers.get("Authorization") || "";
  const bearer = authHeader.startsWith("Bearer ") ? authHeader.slice(7) : "";
  if (!bearer || !env.SUPABASE_URL || !env.SUPABASE_SERVICE_KEY) {
    return Response.json(
      { ok: false, error: "Sign in required to manage billing." },
      { status: 401, headers: cors }
    );
  }
  let userId = null;
  let userEmail = String(body.email || "").trim();
  try {
    const userResp = await fetch(
      env.SUPABASE_URL + "/auth/v1/user",
      { headers: {
          "Authorization": "Bearer " + bearer,
          "apikey": env.SUPABASE_ANON_KEY || env.SUPABASE_SERVICE_KEY || "",
      }}
    );
    if (!userResp.ok) {
      return Response.json(
        { ok: false, error: "Sign-in expired. Please sign in again." },
        { status: 401, headers: cors }
      );
    }
    const userBody = await userResp.json();
    if (!userBody || !userBody.id) {
      return Response.json(
        { ok: false, error: "Auth validation failed (no user id)." },
        { status: 401, headers: cors }
      );
    }
    userId = userBody.id;
    // Prefer the verified email from the JWT over whatever the
    // client sent in the body (which could be stale).
    if (userBody.email) userEmail = String(userBody.email);
  } catch (e) {
    return Response.json(
      { ok: false, error: "Auth validation failed: " + String(e) },
      { status: 502, headers: cors }
    );
  }

  // 1. Try cached stripe_customer_id on profiles
  let customerId = null;
  const profilesBase = env.SUPABASE_URL.replace(/\/+$/, "") +
    "/rest/v1/profiles";
  try {
    const pr = await fetch(
      profilesBase + "?id=eq." + encodeURIComponent(userId) +
        "&select=stripe_customer_id",
      { headers: {
          "apikey": env.SUPABASE_SERVICE_KEY,
          "Authorization": "Bearer " + env.SUPABASE_SERVICE_KEY,
      }}
    );
    if (pr.ok) {
      const rows = await pr.json();
      if (rows && rows[0] && rows[0].stripe_customer_id) {
        customerId = String(rows[0].stripe_customer_id);
      }
    }
  } catch (_) { /* fall through to email lookup */ }

  // 2. Fallback: Stripe customers/search by email
  if (!customerId && userEmail) {
    try {
      const sr = await fetch(
        "https://api.stripe.com/v1/customers/search?query=" +
          encodeURIComponent('email:"' + userEmail + '"') + "&limit=1",
        {
          headers: {
            "Authorization": "Bearer " + env.STRIPE_SECRET_KEY,
            "Content-Type": "application/x-www-form-urlencoded",
          },
        }
      );
      if (sr.ok) {
        const sd = await sr.json();
        if (sd.data && sd.data.length) customerId = sd.data[0].id;
      }
    } catch (e) {
      return Response.json(
        { ok: false, error: "Customer lookup failed: " + String(e) },
        { status: 502, headers: cors }
      );
    }
    // Back-fill profiles.stripe_customer_id so future calls hit the
    // fast path. Best-effort — don't block portal open on failure.
    if (customerId) {
      try {
        await fetch(
          profilesBase + "?id=eq." + encodeURIComponent(userId),
          { method: "PATCH",
            headers: {
              "apikey": env.SUPABASE_SERVICE_KEY,
              "Authorization": "Bearer " + env.SUPABASE_SERVICE_KEY,
              "Content-Type": "application/json",
              "Prefer": "return=minimal",
            },
            body: JSON.stringify({ stripe_customer_id: customerId }),
          }
        );
      } catch (_) {}
    }
  }

  if (!customerId) {
    return Response.json(
      { ok: false,
        error: "No Stripe customer record yet — you're either on a " +
          "promo/trial plan or haven't completed a paid checkout. " +
          "There's nothing to manage in the billing portal until your " +
          "first paid subscription." },
      { status: 404, headers: cors }
    );
  }
  // Create portal session
  const params = new URLSearchParams();
  params.set("customer", customerId);
  params.set("return_url", "https://tickerdesk.io/?portal_returned=1");
  try {
    const r = await fetch(
      "https://api.stripe.com/v1/billing_portal/sessions",
      {
        method: "POST",
        headers: {
          "Authorization": "Bearer " + env.STRIPE_SECRET_KEY,
          "Content-Type": "application/x-www-form-urlencoded",
        },
        body: params.toString(),
      }
    );
    if (!r.ok) {
      const txt = await r.text();
      return Response.json(
        { ok: false, error: "Portal create failed: " + txt.slice(0, 400) },
        { status: 502, headers: cors }
      );
    }
    const session = await r.json();
    return Response.json(
      { ok: true, url: session.url },
      { headers: cors }
    );
  } catch (e) {
    return Response.json(
      { ok: false, error: "Portal call failed: " + String(e) },
      { status: 502, headers: cors }
    );
  }
}

async function handleStripeWebhook(request, env, cors) {
  // Diagnostic logging so failures show up in `wrangler tail` and the
  // Cloudflare dashboard Logs tab. Never logs the secret itself.
  const has = {
    STRIPE_SECRET_KEY: !!env.STRIPE_SECRET_KEY,
    STRIPE_WEBHOOK_SECRET: !!env.STRIPE_WEBHOOK_SECRET,
    SUPABASE_URL: !!env.SUPABASE_URL,
    SUPABASE_SERVICE_KEY: !!env.SUPABASE_SERVICE_KEY,
  };
  console.log("Stripe webhook hit · env present:", JSON.stringify(has));
  if (!env.STRIPE_SECRET_KEY || !env.STRIPE_WEBHOOK_SECRET ||
      !env.SUPABASE_URL || !env.SUPABASE_SERVICE_KEY) {
    console.error("Webhook misconfigured — missing env vars:", JSON.stringify(has));
    return new Response("Webhook misconfigured: " + JSON.stringify(has),
      { status: 500 });
  }
  const sig = request.headers.get("stripe-signature");
  if (!sig) {
    console.error("No stripe-signature header on request");
    return new Response("Missing signature", { status: 400 });
  }
  const bodyText = await request.text();
  // Verify signature (HMAC-SHA256 with whsec)
  const ok = await verifyStripeSig(bodyText, sig, env.STRIPE_WEBHOOK_SECRET);
  if (!ok) {
    // Helpful diagnostic without leaking the secret: show the first few
    // chars of the WHSEC we're using + the t/v1 of the incoming signature
    // so we can tell at a glance whether we have the wrong secret.
    const whsecPrefix = env.STRIPE_WEBHOOK_SECRET.slice(0, 10);
    const sigParts = sig.split(",").reduce(function (acc, p) {
      const [k, v] = p.split("=", 2);
      acc[k] = v;
      return acc;
    }, {});
    console.error("Signature verification FAILED · " +
      "configured secret prefix: " + whsecPrefix + "... · " +
      "incoming sig t=" + sigParts.t + " v1=" +
      (sigParts.v1 || "").slice(0, 12) + "...");
    return new Response("Invalid signature", { status: 400 });
  }
  console.log("Signature verified · processing event");
  let evt;
  try { evt = JSON.parse(bodyText); }
  catch { return new Response("Invalid JSON", { status: 400 }); }

  // Map plan from price_id on the subscription
  function planFromPriceId(id) {
    if (id === env.STRIPE_PRO_PRICE_ID) return "pro";
    if (id === env.STRIPE_PREMIUM_PRICE_ID) return "premium";
    return null;
  }
  // updateProfile — single PATCH that merges subscription tier and/or
  // the Stripe customer_id onto public.profiles. Caller passes only
  // the fields it wants to change; missing fields are left alone so
  // a subscription.updated event doesn't clobber the cached customer
  // id that arrived in checkout.session.completed.
  async function updateProfile(userId, patch) {
    if (!userId || !patch) return;
    const body = {};
    if (patch.tier) body.subscription_tier = patch.tier;
    if (patch.stripe_customer_id) {
      body.stripe_customer_id = patch.stripe_customer_id;
    }
    if (Object.keys(body).length === 0) return;
    const url = env.SUPABASE_URL.replace(/\/+$/, "") +
      "/rest/v1/profiles?id=eq." + encodeURIComponent(userId);
    await fetch(url, {
      method: "PATCH",
      headers: {
        "apikey": env.SUPABASE_SERVICE_KEY,
        "Authorization": "Bearer " + env.SUPABASE_SERVICE_KEY,
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
      },
      body: JSON.stringify(body),
    });
  }

  const type = evt.type;
  const obj = evt.data && evt.data.object;
  console.log("Event type: " + type + " · obj id: " + (obj && obj.id));
  if (type === "checkout.session.completed") {
    const userId = obj.client_reference_id ||
                   (obj.metadata && obj.metadata.user_id);
    const plan = obj.metadata && obj.metadata.plan;
    // obj.customer is the canonical Stripe customer_id created during
    // this checkout. Cache it on the profile so the Billing Portal
    // endpoint can look the customer up without a Stripe customers/search.
    const stripeCustomerId = obj.customer || null;
    console.log("checkout.session.completed · user_id=" + userId +
      " plan=" + plan + " stripe_customer_id=" + stripeCustomerId);
    if (userId) {
      await updateProfile(userId, {
        tier: plan || undefined,
        stripe_customer_id: stripeCustomerId || undefined,
      });
    }
  } else if (type === "customer.subscription.created" ||
             type === "customer.subscription.updated") {
    const userId = obj.metadata && obj.metadata.user_id;
    const priceId = obj.items && obj.items.data && obj.items.data[0] &&
                    obj.items.data[0].price && obj.items.data[0].price.id;
    const plan = planFromPriceId(priceId);
    const stripeCustomerId = obj.customer || null;
    // Treat any non-terminal status as paid. Stripe's subscription
    // lifecycle: incomplete → active → past_due/unpaid → canceled.
    // For Checkout flows the first `customer.subscription.created`
    // event arrives with status='incomplete' BEFORE the payment is
    // fully confirmed (~1 sec later it flips to 'active'). The user
    // already entered card details and clicked Subscribe — we want
    // to grant Pro access immediately, not wait for the second event.
    const liveStatuses = ["active", "trialing", "incomplete",
                          "incomplete_expired", "past_due"];
    const active = liveStatuses.indexOf(obj.status) >= 0;
    console.log("subscription event · user_id=" + userId +
      " priceId=" + priceId + " plan=" + plan +
      " status=" + obj.status + " active=" + active +
      " stripe_customer_id=" + stripeCustomerId);
    if (userId) {
      await updateProfile(userId, {
        tier: active && plan ? plan : "free",
        stripe_customer_id: stripeCustomerId || undefined,
      });
    }
  } else if (type === "customer.subscription.deleted") {
    const userId = obj.metadata && obj.metadata.user_id;
    console.log("subscription.deleted · user_id=" + userId);
    // Leave stripe_customer_id intact — the customer record still
    // exists in Stripe (with no active sub) and the user may want to
    // re-subscribe later. We just downgrade the entitlement.
    if (userId) await updateProfile(userId, { tier: "free" });
  } else {
    console.log("Ignored event type: " + type);
  }
  return new Response(JSON.stringify({ received: true }), {
    status: 200,
    headers: { "Content-Type": "application/json", ...cors },
  });
}

// Stripe webhook signature verification (HMAC-SHA256).
// Uses WebCrypto since the Cloudflare Worker runtime supports it.
async function verifyStripeSig(payload, sig, secret) {
  try {
    const parts = sig.split(",");
    let ts = null, v1 = null;
    parts.forEach(p => {
      const [k, v] = p.split("=", 2);
      if (k === "t") ts = v;
      if (k === "v1") v1 = v;
    });
    if (!ts || !v1) return false;
    const signed = ts + "." + payload;
    const enc = new TextEncoder();
    const key = await crypto.subtle.importKey(
      "raw", enc.encode(secret),
      { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
    const buf = await crypto.subtle.sign("HMAC", key, enc.encode(signed));
    const expected = Array.from(new Uint8Array(buf))
      .map(b => b.toString(16).padStart(2, "0")).join("");
    // Timing-safe compare
    if (expected.length !== v1.length) return false;
    let diff = 0;
    for (let i = 0; i < expected.length; i++) {
      diff |= expected.charCodeAt(i) ^ v1.charCodeAt(i);
    }
    return diff === 0;
  } catch (e) { return false; }
}

// ── Pre-Market Buzz ─────────────────────────────────────────────────
// Top gainers + losers with a one-line catalyst per name. EVERYTHING is
// real upstream data — no fabricated movers or reasons:
//   • DISCOVERY (which names are moving): Polygon's gainers/losers
//     snapshot — the only free "what's moving" list. 15-min delayed by
//     license, so a name that JUST started moving may lag ~15m.
//   • % + price (what's displayed): re-fetched per surfaced name from
//     Yahoo extended-hours (includePrePost) so pre/post-market moves are
//     near-real-time and on the correct (split-adjusted) prior close —
//     Polygon's delayed snapshot materially understated fast low-float
//     movers (e.g. STI showed +247% when it was live +334%) and its
//     prior-close basis could be split-distorted. Falls back to the
//     Polygon snapshot number per-name if Yahoo doesn't answer.
//   • catalyst: the single most-recent Polygon news headline for that
//     ticker, used VERBATIM, and ONLY if published within ~36h. If no
//     recent article exists we emit no catalyst rather than invent one.
//   • company name: from the cached SEC ticker→title map.
// Filters out sub-$1 pennies + leveraged/inverse ETFs so the board
// reads like real single-name catalysts (the way a desk scans it).
const PMB_EXCLUDE = new Set([
  "SPY","QQQ","IWM","DIA","TQQQ","SQQQ","SOXL","SOXS","TNA","TZA",
  "SPXL","SPXS","UPRO","SPXU","UVXY","SVXY","VXX","UDOW","SDOW",
  "TMF","TMV","LABU","LABD","FAS","FAZ","NUGT","DUST","YINN","YANG",
  "BOIL","KOLD","GUSH","DRIP","JNUG","JDST","NVDL","NVDU","TSLL","TSLQ",
  "MSTU","MSTX","MSTZ","CONL","BITX","ETHU","USD","ERX","ERY","WEBL",
]);
// Most-recent material SEC filing for a ticker, within the last 4 days,
// used as a catalyst fallback when Polygon news has nothing fresh.
// Skips insider Forms 3/4/5 (rarely THE catalyst) and returns the
// already-human filing headline + the doc URL. Reuses fetchSecFilings
// (CIK map cached 24h, submissions cached 5 min).
async function latest8KCatalyst(ticker) {
  const res = await fetchSecFilings(ticker, 12);
  const filings = (res && res.filings) || [];
  if (!filings.length) return null;
  const now = Date.now();
  for (const f of filings) {           // EDGAR order = most-recent-first
    const d = Date.parse(f.acceptedDate || f.filingDate || "");
    if (isNaN(d)) continue;
    if ((now - d) / 3600000 > 96) break;        // older than 4 days → stop
    if (/^(Form )?[345]$/.test(f.form)) continue; // skip insider forms
    return { label: f.headline || (f.form + " filing"), url: f.url };
  }
  return null;
}
// Live extended-hours quote for one ticker via Yahoo (includePrePost).
// Returns the latest pre/post/regular print + the true (split-adjusted)
// prior close, so % reflects actual extended-hours trading rather than
// Polygon's 15-min-delayed snapshot. null on any failure (caller keeps
// the Polygon value). Edge-cached 30s.
async function fetchYahooExt(sym) {
  try {
    const url = "https://query1.finance.yahoo.com/v8/finance/chart/" +
      encodeURIComponent(sym) + "?interval=2m&range=1d&includePrePost=true";
    const r = await fetch(url, {
      headers: { "User-Agent": "Mozilla/5.0" },
      cf: { cacheTtl: 30 },
      signal: AbortSignal.timeout(6000),
    });
    if (!r.ok) return null;
    const j = await r.json();
    const res = j && j.chart && j.chart.result && j.chart.result[0];
    const m = res && res.meta;
    if (!m) return null;
    const prev = (typeof m.chartPreviousClose === "number" && m.chartPreviousClose > 0)
      ? m.chartPreviousClose
      : (typeof m.previousClose === "number" && m.previousClose > 0
          ? m.previousClose : null);
    if (!prev) return null;
    // Latest extended-hours print = last non-null close across the
    // prePost-inclusive series; fall back to regularMarketPrice.
    let last = null;
    const q = res.indicators && res.indicators.quote && res.indicators.quote[0];
    const closes = q && Array.isArray(q.close) ? q.close : null;
    if (closes) {
      for (let i = closes.length - 1; i >= 0; i--) {
        if (typeof closes[i] === "number" && closes[i] > 0) { last = closes[i]; break; }
      }
    }
    if (last == null && typeof m.regularMarketPrice === "number") {
      last = m.regularMarketPrice;
    }
    if (last == null) return null;
    return { price: last, prevClose: prev, pct: (last / prev - 1) * 100 };
  } catch (_) { return null; }
}
async function fetchPremarketBuzz(env) {
  if (!env.POLYGON_API_KEY) {
    return { error: "POLYGON_API_KEY not configured", gainers: [], losers: [] };
  }
  const key = env.POLYGON_API_KEY;
  async function snap(dir) {
    try {
      const r = await fetch(
        "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/" +
          dir + "?apiKey=" + key,
        { signal: AbortSignal.timeout(8000) });
      if (!r.ok) return [];
      const j = await r.json();
      return j.tickers || [];
    } catch (_) { return []; }
  }
  // Name lookup from the cached SEC ticker map (one fetch, 24h edge cache).
  let nameByTk = {};
  try {
    const secMap = await fetchSecTickerMap();
    if (secMap) {
      Object.keys(secMap).forEach(function (k) {
        const o = secMap[k];
        if (o && o.ticker) nameByTk[String(o.ticker).toUpperCase()] = o.title;
      });
    }
  } catch (_) {}
  const [gRaw, lRaw] = await Promise.all([snap("gainers"), snap("losers")]);
  function shape(arr) {
    const out = [];
    for (const t of arr) {
      const tk = (t.ticker || "").toUpperCase();
      const pct = t.todaysChangePerc;
      if (!tk || pct == null || PMB_EXCLUDE.has(tk)) continue;
      if (/[.\-]/.test(tk)) continue;             // skip pref/warrant/unit classes
      // REQUIRE a real SEC operating-company name — the single cleanest
      // filter. Leveraged ETNs / inverse notes / structured products
      // (ONDL, XNDX, LUNL, etc.) that flood the raw gainers/losers feed
      // aren't SEC-registered operating companies, so they're absent
      // from company_tickers.json and drop out here — leaving a board of
      // real single-name movers the way a desk actually scans it.
      const name = nameByTk[tk];
      if (!name) continue;
      const price = (t.day && t.day.c) ||
                    (t.lastTrade && t.lastTrade.p) ||
                    (t.prevDay && t.prevDay.c) || null;
      if (price != null && price < 1) continue;   // drop sub-$1 pennies
      out.push({
        ticker: tk,
        name:   name,
        pct:    Math.round(pct * 10) / 10,
        price:  price != null ? Math.round(price * 100) / 100 : null,
      });
      if (out.length >= 16) break;   // candidate pool (pre-filter)
    }
    return out;
  }
  // Candidate pools (up to 16 each), then attach catalysts, then keep
  // the names that read like real desk "buzz": ones with a genuine
  // recent news catalyst OR liquid enough (≥ $5) to be a real mover —
  // dropping the unexplained sub-$5 micro-cap pumps that otherwise
  // dominate the raw %-feed. %-ranked within each column like a desk
  // board. Falls back to top movers if a quiet session yields too few.
  const gCand = shape(gRaw);
  const lCand = shape(lRaw);
  // ── Live re-pricing ──────────────────────────────────────────────
  // Polygon discovered the movers (above); now overwrite each row's pct
  // + price with Yahoo extended-hours so the displayed number is the
  // true near-real-time pre/post move on a split-adjusted basis. Per
  // name: keep Polygon's value only if Yahoo doesn't answer.
  await Promise.all(gCand.concat(lCand).map(async function (row) {
    const y = await fetchYahooExt(row.ticker);
    if (y && y.pct != null && isFinite(y.pct) && y.price != null) {
      row.pct         = Math.round(y.pct * 10) / 10;
      row.price       = Math.round(y.price * 100) / 100;
      row.prev_close  = Math.round(y.prevClose * 100) / 100;
      row.price_basis = "yahoo-ext";
    }
  }));
  // Re-rank with the corrected numbers: gainers must be up, losers down,
  // price >= $1, sorted by magnitude. Drops the Polygon sign-flips and
  // stale prints that Yahoo reveals as actually flat.
  function liveRank(arr, dir) {
    return arr
      .filter(function (r) { return r.price == null || r.price >= 1; })
      .filter(function (r) { return dir === "up" ? r.pct > 0 : r.pct < 0; })
      .sort(function (a, b) { return dir === "up" ? b.pct - a.pct : a.pct - b.pct; });
  }
  const gRank = liveRank(gCand, "up");
  const lRank = liveRank(lCand, "down");
  async function attachCatalyst(row) {
    try {
      const nr = await fetch(
        "https://api.polygon.io/v2/reference/news?ticker=" + row.ticker +
          "&limit=1&order=desc&sort=published_utc&apiKey=" + key,
        { signal: AbortSignal.timeout(6000) });
      if (!nr.ok) return;
      const nj = await nr.json();
      const a = (nj.results || [])[0];
      if (a && a.title && a.published_utc) {
        const ageH = (Date.now() - new Date(a.published_utc).getTime()) / 3600000;
        if (ageH <= 36) {
          row.catalyst = a.title;
          row.catalyst_url = a.article_url || null;
        }
      }
    } catch (_) {}
  }
  await Promise.all(gRank.concat(lRank).map(attachCatalyst));
  // EDGAR 8-K fallback — Polygon's news tier doesn't carry the fresh
  // micro-cap press releases that drive these pops (verified: no XOS
  // article on its +172% day). But the catalyst is almost always filed
  // as an 8-K/424B5/13D within minutes. For any mover STILL without a
  // catalyst, pull its most recent non-insider filing inside 4 days and
  // use the human filing headline ("8-K · Material Agreement",
  // "Prospectus supplement / shelf takedown", etc.) as the why. Bounded
  // to the names that need it.
  const needEdgar = gRank.concat(lRank)
    .filter(function (r) { return !r.catalyst; }).slice(0, 14);
  await Promise.all(needEdgar.map(async function (row) {
    try {
      const f = await latest8KCatalyst(row.ticker);
      if (f) {
        row.catalyst = f.label;
        row.catalyst_url = f.url;
        row.catalyst_src = "edgar";
      }
    } catch (_) {}
  }));
  // Buzz filter: catalyst OR price ≥ $5. If that leaves < 4 in a column
  // (quiet pre-market), fall back to the raw top movers so the pane is
  // never misleadingly empty.
  function buzzPick(cand) {
    const buzz = cand.filter(function (r) {
      return r.catalyst || (r.price != null && r.price >= 5);
    });
    const picked = (buzz.length >= 4 ? buzz : cand).slice(0, 10);
    return picked;
  }
  const gainers = buzzPick(gRank);
  const losers  = buzzPick(lRank);
  // How many displayed rows got the live Yahoo number vs fell back to
  // the Polygon snapshot — surfaces pricing-source health for QA.
  const allShown = gainers.concat(losers);
  const livePriced = allShown.filter(function (r) {
    return r.price_basis === "yahoo-ext";
  }).length;
  return {
    generated:     new Date().toISOString(),
    total:         gainers.length + losers.length,
    up:            gainers.length,
    down:          losers.length,
    top_ticker:    gainers[0] ? gainers[0].ticker : null,
    top_pct:       gainers[0] ? gainers[0].pct : null,
    gainers:       gainers,
    losers:        losers,
    source:        "polygon+yahoo",
    price_basis:   "yahoo-extended",
    live_priced:   livePriced,
    priced_total:  allShown.length,
    // Pricing is near-real-time extended-hours; the LIST of names is
    // discovered from Polygon's 15-min-delayed snapshot, so a brand-new
    // mover can take ~15m to appear.
    delay_minutes: 0,
    discovery:     "polygon-snapshot-15m",
  };
}

// ── In-memory micro-cache ────────────────────────────────────────────────
// The Cache API (caches.default) is INERT on *.workers.dev domains, so the
// edge cache wired into the live-flow / premarket / news handlers below never
// actually hits there — every poll re-ran the full Polygon fan-out (live-flow
// measured 5-7s, and N× the upstream load under concurrent pollers, the kind
// of burst that can trip the Polygon Starter rate limit). This module-global
// TTL cache lives on the warm isolate and works regardless of domain, so
// repeat polls within the TTL return instantly and share one upstream batch.
// Bounded to cap memory. (The Cache API calls stay — they start working for
// free if the worker ever moves behind a custom domain / route.)
const MEM_CACHE = new Map();   // key -> { ts, data }
function memGet(key, ttlMs) {
  const e = MEM_CACHE.get(key);
  if (e && (Date.now() - e.ts) < ttlMs) return e.data;
  if (e) MEM_CACHE.delete(key);
  return null;
}
function memPut(key, data) {
  MEM_CACHE.set(key, { ts: Date.now(), data });
  if (MEM_CACHE.size > 64) MEM_CACHE.delete(MEM_CACHE.keys().next().value);
}

export default {
  async fetch(request, env, ctx) {
    const cors = {
      "Access-Control-Allow-Origin":  "*",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type, Authorization",
      // charset=utf-8 so the Unicode in our JSON bodies (· — ≥ ★ etc.
      // in license/freshness notes) renders correctly in every client.
      // Response.json defaults to bare "application/json"; this explicit
      // value in the spread headers wins. Harmless on the null-body
      // OPTIONS preflight below.
      "Content-Type": "application/json; charset=utf-8",
      // Defense-in-depth: stop browsers MIME-sniffing our JSON responses.
      "X-Content-Type-Options": "nosniff",
    };

    // CORS preflight — null body, so the content-type above is ignored
    // by browsers; we keep cors as-is for simplicity.
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: cors });
    }

    // ── Stripe Checkout + webhook routing ─────────────────────────
    // Path-based: /stripe/checkout (POST) and /stripe/webhook (POST).
    // The webhook ingests subscription events and updates
    // public.profiles.subscription_tier so the entitlement layer
    // in the client picks up the new plan immediately.
    const urlPath = new URL(request.url).pathname;
    if (urlPath === "/stripe/checkout" && request.method === "POST") {
      return handleStripeCheckout(request, env, cors);
    }
    if (urlPath === "/stripe/webhook" && request.method === "POST") {
      return handleStripeWebhook(request, env, cors);
    }
    if (urlPath === "/stripe/portal" && request.method === "POST") {
      return handleStripePortal(request, env, cors);
    }
    // Portfolio screenshot OCR — vision extract of tickers/weights.
    if (urlPath === "/portfolio-ocr" && request.method === "POST") {
      return handlePortfolioOCR(request, env, cors);
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
        // Hit both Yahoo (full chart + intraday bars + sparkline data)
        // AND Polygon snapshot (cleaner last-trade timestamp + prevDay
        // close from the broker-grade feed). Then for each symbol,
        // pick the fresher last-trade source for the live price /
        // change chip and merge Yahoo's bars in for the sparkline.
        const quotes = await Promise.all(syms.map(async function (sym) {
          const [y, p] = await Promise.all([
            fetchYahooQuote(sym),
            fetchPolygonSnapshot(sym, env),
          ]);
          // Pre/post: trust Yahoo extended-hours outright. Polygon's
          // snapshot is 15m-delayed and not reliably pre/post-aware, so
          // letting it override would re-introduce the stale number.
          if (y && y.extended) {
            return Object.assign({}, y, {
              polygon_prev_close: p ? p.prevClose : null,
            });
          }
          // No Polygon → just return Yahoo as-is.
          if (!p || p.price == null) return y;
          // No Yahoo → return Polygon shape (no bars, but at least live
          // price + change). Falls back from a Yahoo outage.
          if (!y || y.price == null) {
            return {
              symbol:        sym,
              price:         p.price,
              change:        p.change,
              prevClose:     p.prevClose,
              bars:          [],
              last_trade_ts: p.last_trade_ts,
              last_trade_age_sec: p.last_trade_ms
                ? Math.max(0, Math.round((Date.now() - p.last_trade_ms) / 1000))
                : null,
              source:        "polygon",
              delay_minutes: 15,
              fetched_at:    new Date().toISOString(),
            };
          }
          // Both populated → keep Yahoo's bars (for the sparkline), and
          // pick the fresher last-trade timestamp for price/change. If
          // Polygon is fresher by >5 seconds OR Yahoo's prev is missing,
          // override.
          const yTs = y.last_trade_ts ? Date.parse(y.last_trade_ts) : 0;
          const pTs = p.last_trade_ts ? Date.parse(p.last_trade_ts) : 0;
          const useP = (pTs > yTs + 5000) || y.prevClose == null;
          if (useP) {
            return Object.assign({}, y, {
              price:         p.price,
              change:        p.change,
              prevClose:     p.prevClose != null ? p.prevClose : y.prevClose,
              last_trade_ts: p.last_trade_ts,
              last_trade_age_sec: p.last_trade_ms
                ? Math.max(0, Math.round((Date.now() - p.last_trade_ms) / 1000))
                : y.last_trade_age_sec,
              source:        "polygon+yahoo",
            });
          }
          // Yahoo is the fresher source — keep it but expose Polygon's
          // prev close as a cross-check (helps detect Yahoo cache bugs).
          return Object.assign({}, y, {
            polygon_prev_close: p.prevClose,
            polygon_price:      p.price,
            source:             "yahoo+polygon",
          });
        }));
        // Edge cache 15s (was 30s) — concurrent users share one fan-out
        // but each user gets a near-fresh price within 15s of the last
        // upstream call. Upstream (Yahoo + Polygon) is already 15-min
        // delayed by license, so dropping below 15s of edge cache buys
        // nothing — that's the floor on freshness.
        const resp = Response.json({ quotes: quotes }, {
          headers: { ...cors, "Cache-Control": "public, max-age=15" },
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
      // ATM-straddle implied move + IV snapshot for a single ticker.
      // `?iv=AAPL` returns { spot, expiry, dte, implied_move_pct, iv_pct,
      // iv_level }. Optional `?earningsDate=YYYY-MM-DD` picks the first
      // expiry on-or-after that date (so the badge reflects move INTO
      // the print, not the closest weekly). Used by the Earnings cards.
      const ivParam = url.searchParams.get("iv");
      if (ivParam) {
        const cache = caches.default;
        const hit = await cache.match(request);
        if (hit) return hit;
        const tk = ivParam.trim().toUpperCase();
        if (!tk || !/^[A-Z.\-]{1,8}$/.test(tk)) {
          return Response.json(
            { error: `Invalid ticker: "${tk}"` },
            { status: 400, headers: cors });
        }
        const ed = (url.searchParams.get("earningsDate") || "").trim();
        const validED = /^\d{4}-\d{2}-\d{2}$/.test(ed) ? ed : null;
        const data = await fetchIvSnapshot(env, tk, validED);
        const resp = Response.json(data, {
          headers: { ...cors, "Cache-Control": "public, max-age=300" },
        });
        ctx.waitUntil(cache.put(request, resp.clone()));
        return resp;
      }
      // Live Flow feed — ?live-flow=1 returns notable options flow across
      // the default ~180-name universe (mega-caps for context PLUS SMID
      // / high-beta where UOA edge lives), augmented with the optional
      // ?tickers=A,B,C list (e.g. user watchlist — never excluded).
      // The screen uses an EITHER-OR gate: a contract passes if its
      // premium clears min_premium OR its volume / open-interest ratio
      // clears min_vol_oi. Defaults ($100K, 2.0×) mirror the batch
      // uoa_scanner.py thresholds so live + batch surface the same kind
      // of signal. Edge cached 30s so concurrent users share one batch.
      if (url.searchParams.get("live-flow")) {
        const cache = caches.default;
        const hit = await cache.match(request);
        if (hit) return hit;
        const extra = (url.searchParams.get("tickers") || "")
          .toUpperCase().split(",").map(function (s) { return s.trim(); })
          .filter(function (s) { return /^[A-Z.\-]{1,8}$/.test(s); });
        const uni = Array.from(new Set(
          (extra.length ? extra.concat(LF_DEFAULT_UNIVERSE)
                        : LF_DEFAULT_UNIVERSE)));
        const opts = {
          tickers:      uni,
          minPremium:   +url.searchParams.get("min_premium")   || 100_000,
          minVolOi:     +url.searchParams.get("min_vol_oi")    || 2.0,
          freshnessMin: +url.searchParams.get("freshness_min") || 90,
          limit:        +url.searchParams.get("limit")         || 100,
        };
        // In-memory fallback cache (the Cache API above is inert on
        // *.workers.dev). Key on the variable inputs only — the default
        // universe is constant, so every default poller shares one 60s batch
        // instead of each re-running the 5-7s Polygon fan-out.
        const memKey = "lf:" + extra.slice().sort().join(",") + ":" +
          opts.minPremium + ":" + opts.minVolOi + ":" +
          opts.freshnessMin + ":" + opts.limit;
        let data = memGet(memKey, 60_000);
        if (!data) { data = await fetchLiveFlow(env, opts); memPut(memKey, data); }
        const resp = Response.json(data, {
          headers: { ...cors, "Cache-Control": "public, max-age=60" },
        });
        ctx.waitUntil(cache.put(request, resp.clone()));
        return resp;
      }
      // Pre-Market Buzz — ?premarket=1 returns top gainers/losers with a
      // verbatim news catalyst per name. Edge-cached 5 min: the movers
      // shift slowly and this fans out to ~20 Polygon news calls, so
      // sharing one upstream batch across users keeps it cheap.
      if (url.searchParams.get("premarket")) {
        const cache = caches.default;
        const hit = await cache.match(request);
        if (hit) return hit;
        // In-memory fallback (Cache API inert on *.workers.dev) — share one
        // 5-min batch across pollers instead of re-running ~20 Polygon news
        // calls each time.
        let data = memGet("pm", 300_000);
        if (!data) { data = await fetchPremarketBuzz(env); memPut("pm", data); }
        const resp = Response.json(data, {
          headers: { ...cors, "Cache-Control": "public, max-age=300" },
        });
        ctx.waitUntil(cache.put(request, resp.clone()));
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
        const lim = url.searchParams.get("limit") || defaultLimit;
        // The general firehose backs a live headline tape, so refresh it on a
        // ~30s cadence; per-ticker lookups change slowly, keep 5 min. In-memory
        // cache (Cache API inert on *.workers.dev) shares one batch across all
        // pollers, so a 30s tape ≠ N× Polygon calls.
        const isFirehose = tk === "general";
        const ttlMs = isFirehose ? 30_000 : 300_000;
        const ccMax = isFirehose ? 30 : 300;
        const nkey = "news:" + tk + ":" + lim;
        let data = memGet(nkey, ttlMs);
        if (!data) { data = await fetchPolygonNews(env, tk, lim); memPut(nkey, data); }
        const resp = Response.json(data, {
          headers: { ...cors, "Cache-Control": "public, max-age=" + ccMax },
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

    // ── Server-side rate limit (QA-08) ───────────────────────────────
    // The client pre-flights tdCanGenerateReport() before this call,
    // but a devtools user can disable the button and POST anyway. So
    // the worker re-validates: identify the user from the Supabase
    // JWT, look up their tier and 30-day report count, reject if
    // they're over the cap. Anonymous calls (no Authorization header)
    // are allowed through with free-tier limits — they will fail at
    // the client gate, but we treat them as best-effort and let the
    // GitHub workflow itself be the backstop.
    //
    // Tier caps mirror PLAN_LIMITS in docs/index.html. Keep in sync.
    const TIER_REPORT_CAPS_30D = {
      free: 3, pro: 10, premium: 100, beta: 50,
    };
    const TIER_LIFETIME_CAPS = {
      free: 3, pro: 9999, premium: 9999, beta: 9999,
    };
    const authHeader = request.headers.get("Authorization") || "";
    const bearer = authHeader.startsWith("Bearer ") ? authHeader.slice(7) : "";
    if (bearer && env.SUPABASE_URL && env.SUPABASE_SERVICE_KEY) {
      try {
        // 1. Validate the user's access token by asking Supabase who
        //    it represents. Anon key is enough for /auth/v1/user.
        const userResp = await fetch(
          env.SUPABASE_URL + "/auth/v1/user",
          { headers: {
              "Authorization": "Bearer " + bearer,
              "apikey": env.SUPABASE_ANON_KEY || env.SUPABASE_SERVICE_KEY,
          }}
        );
        if (!userResp.ok) {
          return Response.json(
            { ok: false, error: "Sign-in expired. Please sign in again." },
            { status: 401, headers: cors });
        }
        const userBody = await userResp.json();
        const uid = userBody && userBody.id;
        if (uid) {
          // 2. Look up the user's effective tier (honors promo expiry
          //    server-side via the get_user_plan RPC).
          const planResp = await fetch(
            env.SUPABASE_URL + "/rest/v1/rpc/get_user_plan",
            { method: "POST",
              headers: {
                "Authorization": "Bearer " + env.SUPABASE_SERVICE_KEY,
                "apikey": env.SUPABASE_SERVICE_KEY,
                "Content-Type": "application/json",
              },
              body: JSON.stringify({}),
            }
          );
          // RPC returns a string ("free" / "pro" / "premium" / "beta")
          // when called with the service role and user_id is implicit
          // via auth.uid() — but service role has no auth.uid(). So
          // fall back to reading the profile row directly with service
          // role and computing the tier inline.
          let tier = "free";
          try {
            const profResp = await fetch(
              env.SUPABASE_URL + "/rest/v1/profiles?id=eq." +
                encodeURIComponent(uid) +
                "&select=subscription_tier,subscription_expires_at",
              { headers: {
                  "Authorization": "Bearer " + env.SUPABASE_SERVICE_KEY,
                  "apikey": env.SUPABASE_SERVICE_KEY,
              }}
            );
            const profBody = await profResp.json();
            const row = (profBody || [])[0];
            if (row) {
              const expired = row.subscription_expires_at &&
                new Date(row.subscription_expires_at) < new Date();
              tier = expired ? "free" : (row.subscription_tier || "free");
            }
          } catch (_) {}

          // 3. Count this user's reports in the last 30 days
          const sinceIso = new Date(Date.now() - 30 * 86400000).toISOString();
          const countResp = await fetch(
            env.SUPABASE_URL + "/rest/v1/report_generations" +
              "?user_id=eq." + encodeURIComponent(uid) +
              "&created_at=gte." + encodeURIComponent(sinceIso) +
              "&select=id",
            { headers: {
                "Authorization": "Bearer " + env.SUPABASE_SERVICE_KEY,
                "apikey": env.SUPABASE_SERVICE_KEY,
                "Prefer": "count=exact",
            }}
          );
          const contentRange = countResp.headers.get("Content-Range") || "";
          const count30 = parseInt(contentRange.split("/")[1] || "0", 10) || 0;
          const cap30 = TIER_REPORT_CAPS_30D[tier] || TIER_REPORT_CAPS_30D.free;

          // 4. Lifetime cap (free tier only — others are effectively unlimited)
          let lifetime = 0;
          if (tier === "free") {
            const lifeResp = await fetch(
              env.SUPABASE_URL + "/rest/v1/report_generations" +
                "?user_id=eq." + encodeURIComponent(uid) +
                "&select=id",
              { headers: {
                  "Authorization": "Bearer " + env.SUPABASE_SERVICE_KEY,
                  "apikey": env.SUPABASE_SERVICE_KEY,
                  "Prefer": "count=exact",
              }}
            );
            const lifeRange = lifeResp.headers.get("Content-Range") || "";
            lifetime = parseInt(lifeRange.split("/")[1] || "0", 10) || 0;
          }
          const lifeCap = TIER_LIFETIME_CAPS[tier] || TIER_LIFETIME_CAPS.free;

          if (count30 >= cap30) {
            return Response.json(
              { ok: false, error: "Monthly report limit reached for " +
                tier + " tier (" + count30 + " of " + cap30 +
                " used). Upgrade to keep researching.",
                code: "monthly_cap", used: count30, cap: cap30, tier: tier },
              { status: 429, headers: cors });
          }
          if (tier === "free" && lifetime >= lifeCap) {
            return Response.json(
              { ok: false, error: "Free-tier lifetime cap reached (" +
                lifetime + " of " + lifeCap +
                " reports). Upgrade for more.",
                code: "lifetime_cap", used: lifetime, cap: lifeCap, tier: tier },
              { status: 429, headers: cors });
          }
        }
      } catch (e) {
        // If our rate-limit lookup itself fails, fail-open rather than
        // blocking legitimate users on a Supabase hiccup. The client
        // gate already gave best-effort protection.
        console.log("Rate-limit lookup failed (fail-open):", e.message);
      }
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

  // ── Scheduled cron backstop ──────────────────────────────────────
  // GitHub's own `schedule:` cron is best-effort and routinely drops
  // fires under platform load — observed 2026-06-03 when NONE of the
  // five scheduled workflows fired all morning (only manual + chained
  // runs), stranding the desk on the prior evening's scans.
  //
  // Cloudflare Cron Triggers are far more reliable, so the worker now
  // *also* pokes the scans on schedule via workflow_dispatch (the same
  // GitHub API + reused env.PAT the ad-hoc report trigger uses — no new
  // secret). The GitHub-native schedules stay in place as primary; this
  // is belt-and-suspenders. If both fire, the scanner's emit_latest
  // dedups by timestamp so a double-run is harmless.
  //
  // Cron→workflow map (UTC, see wrangler.toml [triggers]):
  //   "40 20 * * 1-5"  → EOD: scanner.yml + momentum.yml (post-close)
  //   everything else  → intraday: uoa.yml
  async scheduled(event, env, ctx) {
    if (!env.PAT) {
      console.log("[cron] env.PAT not set — backstop skipped");
      return;
    }
    const repo = (env.REPO || "brownplaya239/smid-scanner");
    const dispatch = async function (workflow) {
      try {
        const r = await fetch(
          "https://api.github.com/repos/" + repo +
            "/actions/workflows/" + workflow + "/dispatches",
          { method: "POST",
            headers: {
              "Authorization":        "Bearer " + env.PAT,
              "Accept":               "application/vnd.github+json",
              "X-GitHub-Api-Version": "2022-11-28",
              "User-Agent":           "tickerdesk-cron-backstop",
              "Content-Type":         "application/json",
            },
            body: JSON.stringify({ ref: "master" }),
          }
        );
        console.log("[cron] dispatch " + workflow + " -> HTTP " + r.status);
      } catch (e) {
        console.log("[cron] dispatch " + workflow + " failed: " + e.message);
      }
    };
    // ── Publish-freshness self-heal ──────────────────────────────────
    // Catches the OTHER failure class, observed 2026-06-10: scans ran and
    // committed fresh JSON, but the PUBLISHED site stayed frozen (Pages
    // didn't build — e.g. a deploy-config change; GITHUB_TOKEN commits
    // also can't trigger workflow-based deploys). On every cron fire,
    // check the age of the DEPLOYED uoa_latest.json; if it's >3h stale,
    // force a native Pages build of current master via the same PAT.
    // Rides Cloudflare cron (reliable), so it can't be blinded by the
    // GitHub-schedule drops that silenced the Actions-side watchdog.
    const healPublish = async function () {
      try {
        const r = await fetch(
          "https://tickerdesk.io/reports/uoa_latest.json?cb=" + Date.now(),
          { signal: AbortSignal.timeout(10000),
            headers: { "User-Agent": "tickerdesk-cron-backstop" } });
        if (!r.ok) return;
        const d = await r.json();
        const gen = Date.parse(d && d.generated || "");
        if (isNaN(gen)) return;
        const ageH = (Date.now() - gen) / 3600000;
        if (ageH <= 3) return;                 // published data is current
        const b = await fetch(
          "https://api.github.com/repos/" + repo + "/pages/builds",
          { method: "POST",
            headers: {
              "Authorization":        "Bearer " + env.PAT,
              "Accept":               "application/vnd.github+json",
              "X-GitHub-Api-Version": "2022-11-28",
              "User-Agent":           "tickerdesk-cron-backstop",
            } });
        console.log("[cron] published uoa stale " + ageH.toFixed(1) +
          "h -> forced Pages build, HTTP " + b.status);
      } catch (e) {
        console.log("[cron] healPublish failed: " + e.message);
      }
    };
    const isEod = event.cron === "40 20 * * 1-5";
    if (isEod) {
      ctx.waitUntil(Promise.all([
        dispatch("scanner.yml"),
        dispatch("momentum.yml"),
        healPublish(),
      ]));
    } else {
      ctx.waitUntil(Promise.all([
        dispatch("uoa.yml"),
        healPublish(),
      ]));
    }
  },
};
