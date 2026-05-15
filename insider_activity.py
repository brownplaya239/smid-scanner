"""
insider_activity.py — Form 4 insider buying signal from SEC EDGAR
Free, authoritative data. The single strongest publicly-available alpha factor.

Output per ticker:
  - count:          # of open-market buys in last N days
  - value_usd:      total $ value of buys
  - senior_buys:    # of buys by C-suite (CEO/CFO/COO/President)
  - unique_insiders: # of distinct insiders buying (cluster signal)
  - cluster_score:  composite signal strength
"""

import os
import re
import time
import requests
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET

# SEC EDGAR requires a User-Agent with contact info
SEC_HEADERS = {
    "User-Agent": "SMID Breakout Scanner sumeetsancheti97@gmail.com",
    "Accept-Encoding": "gzip, deflate",
}

# Rate limit: SEC limits to 10 req/sec — we self-limit to 7/sec for safety
_LAST_REQUEST_TIME = [0.0]
_MIN_INTERVAL = 1.0 / 7.0


def _throttle():
    """Self-limit to stay under SEC's 10 req/sec ceiling."""
    elapsed = time.time() - _LAST_REQUEST_TIME[0]
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)
    _LAST_REQUEST_TIME[0] = time.time()


# Cache CIK lookup (built once, reused for whole scan)
_CIK_CACHE = {}


def _load_cik_lookup():
    """Fetch the SEC's ticker→CIK mapping. ~10K entries, ~1.5MB."""
    if _CIK_CACHE:
        return _CIK_CACHE

    try:
        _throttle()
        resp = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers=SEC_HEADERS, timeout=20
        )
        if resp.status_code != 200:
            return _CIK_CACHE
        data = resp.json()
        for entry in data.values():
            ticker = entry.get("ticker", "").upper().strip()
            cik = str(entry.get("cik_str", "")).zfill(10)
            if ticker and cik:
                _CIK_CACHE[ticker] = cik
    except Exception as e:
        print(f"  ⚠️  CIK lookup failed: {e}")

    return _CIK_CACHE


def _empty_result():
    return {
        "count":           0,
        "value_usd":       0,
        "senior_buys":     0,
        "unique_insiders": 0,
        "cluster_score":   0,
        "summary":         "",
    }


def fetch_insider_activity(ticker, days_back=60, max_filings=15):
    """
    Returns insider buying activity for a single ticker over the last N days.
    Counts only open-market purchases (transaction code 'P').
    """
    result = _empty_result()
    ticker = ticker.upper().strip()

    cik_map = _load_cik_lookup()
    cik = cik_map.get(ticker)
    if not cik:
        return result

    # 1. Get list of recent filings for this CIK
    try:
        _throttle()
        resp = requests.get(
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            headers=SEC_HEADERS, timeout=15
        )
        if resp.status_code != 200:
            return result
        filings = resp.json().get("filings", {}).get("recent", {})
    except Exception:
        return result

    forms     = filings.get("form", [])
    accs      = filings.get("accessionNumber", [])
    dates     = filings.get("filingDate", [])
    primaries = filings.get("primaryDocument", [])

    cutoff = datetime.now().date() - timedelta(days=days_back)

    form4_indices = []
    for i, form in enumerate(forms):
        if form == "4" and i < len(accs) and i < len(dates) and i < len(primaries):
            try:
                fdate = datetime.strptime(dates[i], "%Y-%m-%d").date()
                if fdate >= cutoff:
                    form4_indices.append(i)
            except Exception:
                continue

    if not form4_indices:
        return result

    # 2. Parse each Form 4 XML for transaction details
    # SEC stores XSLT-styled .xml files at /Archives/edgar/data/{cik_int}/{acc_clean}/{primaryDocument}
    # primaryDocument may be e.g. "xslF345X06/form4.xml" — but the raw XML is at the path WITHOUT the xsl prefix.
    cik_int = int(cik.lstrip("0")) if cik.lstrip("0") else int(cik)
    unique_insiders = set()
    senior_count = 0
    buy_count = 0
    total_value = 0.0

    for i in form4_indices[:max_filings]:
        acc_clean = accs[i].replace("-", "")
        primary   = primaries[i]
        # The primaryDocument path includes the xsl wrapper; the raw XML is the basename only
        # e.g. "xslF345X06/form4.xml" → fetch as "form4.xml"
        raw_name = primary.split("/")[-1]
        url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}/{raw_name}"

        try:
            _throttle()
            r = requests.get(url, headers=SEC_HEADERS, timeout=10)
            if r.status_code != 200 or not r.content:
                continue

            # Parse XML — Form 4 has a stable schema
            try:
                root = ET.fromstring(r.content)
            except ET.ParseError:
                continue

            # Reporting owner info
            owner_name = (root.findtext(".//reportingOwner/reportingOwnerId/rptOwnerName") or "").strip()
            officer_title = (root.findtext(".//reportingOwner/reportingOwnerRelationship/officerTitle") or "").lower()
            is_officer = root.findtext(".//reportingOwner/reportingOwnerRelationship/isOfficer") == "1"

            is_senior = is_officer and any(t in officer_title for t in [
                "ceo", "cfo", "coo", "chief executive", "chief financial",
                "chief operating", "president"
            ])

            # Walk through nonDerivative purchases (code P = open market buy)
            had_buy = False
            for txn in root.findall(".//nonDerivativeTransaction"):
                code = (txn.findtext(".//transactionCoding/transactionCode") or "").strip()
                if code != "P":
                    continue

                shares_str = txn.findtext(".//transactionShares/value") or "0"
                price_str  = txn.findtext(".//transactionPricePerShare/value") or "0"

                try:
                    shares = float(shares_str)
                    price  = float(price_str)
                except (ValueError, TypeError):
                    continue

                if shares > 0 and price > 0:
                    buy_count += 1
                    total_value += shares * price
                    had_buy = True
                    if is_senior:
                        senior_count += 1

            if had_buy and owner_name:
                unique_insiders.add(owner_name)

        except Exception:
            continue

    # 3. Compute cluster score — multiple insiders + senior involvement = highest signal
    cluster_score = 0
    if buy_count > 0:
        # Base: number of unique insiders
        cluster_score = len(unique_insiders) * 2
        # Senior officer multiplier
        cluster_score += senior_count * 3
        # Bonus for material dollar size (>$250K = serious buy)
        if total_value >= 1_000_000: cluster_score += 5
        elif total_value >= 250_000: cluster_score += 2

    # Build human-readable summary
    summary = ""
    if buy_count > 0:
        parts = [f"{buy_count} buy{'s' if buy_count != 1 else ''}"]
        if len(unique_insiders) > 1:
            parts.append(f"{len(unique_insiders)} distinct insiders")
        if senior_count > 0:
            parts.append(f"{senior_count} C-suite")
        if total_value >= 1_000_000:
            parts.append(f"${total_value/1e6:.1f}M total")
        elif total_value >= 1_000:
            parts.append(f"${total_value/1e3:.0f}K total")
        summary = ", ".join(parts) + f" (last {days_back}d)"

    return {
        "count":           buy_count,
        "value_usd":       int(total_value),
        "senior_buys":     senior_count,
        "unique_insiders": len(unique_insiders),
        "cluster_score":   cluster_score,
        "summary":         summary,
    }


def fetch_insider_transactions_detail(ticker, days_back=365, max_filings=40):
    """
    Returns a list of transaction-level dicts for the last N days.
    Includes both buys (P) AND sales (S) so the table shows the full picture.
    Each row: {date, owner, title, code, shares, price, value, code_label}
    """
    transactions = []
    ticker = ticker.upper().strip()

    cik_map = _load_cik_lookup()
    cik = cik_map.get(ticker)
    if not cik:
        return transactions

    try:
        _throttle()
        resp = requests.get(
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            headers=SEC_HEADERS, timeout=15
        )
        if resp.status_code != 200:
            return transactions
        filings = resp.json().get("filings", {}).get("recent", {})
    except Exception:
        return transactions

    forms     = filings.get("form", [])
    accs      = filings.get("accessionNumber", [])
    dates     = filings.get("filingDate", [])
    primaries = filings.get("primaryDocument", [])

    cutoff = datetime.now().date() - timedelta(days=days_back)

    form4_indices = []
    for i, form in enumerate(forms):
        if form == "4" and i < len(accs) and i < len(dates) and i < len(primaries):
            try:
                fdate = datetime.strptime(dates[i], "%Y-%m-%d").date()
                if fdate >= cutoff:
                    form4_indices.append(i)
            except Exception:
                continue

    cik_int = int(cik.lstrip("0")) if cik.lstrip("0") else int(cik)

    code_labels = {
        "P": "Buy",  "S": "Sale",  "M": "Opt Exer",  "A": "Grant",
        "F": "Tax W/H", "D": "Disposition", "G": "Gift", "C": "Convert",
    }

    for i in form4_indices[:max_filings]:
        acc_clean = accs[i].replace("-", "")
        primary   = primaries[i]
        raw_name  = primary.split("/")[-1]
        url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}/{raw_name}"

        try:
            _throttle()
            r = requests.get(url, headers=SEC_HEADERS, timeout=10)
            if r.status_code != 200 or not r.content:
                continue

            try:
                root = ET.fromstring(r.content)
            except ET.ParseError:
                continue

            owner_name = (root.findtext(".//reportingOwner/reportingOwnerId/rptOwnerName") or "").strip()
            officer_title = (root.findtext(".//reportingOwner/reportingOwnerRelationship/officerTitle") or "").strip()
            is_director = root.findtext(".//reportingOwner/reportingOwnerRelationship/isDirector") == "1"

            title_disp = officer_title or ("Director" if is_director else "10% Owner")

            for txn in root.findall(".//nonDerivativeTransaction"):
                code = (txn.findtext(".//transactionCoding/transactionCode") or "").strip()
                if code not in code_labels:
                    continue

                txn_date = txn.findtext(".//transactionDate/value") or dates[i]
                shares_str = txn.findtext(".//transactionShares/value") or "0"
                price_str  = txn.findtext(".//transactionPricePerShare/value") or "0"

                try:
                    shares = float(shares_str)
                    price  = float(price_str)
                except (ValueError, TypeError):
                    continue

                if shares <= 0:
                    continue

                transactions.append({
                    "date":       txn_date,
                    "owner":      owner_name,
                    "title":      title_disp,
                    "code":       code,
                    "code_label": code_labels[code],
                    "shares":     shares,
                    "price":      price,
                    "value":      shares * price,
                })

        except Exception:
            continue

    # Sort newest first
    transactions.sort(key=lambda t: t["date"], reverse=True)
    return transactions


def enrich_candidates_with_insiders(candidates, days_back=60):
    """Attach insider activity to each candidate dict in-place."""
    print(f"  Fetching insider activity for {len(candidates)} candidates...")
    for c in candidates:
        try:
            insider = fetch_insider_activity(c.get("ticker", ""), days_back=days_back)
        except Exception:
            insider = _empty_result()
        c["insider_count"]     = insider["count"]
        c["insider_value"]     = insider["value_usd"]
        c["insider_senior"]    = insider["senior_buys"]
        c["insider_cluster"]   = insider["cluster_score"]
        c["insider_summary"]   = insider["summary"]
    return candidates


if __name__ == "__main__":
    # Smoke test
    import json
    for t in ["AAPL", "BKSY", "BE", "TSLA"]:
        r = fetch_insider_activity(t, days_back=90)
        print(f"{t}: {r['summary'] or 'no recent buys'}  (cluster={r['cluster_score']})")
