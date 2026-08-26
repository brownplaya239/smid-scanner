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

## 10c. Earnings Volatility Alpha Engine (2026-08, reviewer-corrected)

**The correction, encoded in the gating ladder:** a validated
implied-vs-realized relationship is a **QUALIFIED SIGNAL**, not a
qualified trade — premium selling is exactly where win rate conceals
negative skew. The ladder is now:

```
Predictive relationship → OOS validated → SIGNAL QUALIFIED
  → executable option reconstruction → transaction-cost positive
  → tail-risk acceptable → TRADE QUALIFIED → paper-forward → CHAMPION
```

`SIGNAL_QUALIFIED` renders in the top tier with an explicit "trade
expression pending executable validation" disclaimer; `TRADE_QUALIFIED`
can only be granted by the backtest report.

**Part 1 — signal anatomy (`earnings_vol_engine.py`, runs every batch,
local data only).** vol_rich n=102 across **18 event nights** (median 6
events/night, max 10 — "n=102" is really n≈18 nights; sizing must treat
a night as the unit): night-cluster bootstrap CI **[+5.87, +7.83]** —
robust, and regime-insensitive (EV 6.4–7.1 in all three regimes).
Move-ratio distribution (|realized|/implied): median **0.23×**, p90
0.88×, p95 1.22×, p99 1.77×, max 1.79×; 83% inside 0.75×, 91% inside
1.0×, blowthrough >1×: 9%, >2×: 0% — this is what parameterizes condor/
fly wings (1.5× implied contained ~97% historically). Signal-level tail
proxy: worst mv −5.53 (ratio 1.77), ES95 −3.69. Decomposition: EV is
monotone in implied size (≥12% implied → 10.87; 8–12% → 7.08; 5–8% →
3.36). vol_cheap is the convexity mirror: ratio median **1.84×**, p95
6.28×, p99 13.4×, CI [+0.55, +4.57] — a long-vol signal. Both types:
`signal_qualified: true`, `trade_qualified: false`.

**Part 2 — option P&L reconstruction (`earnings_vol_backtest.py`,
CI nightly via `earnings_vol.yml`).** For every graded event:
expired-chain reference as-of entry, nearest post-print expiry, legs
priced from daily aggregates, base-case cost model (5% slippage/side/
leg + $0.65/contract), entry = last close before the print, exit =
first close after. Strategies: short straddle (undefined-risk,
diagnostic only — can never qualify anything), iron fly (wings 1.5×
implied), iron condor (shorts 0.75×, wings 1.5×), long straddle for
vol_cheap. Report: n, win, avg/med ROR, PF, worst, p5/ES95/ES99,
**expected log-growth**, loss>1R frequency, night-cluster CI on ROR.
`trade_qualified` requires ALL of: n≥60, avg ROR>0, night-CI low>0,
PF≥1.3, log-growth>0, loss>1R = 0. Smoke test (n=3) already showed the
forecast/monetization gap: an EQT signal "win" lost 73% of risk as an
iron fly after costs. Full 189-event reconstruction runs in CI.

**TickerDesk Fair Move** now renders on earnings cards: Market implied
±X% vs Fair Move ±Y% (ticker's own median |earnings move|, analogue
count shown), Volatility Edge (pp), Richness (%), and stance (SELL VOL
/ BUY VOL). Nothing is a multiplier on a model guess — fair move is the
ticker's measured history.

**Not done yet, deliberately:** cross-strategy R-normalization (waits
for real trade-level P&L per the review), IV-percentile/skew/term-
structure decomposition (no historical IV surface in the record —
`trade_desk_context.py` now logs implied-move context forward), and
portfolio max-simultaneous-exposure sizing (needs the reconstruction's
same-night covariance; the clustering block carries the inputs).

## 10d. v2 reconstruction verdict (2026-08-26, n=179 events)

**Nothing trade-qualified — and the gate is working exactly as
designed.** At the base case (next_close exit, 5%/side/leg slippage):
every predeclared structure negative — flies PF 0.50-0.52, condors
0.52-0.70, cheap straddle/strangle 0.45-0.47, short-straddle
diagnostic 0.64. Grid is uniformly negative (no parameter-instability
mirage), folds mostly negative, exits don't rescue it.

**Wording discipline (reviewer):** what is demonstrated is that the
vol-rich signal *produces positive gross theoretical P&L when marked at
the next-session open, before realistic execution costs* — and that the
gross advantage disappears by the close. "Monetizes" is reserved for
after the NBBO/executable-fill test passes; the entire observed
expectancy (~$69/lot frictionless) sits inside a razor-thin execution
budget.

**The cost attribution isolates WHERE the gross edge lives** (recomputed
from the immutable per-leg marks; attribution only, never
qualification):

| short_straddle @ next_open | PF | avg $/lot |
|---|---|---|
| slippage 0.0% | **1.49** | **+$69** |
| slippage 2.5% | 1.07 | +$13 |
| slippage 5.0% | 0.78 | −$43 |

iron_fly_1.5 @ next_open frictionless: PF 1.41 / +$56. Same
structures at next_CLOSE frictionless: PF ~1.0 / ~$0 — **the crush
edge monetizes at the next-morning open and decays away by the
close.** vol_cheap long premium is negative under every model — the
cheap signal does not monetize as naive long straddles held a day.

**Conclusion:** the earnings-vol anomaly has a real GROSS monetization
window (short vol into the open), and the executable question is now
an execution-quality question: break-even slippage ≈ 2.5-3%/side/leg.
Whether real fills on liquid near-ATM earnings names beat that cannot
be answered from daily bars — it requires NBBO quote reconstruction
(entitlement check in CI) and/or a liquidity-filtered subset. Until
then: vol_rich stays SIGNAL_QUALIFIED, trade_qualified stays false,
and no structure is recommended. v1 rows (with the fabricated-zero
mark flaw) remain in the cache under their own version key —
superseded, never rewritten.

## 10e. V3 — execution-quality study (`earnings_vol_exec.py`)

Structure exploration is FROZEN (V2 finding: timing + friction dominate
structure selection); V3 is microstructure only. Per event, per frozen
structure (fly 1.5x, condors 0.75/1.5 and 0.9/1.5, straddle diagnostic):

- **NBBO snapshots**: entry = last quote 15:45-16:00 ET; exits = last
  quote in 09:30-:31 / :31-:35 / :35-:45 / :45-10:00 (the open-timing
  sweet-spot question). Immutable versioned quote cache.
- **Package-level fills**: mid / mid-25% / mid-50% (canonical EXEC) /
  natural, per side, fees in — a complex-order proxy, not four
  independently-hit legs.
- **Break-even mid capture** per event (required entry credit / package
  mid, given f=0.50 exit) published as a distribution — "median 92%
  capture required" is actionable; "3% slippage" is not.
- **Liquidity conditioning** (predeclared): entry package spread%
  buckets, gross vs executable PF — the causal-candidate table.
- **Gross Edge Capture** = net executable / frictionless P&L.
- **VOL_RICH_EXEC_V1 gate** (now the ONLY trade-qualification
  authority `trade_desk.py` reads): at f=0.25 entry+exit — n≥60,
  avg AND median ROR>0, capital-weighted ROR>0, PF≥1.3, night-CI
  low>0, log-growth>0, 2/3 folds + latest positive, maxDD>−5R,
  loss>1R=0, AND f=0.50 avg ROR>0. Mid-only profitability does not
  qualify. vol_cheap is excluded: frictionless-negative in V2 — the
  predictive signal is retained, the naive trade expression is killed.

**Stat-accounting rule from the 0.9/1.5 condor reconciliation:**
equal-weight avg ROR (+1.8%) vs capital-weighted (−8.7%) diverged
because nearest-strike snapping varies per-event max risk 40×
($42→$1,632 p10/p90); tiny-risk trades dominated the equal-weight
mean. Both now publish labeled, and capital-weighted ROR > 0 is a
permanent qualification bar in every gate.

## 10f. V3 outcome (2026-08-26): entitlement-blocked; minute-mark timing

**The spread/fill study cannot run on the current data plan.** The
hardened probe (12/12 HTTP-level failures across distinct ATM legs)
confirmed the Polygon tier lacks historical NBBO quotes. Outcome A/B
on executability requires the quotes entitlement (Polygon Options
Advanced) — a data-plan purchase decision, recorded as
`quotes_entitlement_unavailable / undetermined, NOT a pass`.
trade_qualified stays false.

**The minute-aggregate fallback (marks, fees only, NO spread model —
timing evidence, never executability evidence) reshaped the picture:**

| gross mark P&L, per lot | 09:31 | 09:35 | 09:45 | 10:00 |
|---|---|---|---|---|
| short_straddle (diag) | +$83 · PF 1.5 | +$84 · PF 1.5 | **+$131 · PF 1.8** | +$118 (n=23) |
| iron_fly_1.5 | −$24 | +$53 | +$4 | thin |
| iron_condor_0.75/1.5 | −$15 | −$30 | −$53 | thin |
| iron_condor_0.9/1.5 | +$2 | −$5 | +$28 | thin |

Three reads, all with the caveat that **every night-cluster CI spans
zero at 22 nights** — suggestive, not established:

1. The gross crush edge **persists through the entire first 15
   minutes** (straddle marks peak at 09:45) — execution would not need
   to race the 09:30 print.
2. The gross edge lives in the **naked straddle**; the defined-risk
   wings cost more than their protection returns at these marks. The
   only structures policy allows users are the ones that don't clear
   even gross marks convincingly — which materially tempers the case
   for the data upgrade.
3. n decay into 10:00 (74 → 23) is mark sparsity (contracts stop
   printing every minute), documented, not survivorship.

**Standing state:** vol_rich = SIGNAL_QUALIFIED only; abstention holds;
every table (signal engine, v2 backtest, minute fallback) accrues
automatically as new earnings nights grade in CI nightly.

## 10g. V3 conclusion (verbatim, per review) and the standing program

> TickerDesk has identified a statistically persistent discrepancy
> between market-implied and subsequently realized earnings moves in
> its vol-rich cohort. Gross minute-level reconstruction indicates the
> discrepancy can produce positive theoretical short-volatility P&L
> shortly after the next session opens. However, currently tested
> defined-risk structures fail to preserve sufficient gross
> expectancy, and executable NBBO-level profitability has not been
> established. Accordingly, vol-rich remains signal-qualified but no
> trade expression is qualified.

**Three-status registry** (`earnings_vol_exec.json.expression_registry`):
Signal Champion `vol_rich SIGNAL_QUALIFIED` · Expression Challengers
`iron_fly_1.5 / condor_0.75_1.5 / condor_0.9_1.5 — REJECTED` (paper-
forward continues automatically, no retuning) · Execution Challenger
`NONE`. **Mechanical NBBO trigger** (Polygon-Advanced purchase gate):
defined-risk gross PF ≥1.3 ✓(1.41) · night-CI low >0 ✗(−1.34) ·
nights ≥30 ✗(20) → **met: false — do not purchase**. The trigger
re-evaluates nightly; the decision resolves from data.

**Wing economics** (n=88, frictionless next_open): straddle +$89 →
fly +$56, wing drag −$33/event, wing debit median 12.9% of straddle
credit. Descriptive split points AGAINST the cheap-wings hypothesis
(fly does better when wings are expensive — wing richness proxies
implied size, where the gross edge is larger); the honest refinement
(wings cheap RELATIVE to implied) is noted, not launched. The
`vol_rich_cheap_wings` forward-only challenger stands as declared and
forward data decides. **Asymmetric defined-risk structures** (skewed
wings matched to gap-direction skew) are recorded as a deferred
hypothesis — untestable without more nights (n≈22 is an overfitting
invitation).

**Fair Move lab** (`fair_move_lab.py`, walk-forward, promotion rule
predeclared: beat v1 MAE by ≥5% in both halves AND not lose on
edge-sign): v1 MAE 3.48pp / edge-sign 77%; cap_shrunk 3.455 / 80% —
better but below the bar → **no promotion, display stays v1**.
Notable: v1 UNDER-forecasts realized by ~2.0pp on the flagged cohort
(bias −1.98) — displayed richness runs slightly hot; the lab tracks
this every run.

**Priority order (standing):** 1) accrue nights — the binding
constraint everywhere; 2) improve Fair Move + attribution; 3) wing-
cost/ATM-edge decomposition; 4) rejected expressions paper-forward
untouched; 5) no Polygon Advanced until the trigger flips; 6) MCP
untouched. The strongest result of the three sprints: a research
process that kills attractive-looking strategies instead of
rationalizing them.

## 11. What would make this product succeed

The moat is the growing point-in-time record + honest gates, not the LLM.
Success = the day a family's own record flips its gate to `qualified` and
the page can truthfully show a Top Idea with its measured analogue stats —
and the next day report the outcome without having altered the original.
