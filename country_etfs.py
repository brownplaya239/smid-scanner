"""
country_etfs.py — Pull YTD performance + dollar volume for the curated
country / region ETF universe, write to docs/reports/country_etfs.json
for the dashboard's Global Markets tab.

Real-data version of the user's mock-data plan. Polygon daily_bars
gives us close history; we compute:
  ytd_pct   = (latest_close / first_close_of_year - 1) * 100
  vs_anchor = ytd_pct - SPY's ytd_pct

Sector concentrations and AUM are baked into ETF_UNIVERSE below
(iShares concentrations are stable year-over-year — a daily fetch
for them isn't worth the API complexity). AUM tier drives the
treemap block size.

Output: docs/reports/country_etfs.json

Schema:
  {
    updated, anchor: {ticker, ytd_pct, aum_b},
    etfs: [{ticker, country, region, developed, ytd_pct, vs_anchor,
            aum_b, aum_tier, top_sector}, ...]
  }
"""

import json
import os
import sys
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import pytz

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import polygon_data as pg

ET = pytz.timezone("America/New_York")
_BASE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(_BASE, "docs", "reports", "country_etfs.json")

# Curated universe: 20 country/region ETFs with stable sector
# concentrations + rough AUM. Sectors hand-maintained from iShares
# factsheets — change only ~1pp month over month so a daily fetch
# isn't worth the API cost.
#   tier: "xl" >= $15B, "lg" >= $5B, "md" >= $1B, "sm" < $1B
#         — drives the treemap block sizing
ETF_UNIVERSE = [
    # ── Anchor (S&P 500 — same exposure as IVV, higher volume) ──
    {"ticker": "SPY",  "country": "United States",       "region": "anchor",
     "developed": True,  "aum_b": 580.0, "aum_tier": "xl",
     "top_sector": "Technology 30%", "anchor": True},

    # ── Americas ──
    {"ticker": "EWC",  "country": "Canada",              "region": "americas",
     "developed": True,  "aum_b": 5.0,  "aum_tier": "md",
     "top_sector": "Financials 30%"},
    {"ticker": "EWW",  "country": "Mexico",              "region": "americas",
     "developed": False, "aum_b": 2.2,  "aum_tier": "md",
     "top_sector": "Cons. Staples 24%"},
    {"ticker": "EWZ",  "country": "Brazil",              "region": "americas",
     "developed": False, "aum_b": 4.1,  "aum_tier": "md",
     "top_sector": "Financials 22%"},
    {"ticker": "ECH",  "country": "Chile",               "region": "americas",
     "developed": False, "aum_b": 0.5,  "aum_tier": "sm",
     "top_sector": "Materials 26%"},
    {"ticker": "ILF",  "country": "Latin America",       "region": "americas",
     "developed": False, "aum_b": 1.7,  "aum_tier": "md",
     "top_sector": "Financials 27%"},

    # ── Europe Developed ──
    {"ticker": "IEUR", "country": "Europe (core MSCI)",  "region": "europe",
     "developed": True,  "aum_b": 5.5,  "aum_tier": "lg",
     "top_sector": "Industrials 16%"},
    {"ticker": "EWU",  "country": "United Kingdom",      "region": "europe",
     "developed": True,  "aum_b": 2.9,  "aum_tier": "md",
     "top_sector": "Financials 18%"},
    {"ticker": "EWG",  "country": "Germany",             "region": "europe",
     "developed": True,  "aum_b": 1.4,  "aum_tier": "md",
     "top_sector": "Industrials 24%"},
    {"ticker": "EWQ",  "country": "France",              "region": "europe",
     "developed": True,  "aum_b": 0.9,  "aum_tier": "sm",
     "top_sector": "Industrials 18%"},
    {"ticker": "EWI",  "country": "Italy",               "region": "europe",
     "developed": True,  "aum_b": 0.4,  "aum_tier": "sm",
     "top_sector": "Financials 32%"},
    {"ticker": "EWP",  "country": "Spain",               "region": "europe",
     "developed": True,  "aum_b": 0.6,  "aum_tier": "sm",
     "top_sector": "Financials 32%"},
    {"ticker": "EWN",  "country": "Netherlands",         "region": "europe",
     "developed": True,  "aum_b": 0.4,  "aum_tier": "sm",
     "top_sector": "Technology 21%"},
    {"ticker": "EWL",  "country": "Switzerland",         "region": "europe",
     "developed": True,  "aum_b": 1.4,  "aum_tier": "md",
     "top_sector": "Healthcare 32%"},
    {"ticker": "EWD",  "country": "Sweden",              "region": "europe",
     "developed": True,  "aum_b": 0.3,  "aum_tier": "sm",
     "top_sector": "Industrials 38%"},
    {"ticker": "EWO",  "country": "Austria",             "region": "europe",
     "developed": True,  "aum_b": 0.2,  "aum_tier": "sm",
     "top_sector": "Financials 36%"},
    {"ticker": "EWK",  "country": "Belgium",             "region": "europe",
     "developed": True,  "aum_b": 0.05, "aum_tier": "sm",
     "top_sector": "Cons. Staples 30%"},
    {"ticker": "EIRL", "country": "Ireland",             "region": "europe",
     "developed": True,  "aum_b": 0.1,  "aum_tier": "sm",
     "top_sector": "Materials 22%"},
    {"ticker": "EPOL", "country": "Poland",              "region": "europe",
     "developed": False, "aum_b": 0.3,  "aum_tier": "sm",
     "top_sector": "Financials 39%"},

    # ── APAC Developed ──
    {"ticker": "EWJ",  "country": "Japan",               "region": "apac",
     "developed": True,  "aum_b": 14.0, "aum_tier": "lg",
     "top_sector": "Industrials 22%"},
    {"ticker": "EWA",  "country": "Australia",           "region": "apac",
     "developed": True,  "aum_b": 2.0,  "aum_tier": "md",
     "top_sector": "Financials 27%"},
    {"ticker": "EWH",  "country": "Hong Kong",           "region": "apac",
     "developed": True,  "aum_b": 0.8,  "aum_tier": "sm",
     "top_sector": "Financials 38%"},
    {"ticker": "EWS",  "country": "Singapore",           "region": "apac",
     "developed": True,  "aum_b": 0.4,  "aum_tier": "sm",
     "top_sector": "Financials 49%"},
    {"ticker": "ENZL", "country": "New Zealand",         "region": "apac",
     "developed": True,  "aum_b": 0.05, "aum_tier": "sm",
     "top_sector": "Industrials 23%"},

    # ── EM Aggregates ──
    {"ticker": "EEM",  "country": "Emerging Markets",    "region": "em",
     "developed": False, "aum_b": 19.0, "aum_tier": "xl",
     "top_sector": "Technology 24%"},

    # ── Asia EM ──
    {"ticker": "INDA", "country": "India",               "region": "em",
     "developed": False, "aum_b": 11.0, "aum_tier": "lg",
     "top_sector": "Financials 25%"},
    {"ticker": "MCHI", "country": "China (large-cap)",   "region": "em",
     "developed": False, "aum_b": 7.5,  "aum_tier": "lg",
     "top_sector": "Cons. Discretionary 31%"},
    {"ticker": "EWT",  "country": "Taiwan",              "region": "em",
     "developed": False, "aum_b": 11.0, "aum_tier": "lg",
     "top_sector": "Technology 67%"},
    {"ticker": "EWY",  "country": "South Korea",         "region": "em",
     "developed": False, "aum_b": 4.3,  "aum_tier": "md",
     "top_sector": "Technology 30%"},
    {"ticker": "EIDO", "country": "Indonesia",           "region": "em",
     "developed": False, "aum_b": 0.3,  "aum_tier": "sm",
     "top_sector": "Financials 47%"},
    {"ticker": "EWM",  "country": "Malaysia",            "region": "em",
     "developed": False, "aum_b": 0.2,  "aum_tier": "sm",
     "top_sector": "Financials 30%"},
    {"ticker": "EPHE", "country": "Philippines",         "region": "em",
     "developed": False, "aum_b": 0.1,  "aum_tier": "sm",
     "top_sector": "Financials 30%"},
    {"ticker": "THD",  "country": "Thailand",            "region": "em",
     "developed": False, "aum_b": 0.3,  "aum_tier": "sm",
     "top_sector": "Financials 21%"},

    # ── EMEA EM ──
    {"ticker": "TUR",  "country": "Turkey",              "region": "em",
     "developed": False, "aum_b": 0.5,  "aum_tier": "sm",
     "top_sector": "Financials 31%"},
    {"ticker": "EZA",  "country": "South Africa",        "region": "em",
     "developed": False, "aum_b": 0.4,  "aum_tier": "sm",
     "top_sector": "Financials 26%"},
    {"ticker": "EIS",  "country": "Israel",              "region": "em",
     "developed": True,  "aum_b": 0.2,  "aum_tier": "sm",
     "top_sector": "Industrials 21%"},
    {"ticker": "KSA",  "country": "Saudi Arabia",        "region": "em",
     "developed": False, "aum_b": 0.7,  "aum_tier": "sm",
     "top_sector": "Financials 35%"},
    {"ticker": "QAT",  "country": "Qatar",               "region": "em",
     "developed": False, "aum_b": 0.1,  "aum_tier": "sm",
     "top_sector": "Financials 49%"},
    {"ticker": "UAE",  "country": "United Arab Emirates","region": "em",
     "developed": False, "aum_b": 0.05, "aum_tier": "sm",
     "top_sector": "Financials 50%"},
]


def _ytd_from_bars(bars):
    """Compute YTD % from a list of Polygon daily bars. Anchors on the
    first trading day of the current calendar year — the first bar in
    the series whose date is >= Jan 1 of this year. Returns None if we
    don't have data going back that far yet."""
    if not bars:
        return None
    year_start = datetime.now(ET).strftime("%Y") + "-01-01"
    first = None
    for b in bars:
        t = b.get("t")
        if not t:
            continue
        d = datetime.fromtimestamp(t / 1000, ET).strftime("%Y-%m-%d")
        if d >= year_start:
            first = b
            break
    if not first or not first.get("c"):
        return None
    latest = bars[-1]
    if not latest.get("c"):
        return None
    return (latest["c"] / first["c"] - 1) * 100


def fetch_ytd(ticker):
    """Pull ~6 months of daily bars (covers all of YTD through May +
    rolling buffer), compute YTD pct. None on any error."""
    bars = pg.daily_bars(ticker, days=180)
    return _ytd_from_bars(bars)


def run():
    print(f"Pulling YTD for {len(ETF_UNIVERSE)} country/region ETFs...")
    tickers = [e["ticker"] for e in ETF_UNIVERSE]
    ytd_map = {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        for t, ytd in ex.map(lambda t: (t, fetch_ytd(t)), tickers):
            ytd_map[t] = ytd
    # SPY anchors the relative comparison
    anchor_ytd = ytd_map.get("SPY")
    print(f"  SPY (anchor) YTD: "
          f"{('%+.2f%%' % anchor_ytd) if anchor_ytd is not None else 'n/a'}")

    anchor = None
    etfs = []
    for cfg in ETF_UNIVERSE:
        ytd = ytd_map.get(cfg["ticker"])
        vs_anchor = (ytd - anchor_ytd) if (ytd is not None
                                            and anchor_ytd is not None) else None
        record = {
            "ticker":     cfg["ticker"],
            "country":    cfg["country"],
            "region":     cfg["region"],
            "developed":  cfg["developed"],
            "aum_b":      cfg["aum_b"],
            "aum_tier":   cfg["aum_tier"],
            "top_sector": cfg["top_sector"],
            "ytd_pct":    round(ytd, 2) if ytd is not None else None,
            "vs_anchor":  round(vs_anchor, 2) if vs_anchor is not None else None,
        }
        if cfg.get("anchor"):
            anchor = record
        else:
            etfs.append(record)
        print(f"  {cfg['ticker']:5s} {cfg['country'][:28]:28s} "
              f"YTD={('%+6.2f%%' % ytd) if ytd is not None else '  n/a':>7}  "
              f"vs SPY={('%+6.2f%%' % vs_anchor) if vs_anchor is not None else '  n/a':>7}")

    payload = {
        "updated": datetime.now(ET).isoformat(timespec="seconds"),
        "anchor":  anchor,
        "etfs":    etfs,
        "source":  "Polygon daily bars · sector + AUM hand-maintained from iShares factsheets",
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    print(f"  Wrote country_etfs.json ({len(etfs)} ETFs + 1 anchor)")


if __name__ == "__main__":
    run()
