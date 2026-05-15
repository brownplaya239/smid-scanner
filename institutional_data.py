"""
institutional_data.py — Institutional ownership intelligence for ad-hoc reports.

Sources:
  - yfinance: institutional_holders, major_holders, mutualfund_holders (free, ~85% reliable)
  - SEC EDGAR: 13D/13G filings (free, excellent — captures activist + 5%+ stakes)

Smart-money fund detection: hardcoded list of well-known active small-cap / growth
managers. If any of these show up in the top holders, that's a stronger signal than
seeing Vanguard/BlackRock (which is just index inclusion, not conviction).
"""

import re
import time
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET

from insider_activity import _load_cik_lookup, SEC_HEADERS, _throttle


# Known smart-money active managers (high-conviction signal when present)
# These are funds known for research-driven small/mid-cap positions, NOT passive index buys.
SMART_MONEY_FUNDS = {
    # Tiger Cubs / hedge funds
    "tiger global", "coatue", "lone pine", "viking", "maverick", "dragoneer",
    "whale rock", "tcv", "altimeter", "d1 capital", "light street", "matrix capital",
    # Specialized small-cap / growth
    "wasatch", "kornitzer", "fidelity small cap", "fidelity contrafund", "fidelity growth",
    "t. rowe price small", "t rowe price small", "primecap", "ark invest", "ark investment",
    "baillie gifford", "edgewood", "polen capital", "sands capital",
    # Activists
    "elliott", "starboard", "icahn", "third point", "trian", "engaged capital",
    "jana partners", "engine no. 1", "engine no 1", "value act",
    # Other known stock-pickers
    "jennison", "gilder gagnon", "akre", "ruane", "longleaf", "wedgewood",
    "pzena", "first manhattan", "joho capital", "tybourne",
}

# Known passive / index funds (signal = NEUTRAL — they hold by mandate, not conviction)
PASSIVE_FUNDS = {
    "vanguard", "blackrock", "ishares", "state street", "ssga", "spdr",
    "schwab", "invesco", "fidelity index", "fidelity total",
    "northern trust", "geode capital", "bnp paribas",
}


def classify_fund(fund_name):
    """Return (category, color_rgb) for a fund."""
    name = (fund_name or "").lower()
    for sm in SMART_MONEY_FUNDS:
        if sm in name:
            return "Smart $", (39, 174, 96)        # green
    for pf in PASSIVE_FUNDS:
        if pf in name:
            return "Passive", (130, 130, 140)      # grey
    return "Active", (52, 100, 180)                # blue (active but not famous)


def fetch_institutional_data(ticker):
    """
    Returns a dict with:
      - major_holders:    overall breakdown (insider%, inst%, mf%, # holders)
      - top_holders:      list of top institutional holders (fund, shares, pct_float, value, date_reported)
      - mf_holders:       list of top mutual fund holders
      - smart_money_count: # of known smart-money funds in top holders
      - smart_money_funds: names of known smart-money funds detected
    """
    out = {
        "major_holders":      {},
        "top_holders":        [],
        "mf_holders":         [],
        "smart_money_count":  0,
        "smart_money_funds":  [],
    }

    try:
        t = yf.Ticker(ticker)

        # Major holders breakdown
        try:
            mh = t.major_holders
            if isinstance(mh, pd.DataFrame) and not mh.empty:
                # yfinance returns DataFrame with values in column 0, label in column 1
                # Or sometimes index-labeled — handle both shapes
                if "Value" in mh.columns:
                    out["major_holders"] = dict(zip(mh.index.astype(str), mh["Value"].astype(float)))
                else:
                    rows = mh.values.tolist()
                    for row in rows:
                        if len(row) >= 2:
                            out["major_holders"][str(row[1]).strip()] = str(row[0]).strip()
        except Exception:
            pass

        # Top institutional holders
        try:
            ih = t.institutional_holders
            if isinstance(ih, pd.DataFrame) and not ih.empty:
                for _, row in ih.head(10).iterrows():
                    holder_name = str(row.get("Holder", "")).strip()
                    if not holder_name:
                        continue
                    cat, _ = classify_fund(holder_name)
                    rec = {
                        "fund":          holder_name,
                        "shares":        int(row.get("Shares", 0) or 0),
                        "pct_out":       float(row.get("pctHeld", row.get("% Out", 0)) or 0),
                        "value":         float(row.get("Value", 0) or 0),
                        "date_reported": str(row.get("Date Reported", "")).split(" ")[0],
                        "category":      cat,
                    }
                    out["top_holders"].append(rec)
                    if cat == "Smart $":
                        out["smart_money_count"] += 1
                        out["smart_money_funds"].append(holder_name)
        except Exception:
            pass

        # Mutual fund holders
        try:
            mfh = t.mutualfund_holders
            if isinstance(mfh, pd.DataFrame) and not mfh.empty:
                for _, row in mfh.head(5).iterrows():
                    holder_name = str(row.get("Holder", "")).strip()
                    if not holder_name:
                        continue
                    cat, _ = classify_fund(holder_name)
                    out["mf_holders"].append({
                        "fund":          holder_name,
                        "shares":        int(row.get("Shares", 0) or 0),
                        "pct_out":       float(row.get("pctHeld", row.get("% Out", 0)) or 0),
                        "value":         float(row.get("Value", 0) or 0),
                        "date_reported": str(row.get("Date Reported", "")).split(" ")[0],
                        "category":      cat,
                    })
                    if cat == "Smart $" and holder_name not in out["smart_money_funds"]:
                        out["smart_money_count"] += 1
                        out["smart_money_funds"].append(holder_name)
        except Exception:
            pass

    except Exception:
        pass

    return out


def fetch_13d_13g_filings(ticker, days_back=365, max_filings=20):
    """
    Returns recent 13D/13G filings against this company from SEC EDGAR.
    These reveal funds that have crossed the 5% beneficial ownership threshold.
    Each filing: date, filer (parsed from primary doc), stake_pct, form_type.

    Note: filer name + stake % require parsing the SC 13 document (XBRL or text).
    For reliability we extract just date and form type from the filings index;
    fund name parsing is best-effort.
    """
    out = []
    ticker = ticker.upper().strip()

    cik_map = _load_cik_lookup()
    cik = cik_map.get(ticker)
    if not cik:
        return out

    try:
        _throttle()
        resp = requests.get(
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            headers=SEC_HEADERS, timeout=15
        )
        if resp.status_code != 200:
            return out
        filings = resp.json().get("filings", {}).get("recent", {})
    except Exception:
        return out

    forms     = filings.get("form", [])
    accs      = filings.get("accessionNumber", [])
    dates     = filings.get("filingDate", [])
    primaries = filings.get("primaryDocument", [])

    cutoff = datetime.now().date() - timedelta(days=days_back)
    cik_int = int(cik.lstrip("0")) if cik.lstrip("0") else int(cik)

    interesting_forms = {"SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A"}

    candidates = []
    for i, form in enumerate(forms):
        if form not in interesting_forms:
            continue
        if i >= len(dates) or i >= len(accs) or i >= len(primaries):
            continue
        try:
            fdate = datetime.strptime(dates[i], "%Y-%m-%d").date()
            if fdate < cutoff:
                continue
            candidates.append((i, form, fdate))
        except Exception:
            continue

    # Sort newest first, cap
    candidates.sort(key=lambda x: x[2], reverse=True)
    candidates = candidates[:max_filings]

    for i, form, fdate in candidates:
        # Extract filer name from primary doc (best-effort; SC 13 docs are usually HTML or .txt)
        filer = ""
        stake = ""
        acc_clean = accs[i].replace("-", "")
        primary   = primaries[i]
        url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}/{primary}"

        try:
            _throttle()
            r = requests.get(url, headers=SEC_HEADERS, timeout=10)
            if r.status_code == 200 and r.text:
                text = r.text[:50000]  # cap to first 50K chars to avoid mega-files
                # SC 13 docs typically have "NAME OF REPORTING PERSON" or "Name of Reporting Person"
                # followed by the filer name on the next non-empty line, sometimes a few lines down
                m = re.search(
                    r"Name(?:s)?\s+of\s+Reporting\s+Person.*?(?:\n|<br/?>|</td>)\s*([^\n<]{3,80})",
                    text, re.IGNORECASE | re.DOTALL,
                )
                if m:
                    filer = re.sub(r"\s+", " ", m.group(1)).strip(" \t,.:")
                # Stake percentage: "PERCENT OF CLASS REPRESENTED" or "Percent of Class"
                m2 = re.search(
                    r"Percent(?:age)?\s+of\s+Class.*?([0-9]+\.?[0-9]*)\s*%",
                    text, re.IGNORECASE | re.DOTALL,
                )
                if m2:
                    stake = f"{m2.group(1)}%"
        except Exception:
            pass

        out.append({
            "date":      fdate.strftime("%Y-%m-%d"),
            "form":      form,
            "filer":     filer or "(see SEC filing)",
            "stake":     stake or "—",
            "filing_url": url,
            "is_active": "13G" not in form,  # 13D = active stake; 13G = passive
        })

    return out


def compute_smart_money_score(insider_data, institutional_data, filings_13_data):
    """
    Composite signal: GREEN / YELLOW / RED based on aggregate institutional intelligence.

    GREEN: insider cluster (>=5) AND/OR multiple smart-money funds AND/OR recent 13D activist
    YELLOW: at least one positive signal but mixed picture
    RED: insider selling, no smart money, passive-only ownership
    """
    score = 0
    reasons = []

    # Insider signal
    icount = insider_data.get("count", 0)
    icluster = insider_data.get("cluster_score", 0)
    if icluster >= 5:
        score += 2
        reasons.append(f"insider cluster ({insider_data.get('summary', '')})")
    elif icount >= 1:
        score += 1
        reasons.append(f"isolated insider buy ({insider_data.get('summary', '')})")

    # Smart money signal
    sm_count = institutional_data.get("smart_money_count", 0)
    if sm_count >= 3:
        score += 3
        reasons.append(f"{sm_count} smart-money funds in top holders")
    elif sm_count >= 1:
        score += 1
        reasons.append(f"{sm_count} smart-money fund(s) in top holders")

    # 13D/13G activity
    active_filings = sum(1 for f in filings_13_data if f.get("is_active"))
    passive_filings = sum(1 for f in filings_13_data if not f.get("is_active"))
    if active_filings >= 1:
        score += 2
        reasons.append(f"{active_filings} recent 13D activist filing(s)")
    if passive_filings >= 2:
        score += 1
        reasons.append(f"{passive_filings} recent 13G 5%+ filings")

    if score >= 5:
        label, color = "GREEN — High Smart Money Conviction", (39, 174, 96)
    elif score >= 2:
        label, color = "YELLOW — Mixed Signals", (200, 130, 20)
    else:
        label, color = "GREY — Insufficient Smart Money Evidence", (130, 130, 140)

    return {
        "score":   score,
        "label":   label,
        "color":   color,
        "reasons": reasons,
    }


if __name__ == "__main__":
    import json
    for t in ["IONQ", "AAPL"]:
        print(f"\n=== {t} ===")
        d = fetch_institutional_data(t)
        f = fetch_13d_13g_filings(t)
        print(f"Major holders: {d['major_holders']}")
        print(f"Top holders ({len(d['top_holders'])}):")
        for h in d['top_holders'][:5]:
            print(f"  {h['fund'][:40]:40s} {h['pct_out']*100:5.2f}%  {h['category']}")
        print(f"Smart money: {d['smart_money_count']} funds: {d['smart_money_funds']}")
        print(f"13D/13G filings ({len(f)}):")
        for fi in f[:5]:
            print(f"  {fi['date']}  {fi['form']:10s}  {fi['filer'][:30]:30s}  stake={fi['stake']}")
