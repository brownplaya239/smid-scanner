"""
report_archive.py — Publishes report PDFs to the GitHub Pages site.

Each scan/lookup saves its PDF into docs/reports/ and rebuilds
docs/reports/manifest.json, which the site (docs/index.html) reads to
populate the report tabs. Old reports are pruned to keep the repo lean.
"""

import os
import re
import json
import glob
import urllib.request
import urllib.error
from datetime import datetime, timezone

_BASE       = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(_BASE, "docs", "reports")

TYPE_LABELS = {
    "smid-scanner":    "SMID Scanner",
    "iwm-scanner":     "IWM Scanner",
    "smid-setup":      "SMID Setup Builder",
    "iwm-setup":       "IWM Setup Builder",
    "qm-monthly":      "QM Monthly Gainers",
    "stockbee-weekly": "Stockbee Weekly 20%",
    "adhoc":           "Ad-Hoc Lookups",
    "alt-data":        "Alt-Data Intel",
}


def save_report(pdf_bytes, filename):
    """Write a report PDF into docs/reports/ for the Pages archive."""
    try:
        os.makedirs(REPORTS_DIR, exist_ok=True)
        path = os.path.join(REPORTS_DIR, filename)
        with open(path, "wb") as f:
            f.write(pdf_bytes)
        print(f"  Archived to site: docs/reports/{filename}")
        return path
    except Exception as e:
        print(f"  Site archive failed: {e}")
        return None


def _classify(fn):
    """Map a report filename to (type_key, ticker_or_None)."""
    if fn.startswith("smid_scanner_"):    return "smid-scanner",    None
    if fn.startswith("iwm_scanner_"):     return "iwm-scanner",     None
    if fn.startswith("smid_setup_"):      return "smid-setup",      None
    if fn.startswith("iwm_setup_"):       return "iwm-setup",       None
    if fn.startswith("qm_monthly_"):      return "qm-monthly",      None
    if fn.startswith("stockbee_weekly_"): return "stockbee-weekly", None
    if fn.startswith("ticker_"):
        m = re.match(r"ticker_([A-Za-z.\-]+)_", fn)
        return "adhoc", (m.group(1).upper() if m else None)
    if fn.startswith("altdata_"):
        m = re.match(r"altdata_([A-Za-z.\-]+)_", fn)
        return "alt-data", (m.group(1).upper() if m else None)
    return None, None


def rebuild_manifest(keep_per_type=30):
    """Scan docs/reports/, prune old PDFs, write manifest.json."""
    if not os.path.isdir(REPORTS_DIR):
        return

    files = [os.path.basename(p) for p in glob.glob(os.path.join(REPORTS_DIR, "*.pdf"))]
    groups = {k: [] for k in TYPE_LABELS}

    for fn in files:
        typ, sym = _classify(fn)
        if typ is None:
            continue
        m = re.search(r"(\d{4}-\d{2}-\d{2})_(\d{2})(\d{2})\.pdf$", fn)
        if not m:
            continue
        date, hh, mm = m.group(1), int(m.group(2)), int(m.group(3))
        try:
            dt    = datetime.strptime(date, "%Y-%m-%d")
            hour12 = hh % 12 or 12
            ampm   = "AM" if hh < 12 else "PM"
            label  = dt.strftime("%b %d, %Y") + f"  -  {hour12}:{mm:02d} {ampm} ET"
        except Exception:
            label = f"{date} {hh:02d}:{mm:02d}"
        entry = {"file": fn, "sort": f"{date}_{hh:02d}{mm:02d}", "label": label}
        if sym:
            entry["ticker"] = sym
        groups[typ].append(entry)

    manifest = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reports":   {},
    }
    for typ, entries in groups.items():
        entries.sort(key=lambda e: e["sort"], reverse=True)
        keep, prune = entries[:keep_per_type], entries[keep_per_type:]
        for e in prune:
            try:
                os.remove(os.path.join(REPORTS_DIR, e["file"]))
            except Exception:
                pass
        manifest["reports"][typ] = [
            {k: v for k, v in e.items() if k != "sort"} for e in keep
        ]

    with open(os.path.join(REPORTS_DIR, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)

    total = sum(len(v) for v in manifest["reports"].values())
    print(f"  Manifest rebuilt: {total} reports archived on site")


def archive(pdf_bytes, filename):
    """Convenience: save a PDF and rebuild the manifest in one call."""
    save_report(pdf_bytes, filename)
    rebuild_manifest()


# ─────────────────────────────────────────────────────────────────────
# Per-user private report storage
#
# Ad-hoc + alt-data reports are user-specific research, so instead of the
# public docs/reports archive they go to a PRIVATE Supabase Storage bucket
# scoped by user_id. The pipeline uploads with the service key (CI secret);
# the worker mints signed URLs for the client. Nothing hits the public site.
# ─────────────────────────────────────────────────────────────────────

def _sb_env():
    return (os.environ.get("SUPABASE_URL", "").rstrip("/"),
            os.environ.get("SUPABASE_SERVICE_KEY", ""))


def _link_report_generation(url_base, key, user_id, ticker, kind, storage_path):
    """Attach storage_path (+ mark done) onto the most-recent queued
    report_generations row for (user, ticker); insert one if none exists."""
    hdr = {"Authorization": f"Bearer {key}", "apikey": key,
           "Content-Type": "application/json"}
    body = {"storage_path": storage_path, "kind": kind, "status": "done",
            "completed_at": datetime.now(timezone.utc).isoformat()}
    q = (f"{url_base}/rest/v1/report_generations?user_id=eq.{user_id}"
         f"&ticker=eq.{ticker}&storage_path=is.null"
         f"&order=created_at.desc&limit=1&select=id")
    req = urllib.request.Request(q, headers={**hdr, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        rows = json.loads(r.read().decode("utf-8"))
    if rows:
        patch = urllib.request.Request(
            f"{url_base}/rest/v1/report_generations?id=eq.{rows[0]['id']}",
            data=json.dumps(body).encode("utf-8"),
            headers={**hdr, "Prefer": "return=minimal"}, method="PATCH")
        urllib.request.urlopen(patch, timeout=20).read()
    else:
        body.update({"user_id": user_id, "ticker": ticker, "report_type": kind})
        ins = urllib.request.Request(
            f"{url_base}/rest/v1/report_generations",
            data=json.dumps(body).encode("utf-8"),
            headers={**hdr, "Prefer": "return=minimal"}, method="POST")
        urllib.request.urlopen(ins, timeout=20).read()


def upload_user_report(pdf_bytes, filename, user_id, ticker, kind):
    """Upload an ad-hoc/alt-data PDF to user-reports/<user_id>/<filename> and
    link it onto the user's report_generations row. Returns True on success.
    Returns False (so the caller falls back to the public archive) when the
    Storage bucket / creds aren't configured yet — safe pre-setup rollout."""
    url_base, key = _sb_env()
    if not (url_base and key and user_id):
        return False
    storage_path = f"{user_id}/{filename}"
    try:
        req = urllib.request.Request(
            f"{url_base}/storage/v1/object/user-reports/{storage_path}",
            data=pdf_bytes,
            headers={"Authorization": f"Bearer {key}", "apikey": key,
                     "Content-Type": "application/pdf", "x-upsert": "true"},
            method="POST")
        with urllib.request.urlopen(req, timeout=45) as r:
            r.read()
    except Exception as e:
        print(f"  Private upload failed ({e}); falling back to public archive.")
        return False
    try:
        _link_report_generation(url_base, key, user_id, ticker, kind, storage_path)
    except Exception as e:
        print(f"  report_generations link failed (non-fatal): {e}")
    print(f"  Uploaded private report: user-reports/{storage_path}")
    return True


if __name__ == "__main__":
    rebuild_manifest()
