# Equity Research v5 — Institutional Layer Specification
(Tiger diligence + Sundheim decision layer. Received 2026-07-28.
This file is the canonical build contract; phases track in the task list.)

## Phase map (build order)
- **A. Capability-based routing** — CompanyProfile + EvidenceCapability;
  router decides from capabilities, never ticker identity. NO_TICKER_SPECIFIC_BRANCH.
- **B. Claim contract v2** — full argument object (claim_id..status), publication
  gate (2+ admitted refs, mechanism, counterevidence-or-absence, financial
  implication, invalidation, freshness, sourced expectations before "variant").
  Statuses: SUPPORTED / PARTIALLY_SUPPORTED / CONFLICTED / STALE / NOT_ESTABLISHED.
- **C. Expectations engine** — ONE canonical Expectations object per KPI
  (guidance / consensus+as_of / tickerdesk_estimate / valuation_implied /
  historical baseline / differences). Variant requires sourced expectations;
  otherwise "business insight". Matrix render.
- **D. Dual assessment** — Business Quality (EXCEPTIONAL..NOT_ESTABLISHED) vs
  Investment Attractiveness (HIGH..NOT_UNDERWRITTEN); never averaged; tension
  displayed.
- **E. Probability-weighted scenarios** — probabilities (ANALYST_JUDGMENT, sum
  100%), EV arithmetic, up/down ratio, annualized return; never when
  NOT_UNDERWRITTEN.
- **F. Research memory** — versioned ResearchState + deterministic ChangeSet,
  "What changed since prior report", prior-hash verification.
- **G. Tiger diligence matrix** — internal question ledger (question_id..
  follow-up); conclusions in core; unanswered material -> Known Unknowns.
- **H. Value-of-information ledger** — prioritized unresolved questions, top 3
  in core, ranked by rating/scenario impact.
- **I. Metric ontology + sector adapters** — normalized metric registry with
  applicability; versioned adapters (software/banks/insurers/REITs/consumer/
  marketplaces/industrials/energy/biotech/generic) over shared contracts.
- **J. Valuation method routing** — method selected from characteristics;
  selected AND rejected methods explained.
- **K. Peer selection engine** — documented criteria + exclusion reasons; no
  static lists.
- **L. Page architecture v2** — Decision Sheet (IC summary), Argument, Business
  Quality & Diligence, Financial Dashboard (+SBC, balance sheet, guidance
  history), Expectations/Valuation/Scenarios, Timeline/Risk/Context.
  NEW_LISTING: "not yet underwritten" structure.
- **M. Validation extensions** — ~25 checks w/ proven mutations (see spec body
  below) incl. NO_TICKER_SPECIFIC_BRANCH source scan, schema versions +
  source-ledger hash in validation JSON.
- **N. Testing** — synthetic fixtures per archetype/adapter/failure state;
  pilot regression fixtures (NOW/SG/HOOD/SPCX as fixtures ONLY); unseen-ticker
  generalization per adapter.
- **O. Portfolio-context boundary** — no sizing language without an admitted
  PortfolioContext object; standard scope disclaimer otherwise.

## Hard rules
- Pilot tickers must never appear in shared logic (enforced by scan).
- Absence of evidence changes report shape; "not established" is a valid output.
- Assumptions files: universal versioned schema; graceful degradation.
- v4 production path untouched until human-approved cutover.
- A mutation passes only when it fails for the intended check and reason.

## Full original specification
(verbatim, for reference)
