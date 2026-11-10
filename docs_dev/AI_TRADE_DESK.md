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
earnings_ideas.py / setup_outcomes.py / grade_engine.py / regime loop
        │
        ▼
trade_desk_validation.py  walk-forward validation of the Research Rank
        │                 (Alpha Score v1) + measured regime-conflict
        │                 cells → docs/reports/trade_desk_validation.json
trade_desk_research.py    setup-level research; its champion/challenger
        │                 registry is the flow gate's ONLY authority
        │                 → docs/reports/trade_desk_research.json
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
- **Regime** joins on the *prior session's* label from
  `regime_history.json` (`trade_desk_validation.prior_regime`), in
  training AND serving. A day's breadth label is computed after that
  day's close, so joining the flag day's own label to an intraday flag
  was look-ahead — and live candidates, which never have today's label,
  were scored as `regime:unknown` (2026-11 audit). A prior label more
  than 5 calendar days old reads `unknown`.
- **Outcomes** are close-anchored (`exc_c`: stock and SPY legs both from
  the flag-day close). The older `excess` field anchors the stock leg at
  the intraday flag price but SPY at the close; rows that only carry it
  are dropped and counted (`dropped_no_close_anchor`), never mixed in.
- **Trade ideas**: `data/trade_desk_log.jsonl` is an append-only event
  ledger (`issued` / `graded` lines). Issuance freezes the full idea with
  `as_of`, `data_cutoff`, `engine_version`, `model_version`. Nothing is
  ever rewritten (tests: `test_ledger_append_only_and_idempotent`).
  Each line is a *paper position*: earnings ideas get one id per event
  (type, ticker, direction, report date); other families keep a per-day
  id but are not re-issued while the same ticker+direction position is
  inside its 5-session window. Reads apply the same rule, so legacy
  per-day repeats are de-duplicated without rewriting history.

## 3. Research Rank (Alpha Score v1) — what it is and what the data says

**Definition.** Additive expected-excess model on categorical flag-time
features (side, regime, into-earnings, DTE bucket, liquidity, cap bucket,
opening read, signal type, premium band). Per-feature deviations from the
training-window baseline are shrunk `n/(n+200)` and clamped (±2 pp per
feature, ±6 pp total) — the house edge-weights contract. The score maps
expected +5d direction-signed excess vs SPY to its percentile in the
graded historical record (~90k signals by 2026-11), clamped to 0–100:
**90 means "top decile of expected edge", not "90% win probability" —
and it is NOT a rank among today's candidates** (the site's tooltip said
so until the 2026-11 audit; it now prints the measured skill from
`trade_desk_validation.json`).

**What it is used for.** Sorting the Research Watch table and the
calibration bands. No gate reads it: the flow family's gate is the
research registry (§10b). Its `qualification` block is informational.

**Measured skill at the 2026-11 audit** (before the prior-session /
close-anchor retrain): walk-forward IC **−0.18 / 0.00 / 0.00** by fold,
**−0.03** on the holdout; the desk's own forward record had the 80–100
band at **−0.12 pp** (n=459). The retrain fixes the inputs; it is not
expected to create skill. The live numbers are whatever the JSON says.

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

**Consequence, by design:** the flow family holds at WATCH and the UI
abstains from flow Top Ideas. The gate that could change that is the
setup-level research registry (§10b), re-run every batch in CI — it
flips to `qualified` automatically the day a challenger clears its
predefined criteria; no human flips a constant.

## 4. Strategy family gates (all measured, auto-promoting)

| family | source of truth | rule |
|---|---|---|
| flow | `trade_desk_research.json` registry | a promoted champion → qualified (criteria in §10b) |
| earnings directional (post_report_drift, momentum_into_print) | `earnings_ideas.json` by_type (one grade per event) | event-date-clustered 95% CI low > 0.20 pp modeled round trip → qualified; EV − cost ≤ 0 → degraded; else watch |
| earnings vol_rich / vol_cheap | `earnings_vol.json` + `earnings_vol_exec.json` | night-cluster CI → signal_qualified; only the NBBO study grants trade_qualified |
| momentum (swing A-tier) | `data/setup_outcomes_log.json` — the same cohort the candidates come from | same CI-above-cost rule, pick-date clusters, repeat picks inside a 5-session window de-duplicated |

Every stock-level gate labels its target: the earnings and momentum
records are **raw returns, not vs SPY**. A "qualified" directional
family is a **paper edge** — it counts on its own *Directional
Qualified* tile, never as *Trade Qualified* (reserved for an executable
option expression). Without its per-pick log (local runs) the momentum
gate falls back to the published aggregate and can never qualify.

The momentum gate used to read `scan_outcomes.json` (QM/Stockbee scans)
while its candidates came from the swing A-tier — a different
population. Both now come from the swing A-tier.

Degraded families still generate candidates (they freeze into the ledger
and keep grading — a degraded family can rehabilitate itself) and
display in the collapsed Experimental tier (capped per family).

## 5. Hard filters (every rejection carries a reason)

`BAD_LIQUIDITY` (grade D contract) · `LOW_PREMIUM` (<$250k cluster) ·
`STALE_DATA` (signal >90 min old, source JSON >48 h old, regime label >5
days old → unknown) · `REGIME_CONFLICT` · `LOW_ALPHA` (score <70; the
candidate is still frozen as a paper-only `SHADOW` idea for calibration)
· `INSUFFICIENT_DATA` · `STRATEGY_DEGRADED`. Counts publish in
`trade_desk.json.rejections` — candidate-level only. Non-directional
*prints* (sellers / hedge / income) never become candidates and publish
separately as `skipped_prints.NOT_DIRECTIONAL`.

`REGIME_CONFLICT` is derived every run from the graded record
(`trade_desk_validation.json` → `regime_conflict`): a prior-session
regime × direction cell blocks candidates only when its +5d
close-anchored excess, 95% CI clustered by flag date, lies wholly below
the all-flow average (min n 500, min 20 dates). The old rule (bullish
flow in risk_off/mixed, citing EV −3.08/−4.52) could not be reproduced —
by 2026-11 `uoa_edge.by_regime` had risk_on as the *worst* regime.

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

Every candidate that clears its family's filters is frozen (display caps
are for the page only), one paper position at a time (§2). Grading:

- **flow / momentum**: +5 sessions close-to-close from the issue day's
  close (documented conservative fill — no intraday fill assumed),
  direction-signed excess vs SPY via yfinance.
- **earnings directional types**: graded by the earnings loop's own
  grader (`earnings_ideas._grade`), so the desk and the gate share ONE
  definition: post_report_drift = raw signed 5-session return from the
  **reaction close** (session-aware: BMO day / AMC next day);
  momentum_into_print = 3 sessions from the close before the report.
  Drift used to be graded from the issue-day close — before the print —
  so its window contained the earnings reaction itself (the desk showed
  +0.64 on n=370 while the gate showed −0.45 on n=459). Those grades are
  superseded (`gv` < 2) and the events are regraded.
- **neutral vol ideas** are not "tracked" here — the earnings loop
  grades them |move| vs implied (Forecast record).

Published per family, each on its own target (never blended), gated at
n≥30, with a 95% CI clustered by the date the move starts (CR1, the same
estimator as the earnings gate). "Tracked" counts gradeable directional
positions only.

**Research Rank calibration** bands are 90–100 / 80–89 / 70–79 / <70.
The display floor is 70, so the old 40–59 and <40 bands could never fill
and "60–79" was really 70–79. Sub-floor flow candidates are now frozen
as paper-only `SHADOW` ideas (never displayed, never in the directional
record) so the <70 band fills and calibration is testable. Calibration
is the rank's report card: so far it has NOT been monotonic (at the
2026-11 audit the top band averaged −0.12 pp) — if it stays flat, the
rank stays a sort order and the copy says so.

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

1. **History is short** (11 weeks at ship, ~20 by 2026-11; each JSON
   publishes its computed `span_weeks`; regime labels only from
   2026-07-06). Everything above re-runs as the record grows.
2. **Overlapping signals** (same ticker, same day) inflate effective n
   in the signal-level validation; ICs are honest but naive confidence
   intervals would be optimistic. The setup-level research and the idea
   ledger de-duplicate; the ledger's CIs are date-clustered.
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
  JSON): pooled tail avg>0 AND med>0 AND bootstrap CI low above the
  0.20 pp modeled stock round trip AND n≥100 AND ≥2/3 folds positive
  AND holdout avg>0 — all for the **same tail** being promoted. (Until
  2026-11 the fold check always read the top-5% folds, even when
  promoting the top-2% or top-1% tail, and the CI bar was 0, not cost.)
- **2026-11 input corrections** (same as the Research Rank): regime is
  the prior session's label; outcomes are `exc_c` only (no fallback to
  the mixed-anchor `excess`; dropped setups counted).
- **Tail labels:** a "top X%" cut is a percentile of the TRAINING
  predictions, so the share it selects out of sample drifts — at the
  2026-11 audit the holdout "Top 5/10/20%" rows selected only 0.7 /
  1.2 / 2.5% of setups. Every tail now publishes `share_pct` and the
  site prints "took X%" beside it.

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

## 10h. Matched-control flow scorecard (2026-09-08, `uoa_scorecard.py`)

The SPY-benchmark scorecard conflated beta, momentum-universe drift and
earnings-vol premium with flow information. The redesigned card
benchmarks every signal against 10 controls matched at signal date on
sector, cap band, 63d momentum and 20d vol (flow-contaminated names
excluded; assignments frozen), with week x sector cluster-bootstrap
inference. First full run (41,887 matched-graded of ~84k directional
signals; 38,819 unmatchable — reasons to be instrumented):

- **Overall flow alpha is dead.** Legacy +0.49pp vs SPY -> matched
  **-0.09pp**, CI [-0.60, +0.47], **effective n 1,620** (nominal
  41,887; 303 clusters — a 26x deflation). Net of modeled costs
  (CS half-spread + sqrt impact, $10k clip): **-0.46pp**. Simulated
  net book: -9.6pp over 77 days, maxDD -10pp, 69 days underwater.
- **The July "put-buys carry edge" finding was a benchmark artifact:**
  matched put-buys are **-0.66pp** (CI spans zero). Score>=80:
  +0.33pp, CI spans zero. Decile top-minus-bottom 0.40, monotone 4/9.
  Adjusted-score matched IC +5d: **0.005**.
- **Into-earnings +0.85pp survived matching** (n=13,089) — but CI
  [-0.11, +1.82] spans zero at n_eff 691, and v1 historical controls
  are NOT days-to-earnings matched, so event premium is not yet
  removed. Match v2 (forward, 421 assigned so far) adds the earnings
  bucket; that accrual decides this pocket.
- **Crowding confirmed — the one genuinely new edge candidate.**
  Golden-sweep matched EV falls monotonically with conspicuousness
  (premium/ADV): quiet (<0.1%) **+0.68pp, 53% hit, median +0.20,
  n=2,560** -> loud (>2%) **-5.68pp**. The best-looking prints are
  crowded; the quiet qualifying prints carry what edge exists. This
  inverts the intuitive filter and is now the flow family's premier
  forward hypothesis (declare before optimizing — same discipline as
  the vol challengers).
- **Entry-lag ladder** (8,485 signal-rows over 600 sampled ticker-days,
  earnings-weighted): at-signal +0.77 -> +15m +0.62 -> +60m +0.69 ->
  next-open +0.22. Not a 15-minute latency race — the edge survives
  the session and dies overnight. Same-session automation pressure:
  low.
- **Multiple-testing honesty applied to the Hypothesis Lab:** with the
  scorecard's effective-n deflation (0.039) and Benjamini-Hochberg at
  q=0.10 across the OOS-evaluated family, **0 of the 5 published
  survivors pass** — all now carry `bh_pass:false`.

Promotion gates published on the card: positive matched NET EV over
>=8 unseen weeks, cluster-corrected |t|>2 on effective n, monotone OOS
deciles, no worst-regime degradation. Nightly rebuild:
`uoa_scorecard.yml`. Card honesty block lists every modeling
limitation (meta-snapshot sector/mcap, 4-feature historical matches,
close-anchoring, modeled costs).

## 11. What would make this product succeed

The moat is the growing point-in-time record + honest gates, not the LLM.
Success = the day a family's own record flips its gate to `qualified` and
the page can truthfully show a Top Idea with its measured analogue stats —
and the next day report the outcome without having altered the original.
