# AI Trade Desk — architecture, alpha methodology, and honest findings

**Version:** trade_desk_v1 / alpha_score_v1 · **Shipped:** 2026-08 ·
**Governing principle: ALPHA IS PARAMOUNT.** No strategy family surfaces an
actionable Top Idea unless its own measured, walk-forward-validated record
says it has edge. Abstention is the designed default, not a failure state.

---

## 1. Architecture

TickerDesk is a batch pipeline (GitHub Actions crons → Python modules →
static JSONs in `docs/reports/` → GitHub Pages single-file frontend) plus a
Cloudflare Worker (`api.tickerdesk.io`) for live quotes/chains and the
LLM-backed endpoints. The Trade Desk reuses that architecture wholesale —
no parallel systems:

```
uoa_scanner.py ──► data/uoa_signals.jsonl        (append-only signal ledger)
uoa_alpha.py   ──► data/uoa_alpha_cache.json     (frozen matured outcomes)
               ──► docs/reports/uoa_edge.json    (measured edge, IC, deciles)
earnings_ideas.py / scan_outcomes.py / grade_engine.py / regime loop
        │
        ▼
trade_desk_validation.py  walk-forward validation of the Alpha Score
        │                 → docs/reports/trade_desk_validation.json
        ▼
trade_desk.py             candidates → features → hard filters → score →
        │                 family gates → construction → immutable ledger
        │                 → docs/reports/trade_desk.json
        ▼
docs/index.html #panel-trade-desk   (UI: Top Ideas / abstention / watch /
        │                            gates / calibration / What Changed)
        ▼
worker /ask-desk (POST, auth-gated)  LLM Q&A over the published JSON only
```

Both run in `uoa.yml` after `uoa_alpha.py`, every ~30 min during RTH,
`continue-on-error` so the flow publish never blocks on the desk.

## 2. Point-in-time correctness (P0)

- **Signals**: `uoa_signals.jsonl` is append-only; features are written at
  flag time (`flagged_at`, `underlying_px_at_flag`, liquidity, side, DTE…).
- **Outcomes**: `uoa_alpha_cache.json` freezes a signal's forward returns
  once every horizon has matured (35 days); frozen entries are never
  recomputed.
- **The at-flag feature vector** (`features_at_flag`, shared by import
  between validation and engine) contains ONLY flag-time fields. Next-day
  OI confirmation and forward returns are structurally excluded; a
  permanent regression test (`test_features_at_flag_has_no_lookahead_fields`)
  guards this.
- **Regime** joins on the *flag date's* label from `regime_history.json`,
  never today's.
- **Trade ideas**: `data/trade_desk_log.jsonl` is an append-only event
  ledger (`issued` / `graded` lines). Issuance freezes the full idea with
  `as_of`, `data_cutoff`, `engine_version`, `model_version`. Re-runs are
  idempotent per (family, ticker, direction, day); nothing is ever
  rewritten (tests: `test_ledger_append_only_and_idempotent`).

## 3. Alpha Score v1 — what it is and what the data says

**Definition.** Additive expected-excess model on categorical flag-time
features (side, regime, into-earnings, DTE bucket, liquidity, cap bucket,
opening read, signal type, premium band). Per-feature deviations from the
training-window baseline are shrunk `n/(n+200)` and clamped (±2 pp per
feature, ±6 pp total) — the house edge-weights contract. The score maps
expected +5d direction-signed excess vs SPY to its percentile in the
training distribution: **90 means "top decile of expected edge", not "90%
win probability."**

**Validation (2026-08, 27,521 graded buyer-initiated signals,
2026-05-18 → 2026-06-30, 3 expanding folds + 15% untouched holdout):**

| window | n | IC | score≥80 avg signed excess |
|---|---|---|---|
| fold 1 | 5,848 | **−0.194** | −2.83 pp (n=1,487) |
| fold 2 | 5,848 | +0.004 | +1.54 pp (n=709) |
| fold 3 | 5,849 | −0.127 | −1.06 pp (n=948) |
| holdout | 4,128 | **−0.185** | −0.72 pp (n=682) |

Raw `trade_score` baseline IC over the same windows: −0.06 / −0.01 / +0.02.

**Verdict: `no_validated_edge`.** No cutoff produced positive average AND
median signed excess in every fold. Fixed rule families fared no better —
each flips sign across fortnights (into-earnings: +5.5 → −0.8 → +0.4 pp;
put-buys: −6.1 → +0.9 → +3.3 pp; calls the mirror image). The window is a
single ~11-week macro stretch; regime effects dominate and the record is
too short to separate edge from regime.

**Consequence, by design:** the flow family's gate holds at WATCH, the UI
abstains from Top Ideas, and the paper-forward ledger accrues the record
that can change the verdict. The validator re-runs every batch in CI; the
gate flips to `qualified` automatically the day the growing frozen record
clears it — no human flips a constant.

## 4. Strategy family gates (all measured, auto-promoting)

| family | source of truth | state at ship | rule |
|---|---|---|---|
| flow | `trade_desk_validation.json` | watch (`no_validated_edge`) | validated cutoff → qualified |
| earnings/post_report_drift | `earnings_ideas.json` by_type | **degraded** (EV −0.9, n=82) | EV>0 at n≥30 → qualified |
| earnings/vol_rich, vol_cheap, momentum_into_print | same | watch (accruing, n<30) | same |
| momentum scans | `scan_outcomes.json` | **degraded** (EV −1.4, PF 0.74, n=737) | EV>0 at n≥30 → qualified |

Degraded families still generate candidates (they freeze into the ledger
and keep grading — a degraded family can rehabilitate itself) but are
rejected from display with `STRATEGY_DEGRADED`.

## 5. Hard filters (every rejection carries a reason)

`BAD_LIQUIDITY` (grade D contract) · `LOW_PREMIUM` (<$250k cluster) ·
`STALE_DATA` (signal >90 min old, source JSON >48 h old, regime label >5
days old → unknown) · `REGIME_CONFLICT` (bullish flow in risk_off/mixed —
measured EV −3.08/−4.52 on n=15,445/5,872, `uoa_edge.by_regime`) ·
`LOW_ALPHA` (score <70) · `INSUFFICIENT_DATA` · `NOT_DIRECTIONAL`
(sellers/hedge/income prints) · `STRATEGY_DEGRADED`. Counts publish in
`trade_desk.json.rejections`.

## 6. Trade construction (v1 scope)

Flow ideas carry the flagged reference contract (symbol, DTE, flag
premium/volume/OI, liquidity grade, underlying at flag) and an expression
recommendation (`reference_contract` for A/B liquidity, `underlying`
otherwise). Live option marks are NOT available to the batch engine —
those fields are `null` and the UI renders **"Unavailable"**; no invented
prices, ever. Spread-aware structure selection (verticals vs longs vs
stock, IV-crush comparison) is deferred until a family qualifies — pricing
a trade nobody may act on is premature.

## 7. Grading, performance, calibration

Issued ideas with a bullish/bearish direction grade at +5 sessions,
close-to-close from the issue day's close (documented conservative fill
convention — no intraday fill assumed), direction-signed excess vs SPY via
yfinance. Stats gate at n≥30; the Alpha-Score calibration table (80-100 /
60-79 / 40-59 / <40 → hit, avg) publishes with per-bucket accrual counts
from day one. Monotonic calibration is the score's report card; if it
fails, the score gets fixed, not the display.

## 8. Ask TickerDesk (LLM layer)

`POST https://api.tickerdesk.io/ask-desk {question}` — Supabase
auth-gated (`requireUser`, fails closed), 300-char cap, 5-min memory
cache. Claude Haiku receives ONLY the published `trade_desk.json` +
slimmed validation JSON with a contract: every number must trace to the
payload, no invented prices/probabilities/backtests, FACT vs INFERENCE
distinguished, no personalized advice. The LLM is an interface over the
deterministic output — it is never in the scoring path.

## 9. MCP readiness

The service surface is already tool-shaped, stateless, and JSON-typed:

| future MCP tool | today's implementation |
|---|---|
| `scan_opportunities` | `trade_desk.json` (top_ideas/watch/rejections) |
| `get_market_regime` | `regime_history.json` |
| `get_signal_history` | `uoa_signals_scored.json` / `uoa_edge.json` |
| `get_trade_idea` | ledger `issued` events by id |
| `backtest_setup` | `trade_desk_validation.py` (as_of-safe by construction) |
| `ask` | worker `/ask-desk` |

An MCP server would wrap these same files/endpoints behind typed tools
with the worker's existing auth. `orders.*` scopes are intentionally
absent — no execution surface exists.

## 10. Known limitations (do not hide these)

1. **History is short and one-regime** (11 weeks live flow; regime labels
   only from 2026-07-06). Everything above re-runs as the record grows.
2. **Overlapping signals** (same ticker, same day) inflate effective n;
   ICs are honest but confidence intervals would be optimistic.
3. **Underlying-level grading**: +5d stock excess, not option P/L. An
   option-P/L grading loop needs historical option marks (Polygon has
   them; CI-only key) — the top backlog item once a family qualifies.
4. **35-day frozen-cache lag**: validation sees signals matured ≥35 days
   ago. Fresher slices are visible in `uoa_edge.json` counterfactuals but
   are not decomposable into stability windows there.
5. **No execution modeling yet** (fills, spreads, slippage) — moot while
   ideas are paper-tracked at underlying level; required before any
   option-level performance claim.

## 10b. Setup-level research (2026-08 sprint — `trade_desk_research.py`)

The signal-level validation's flaw was correlation: same-ticker same-day
prints are one bet counted many times. The research module fixes this and
runs the program a reviewer specified: **collapse → purge/embargo → tail
precision → interactions → ablation → cluster bootstrap → registry.**

- **Collapse:** 153,818 raw prints → **4,767 independent ticker-session
  setups (2,999 graded)** — effective n is ~2% of raw. Outcome =
  direction-signed +5-session **close-anchored** excess (`exc_c`, both
  legs at flag-day close — no intraday drift leak). Mixed days
  (0.4 < bull share < 0.6) excluded. Prior-session OI persistence is
  included as a feature because a T−1 print's next-day OI check is known
  by T; same-day OI stays excluded (look-ahead).
- **Purged, embargoed walk-forward:** expanding folds; train rows whose
  5-session label window reaches the validation block are dropped
  (embargo 9 calendar days); 15% chronological holdout untouched.
- **Extreme-tail precision (the product metric), pooled OOS:**

  | selection | avg / med | hit | n | cluster-bootstrap 95% CI |
  |---|---|---|---|---|
  | All | −0.92 / −0.90 | 46% | 1,913 | — |
  | Top 10% | −0.61 / −0.97 | 42% | 175 | −2.4 → +1.1 |
  | Top 5% | +1.74 / +0.10 | 51% | 55 | **−1.2 → +4.5** |
  | Top 1% | +4.66 / +6.44 | 70% | 10 | **−0.8 → +9.4** (9 clusters) |

  Holdout (most recent window): top 5% = **−3.34, CI −5.8 → −0.7** —
  the tail selection actively lost most recently.
- **Interactions:** 884 conjunctions enumerated, 13 passed train gates
  (min-n, positive both halves) — **every one went negative OOS.** The
  "31–90 DTE liquid bullish flow" pocket that led training flipped hard.
- **Ablation:** full-model OOS top-decile −0.61; no single feature-group
  removal turns it positive. Nothing in the current columns creates edge.
- **Verdict:** challenger NOT promoted. Champion = **NONE** → desk
  abstains for the flow family. Promotion criteria (predefined, in the
  JSON): pooled tail avg>0 AND med>0 AND bootstrap CI low>0 AND n≥100
  AND ≥2/3 folds positive AND holdout avg>0.

**Conclusion: the signal inputs need to change.** The hypothesized
institutional conditionals (flow × relative strength × earnings revisions
× IV state × sector) are untestable because the ledger never captured
those columns — so `trade_desk_context.py` now logs them point-in-time
every session (`data/setup_context.jsonl`: RS rank/5d/20d, trend, RSI,
vol ratio, ATR%, EMA position, swing grade, sector, mcap, days-to-
earnings, implied vs realized move, regime) for every ticker with
directional flow plus every name reporting ≤7 days. In ~6–8 weeks the
research re-runs with these columns joined.

**Meanwhile, the first genuine qualification arrived from a different
family:** the earnings loop's `vol_rich` type crossed its pre-defined
gate on its own graded record — **n=102, 91% win rate, EV +6.87
vol-points** (implied minus realized |move|: options systematically
overprice earnings moves — a defined-risk premium-selling edge).
`vol_cheap` also active-positive (n=48, EV +2.49). These are the
Qualified Trades tier at ship; note their EV is in vol-points (edge for
premium sellers), not stock alpha — the card says so.

## 11. What would make this product succeed

The moat is the growing point-in-time record + honest gates, not the LLM.
Success = the day a family's own record flips its gate to `qualified` and
the page can truthfully show a Top Idea with its measured analogue stats —
and the next day report the outcome without having altered the original.
