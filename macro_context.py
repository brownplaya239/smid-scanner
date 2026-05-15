"""
macro_context.py — Market regime overlay for scanner / setup builder
Pulls SPY, IWM, VIX, sector ETFs and computes risk regime signals.
Free data via yfinance.
"""

import yfinance as yf
import pytz
from datetime import datetime

ET = pytz.timezone("America/New_York")


def fetch_macro_context():
    """
    Returns a dict with current macro regime indicators.
    Used for both prompt context and report header display.
    """
    out = {
        "regime":             "Unknown",
        "regime_description": "",
        "spy_20d_pct":        0.0,
        "iwm_20d_pct":        0.0,
        "iwm_spy_trend":      0.0,
        "vix":                0.0,
        "vix_change_20d":     0.0,
        "tnx_yield":          0.0,
        "leading_sectors":    [],
        "lagging_sectors":    [],
    }

    try:
        # Bulk download — much faster than individual calls
        symbols = ["SPY", "IWM", "^VIX", "^TNX"]
        data = yf.download(symbols, period="60d", interval="1d",
                           progress=False, auto_adjust=False, group_by="ticker")

        def _series(sym):
            try:
                return data[sym]["Close"].dropna()
            except Exception:
                return None

        spy = _series("SPY")
        iwm = _series("IWM")
        vix = _series("^VIX")
        tnx = _series("^TNX")

        if spy is not None and len(spy) >= 20:
            out["spy_20d_pct"] = round((spy.iloc[-1] / spy.iloc[-20] - 1) * 100, 2)

        if iwm is not None and len(iwm) >= 20:
            out["iwm_20d_pct"] = round((iwm.iloc[-1] / iwm.iloc[-20] - 1) * 100, 2)

            # IWM/SPY ratio trend = small cap relative leadership
            if spy is not None and len(spy) >= 20:
                ratio_now    = iwm.iloc[-1] / spy.iloc[-1]
                ratio_20d_ago = iwm.iloc[-20] / spy.iloc[-20]
                out["iwm_spy_trend"] = round((ratio_now / ratio_20d_ago - 1) * 100, 2)

        if vix is not None and len(vix) >= 20:
            out["vix"] = round(float(vix.iloc[-1]), 2)
            out["vix_change_20d"] = round(float(vix.iloc[-1] - vix.iloc[-20:].mean()), 2)

        if tnx is not None and len(tnx) >= 1:
            out["tnx_yield"] = round(float(tnx.iloc[-1]) / 10, 2)

        # Sector ETFs — XL_ family, find leaders/laggards by 20d return
        sector_etfs = {
            "XLK": "Technology", "XLF": "Financials", "XLV": "Healthcare",
            "XLE": "Energy",     "XLY": "Cons Disc",  "XLP": "Cons Staples",
            "XLI": "Industrials","XLB": "Materials",  "XLRE": "Real Estate",
            "XLU": "Utilities",  "XLC": "Comms",
        }
        sector_perf = []
        try:
            sector_data = yf.download(list(sector_etfs.keys()), period="30d", interval="1d",
                                      progress=False, auto_adjust=False, group_by="ticker")
            for etf, name in sector_etfs.items():
                try:
                    s = sector_data[etf]["Close"].dropna()
                    if len(s) >= 20:
                        ret = (s.iloc[-1] / s.iloc[-20] - 1) * 100
                        sector_perf.append((name, round(ret, 2)))
                except Exception:
                    pass
        except Exception:
            pass

        if sector_perf:
            sector_perf.sort(key=lambda x: x[1], reverse=True)
            out["leading_sectors"] = [f"{n} ({r:+.1f}%)" for n, r in sector_perf[:3]]
            out["lagging_sectors"] = [f"{n} ({r:+.1f}%)" for n, r in sector_perf[-3:]]

        # Determine regime — composite of VIX, SPY trend, IWM relative strength
        vix_now    = out["vix"]
        spy_20d    = out["spy_20d_pct"]
        iwm_lead   = out["iwm_spy_trend"]

        if vix_now > 0:
            if vix_now < 16 and spy_20d > 1 and iwm_lead > 0:
                out["regime"] = "Risk-On (Broad)"
                out["regime_description"] = "VIX low, SPY trending up, small caps leading. Best environment for breakouts."
            elif vix_now < 18 and spy_20d > 0:
                if iwm_lead > 0:
                    out["regime"] = "Risk-On (Small-Cap Leadership)"
                    out["regime_description"] = "Small caps outperforming. SMID/IWM breakouts have tailwind."
                else:
                    out["regime"] = "Risk-On (Large-Cap Leadership)"
                    out["regime_description"] = "Mega caps leading. SMID breakouts face crosswind — be selective."
            elif vix_now > 22 or spy_20d < -3:
                out["regime"] = "Risk-Off"
                out["regime_description"] = "Elevated volatility / weak SPY. Most breakouts will fail. Reduce conviction or step aside."
            elif vix_now > 25 or spy_20d < -6:
                out["regime"] = "Risk-Off (Stress)"
                out["regime_description"] = "High stress. Even strong setups likely to fail. Best to wait."
            else:
                out["regime"] = "Mixed / Transitional"
                out["regime_description"] = "Neither clearly risk-on nor risk-off. Treat all signals with reduced conviction."

    except Exception as e:
        out["regime_description"] = f"Macro fetch failed: {e}"

    return out


if __name__ == "__main__":
    import json
    ctx = fetch_macro_context()
    print(json.dumps(ctx, indent=2))
