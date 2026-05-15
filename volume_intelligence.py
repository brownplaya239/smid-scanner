"""
volume_intelligence.py — Price/volume analysis for institutional behavior detection

Three signals that together reveal "is real money buying/selling?":
  1. A/D Rating (O'Neill/IBD style) — letter grade A-E summarizing 13-week price/volume action
  2. Significant Volume Bars — find every >2x avg-volume day and classify as
       ACCUMULATION (price up), DISTRIBUTION (price down), ABSORPTION (price flat)
  3. Quarterly Trend — same A/D math broken into 3 monthly slices to show momentum

These are computed from price/volume only, no external data feeds needed.
The QoQ "proxy" approach: rather than 13F-vs-13F (paid data), we use the
price/volume footprint institutions leave behind — which is closer to real-time
than 13F filings (45-day lag) anyway.
"""

import pandas as pd


def compute_ad_rating(hist):
    """
    Accumulation/Distribution letter grade A-E based on weighted up-vol vs down-vol
    over the last 65 trading days (~13 weeks). Recent days weighted higher.

    Returns:
      grade:   "A" / "B" / "C" / "D" / "E" / "—"
      score:   0-100 (% of total volume on up days, time-weighted)
      label:   human-readable interpretation
      up_days, down_days, neutral_days: counts
    """
    if hist is None or len(hist) < 65:
        return {
            "grade": "—", "score": 0, "label": "insufficient history",
            "up_days": 0, "down_days": 0, "neutral_days": 0,
        }

    data = hist.tail(65).copy()
    avg_vol = data["Volume"].mean()
    if avg_vol <= 0:
        return {
            "grade": "—", "score": 0, "label": "no volume data",
            "up_days": 0, "down_days": 0, "neutral_days": 0,
        }

    up_w, down_w = 0.0, 0.0
    up_days, down_days, neutral_days = 0, 0, 0

    for i in range(1, len(data)):
        prev = float(data["Close"].iloc[i-1])
        curr = float(data["Close"].iloc[i])
        vol  = float(data["Volume"].iloc[i])
        # Linear time weight — recent days weighted higher
        time_w = i / len(data)
        # Volume factor — heavy-volume days count more (capped at 3x)
        vol_f = min(vol / avg_vol, 3.0)
        weighted = time_w * vol_f

        change_pct = (curr / prev - 1) * 100 if prev else 0
        if change_pct > 0.15:
            up_w += weighted
            up_days += 1
        elif change_pct < -0.15:
            down_w += weighted
            down_days += 1
        else:
            neutral_days += 1

    total = up_w + down_w
    if total <= 0:
        ratio = 0.5
    else:
        ratio = up_w / total

    score = round(ratio * 100, 1)
    if   ratio >= 0.65: grade, label = "A", "Strong Accumulation"
    elif ratio >= 0.55: grade, label = "B", "Accumulation"
    elif ratio >= 0.45: grade, label = "C", "Neutral / Mixed"
    elif ratio >= 0.35: grade, label = "D", "Distribution"
    else:               grade, label = "E", "Heavy Distribution"

    return {
        "grade":         grade,
        "score":         score,
        "label":         label,
        "up_days":       up_days,
        "down_days":     down_days,
        "neutral_days":  neutral_days,
    }


def find_significant_volume_bars(hist, lookback=90, threshold=2.0):
    """
    Find every day with >threshold * 20-day-avg-volume in the lookback window.
    Classify each:
      - ACCUMULATION: heavy volume + price up >1.5%   (institutions buying)
      - DISTRIBUTION: heavy volume + price down >1.5% (institutions selling)
      - ABSORPTION:   heavy volume + flat price       (silent build — most bullish for VCP setups)

    Returns list sorted newest first.
    """
    if hist is None or len(hist) < 30:
        return []

    data = hist.tail(lookback).copy()
    avg_vol = data["Volume"].rolling(20).mean()

    bars = []
    for i in range(20, len(data)):
        vol = float(data["Volume"].iloc[i])
        avg = float(avg_vol.iloc[i]) if not pd.isna(avg_vol.iloc[i]) else 0
        if avg <= 0:
            continue
        rel_vol = vol / avg
        if rel_vol < threshold:
            continue

        prev_close = float(data["Close"].iloc[i-1])
        close      = float(data["Close"].iloc[i])
        change_pct = (close / prev_close - 1) * 100 if prev_close else 0

        if change_pct > 1.5:
            classification, signal = "ACCUMULATION", "Bullish"
        elif change_pct < -1.5:
            classification, signal = "DISTRIBUTION", "Bearish"
        else:
            classification, signal = "ABSORPTION", "Stealth Buy"

        bars.append({
            "date":           data.index[i].strftime("%Y-%m-%d"),
            "close":          round(close, 2),
            "change_pct":     round(change_pct, 2),
            "volume":         int(vol),
            "rel_vol":        round(rel_vol, 1),
            "classification": classification,
            "signal":         signal,
        })

    bars.sort(key=lambda b: b["date"], reverse=True)
    return bars


def compute_monthly_flow(hist):
    """
    Break the last ~3 months into monthly chunks and show A/D ratio + price action
    for each. Reveals institutional momentum — is accumulation accelerating or fading?

    Returns list of 3 dicts (oldest first), each with:
      label, ratio, trend, price_chg, avg_vol_ratio
    """
    if hist is None or len(hist) < 65:
        return []

    data = hist.tail(66).copy()
    chunks = [(0, 22), (22, 44), (44, 66)]
    labels = ["3 Months Ago", "2 Months Ago", "Last Month"]

    overall_avg_vol = data["Volume"].mean()
    if overall_avg_vol <= 0:
        return []

    monthly = []
    for (start, end), label in zip(chunks, labels):
        chunk = data.iloc[start:end]
        if len(chunk) < 5:
            continue

        up_vol, down_vol = 0.0, 0.0
        for j in range(1, len(chunk)):
            prev = float(chunk["Close"].iloc[j-1])
            curr = float(chunk["Close"].iloc[j])
            vol  = float(chunk["Volume"].iloc[j])
            if curr > prev * 1.0015:
                up_vol += vol
            elif curr < prev * 0.9985:
                down_vol += vol

        total = up_vol + down_vol
        ratio = (up_vol / total) if total > 0 else 0.5

        if   ratio >= 0.65: trend = "Strong Accumulation"
        elif ratio >= 0.55: trend = "Accumulation"
        elif ratio >= 0.45: trend = "Mixed"
        elif ratio >= 0.35: trend = "Distribution"
        else:               trend = "Heavy Distribution"

        price_chg = (float(chunk["Close"].iloc[-1]) / float(chunk["Close"].iloc[0]) - 1) * 100
        monthly.append({
            "label":         label,
            "ratio":         round(ratio * 100, 1),
            "trend":         trend,
            "price_chg":     round(price_chg, 2),
            "avg_vol_ratio": round(float(chunk["Volume"].mean()) / overall_avg_vol, 2),
        })

    return monthly


def detect_silent_build(hist, lookback=20, vol_dryup_threshold=0.85, price_band_pct=4.0):
    """
    Find the institutional 'silent build' pattern: vol dries up while price holds in
    a tight range. This is the highest-conviction accumulation pattern (Qullamaggie's
    dry-up signal + tight base = institutions absorbing supply at a fixed price).

    Returns dict: {detected: bool, days_in_pattern: int, price_band: (low, high), notes: str}
    """
    if hist is None or len(hist) < lookback + 20:
        return {"detected": False, "notes": "insufficient history"}

    data = hist.tail(lookback + 20).copy()
    recent = data.tail(lookback)
    prior  = data.iloc[-(lookback + 20):-lookback]

    recent_avg_vol = float(recent["Volume"].mean())
    prior_avg_vol  = float(prior["Volume"].mean())
    if prior_avg_vol <= 0:
        return {"detected": False, "notes": "no prior volume baseline"}

    vol_ratio = recent_avg_vol / prior_avg_vol
    high  = float(recent["Close"].max())
    low   = float(recent["Close"].min())
    band_pct = (high / low - 1) * 100 if low > 0 else 999

    detected = (vol_ratio < vol_dryup_threshold) and (band_pct < price_band_pct)
    return {
        "detected":         detected,
        "days_in_pattern":  lookback if detected else 0,
        "vol_dryup_pct":    round((1 - vol_ratio) * 100, 1),
        "price_band_pct":   round(band_pct, 2),
        "price_low":        round(low, 2),
        "price_high":       round(high, 2),
        "notes": ("Silent-build pattern detected: vol dried up "
                  f"{(1 - vol_ratio)*100:.0f}% vs prior period while price held "
                  f"within a {band_pct:.1f}% band — classic institutional absorption."
                  if detected else
                  "No silent-build pattern in last 20 days."),
    }


if __name__ == "__main__":
    import yfinance as yf
    for tk in ["IONQ", "BKSY"]:
        h = yf.Ticker(tk).history(period="200d", interval="1d")
        ad = compute_ad_rating(h)
        bars = find_significant_volume_bars(h)
        flow = compute_monthly_flow(h)
        silent = detect_silent_build(h)
        print(f"\n=== {tk} ===")
        print(f"A/D Rating: {ad['grade']} ({ad['label']}, score {ad['score']}, "
              f"{ad['up_days']}up / {ad['down_days']}down / {ad['neutral_days']}flat)")
        print(f"Significant volume bars (last 90d): {len(bars)}")
        for b in bars[:5]:
            print(f"  {b['date']}  {b['rel_vol']:.1f}x  {b['change_pct']:+.1f}%  -> {b['classification']}")
        print(f"Monthly flow:")
        for m in flow:
            print(f"  {m['label']:14s} {m['ratio']:5.1f}% up-vol  ({m['trend']:22s})  price {m['price_chg']:+.1f}%")
        print(f"Silent build: {silent['notes']}")
