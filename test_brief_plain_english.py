"""The Daily Brief says what it means, and says it about the right thing.

Each test pins one thing a reader saw on 2026-11-16:

- a Friday close labelled "pre-market 08:45 ET" on Monday morning;
- internal rule codes ("Rules: GRADE_TRANSITION, FLOW_PRESENT") in the email;
- "Earnings in 0d" for a company reporting that morning;
- earnings rows reading "n/a" all the way across;
- a Motley Fool opinion piece as the top market story;
- an Infoblox press release filed as Amazon's own statement;
- a subject that counted changes ("2 watchlist changes") instead of naming them.
"""

import brief_adapters as BA
import brief_compose as BC
import brief_earnings as BE
import brief_news as BN
import brief_render as BR
import brief_text as BX
import test_fixtures as TF

# ── prices ──────────────────────────────────────────────────────────────


def test_a_scan_price_is_labelled_as_the_close_it_is():
    swing = {"TSN": {"price": 58.82, "session_date": "2026-11-13", "grade": "D"}}
    rec = BA.build_changes(["TSN"], swing, {}, {}, {})[0]["price_record"]
    assert rec["text"] == "$58.82 · Nov 13 close"
    assert rec["session_basis"] == "prior_close"
    assert "pre-market" not in rec["text"]


def test_a_dated_close_reads_as_a_day_not_an_iso_date():
    assert BC.price_record(10, BC.BASIS_CLOSE, "2026-07-21")["text"] == "$10.00 · Jul 21 close"
    # a stamp with a clock keeps the clock
    pre = BC.price_record(10, BC.BASIS_PREMARKET, "2026-07-22 08:40 ET")["text"]
    assert pre == "$10.00 · pre-market 08:40 ET"


# ── rule codes ──────────────────────────────────────────────────────────


def test_rule_codes_stay_in_the_model_but_not_in_the_email():
    model = TF.build("risk_on", TF.RISK_ON)
    recs = [r for s in model["sections"] if s["id"] == "watchlist" for r in s["records"]]
    assert any(r.get("reason_codes") for r in recs), "fixture should carry codes"
    txt = BX.render_text(model)
    html = BC.visible_text(BR.render(model, preheader="x"))
    for code in BC.RULES:
        assert code not in txt
        assert code not in html
    assert "Rules:" not in txt and "Rules:" not in html


# ── earnings wording ────────────────────────────────────────────────────


def test_earnings_horizon_in_words():
    assert BC.earnings_phrase(0) == "earnings today"
    assert BC.earnings_phrase(1) == "earnings tomorrow"
    assert BC.earnings_phrase("4") == "earnings in 4 days"
    assert BC.plain_catalyst("Earnings in 0d") == "earnings today"
    assert BC.plain_catalyst("guidance raised") == "guidance raised"


def test_a_watch_list_row_reports_today_in_words():
    r = BC.rank_ticker(
        {
            "ticker": "TSN",
            "grade_delta": -1,
            "grade_from": "C-",
            "grade_to": "D",
            "has_flow": True,
            "catalyst": "Earnings in 0d",
            "earnings_in_days": 0,
            "earnings_confirmed": True,
        }
    )
    blob = " ".join(r["reasons"]) + " " + (r["synthesis_line"] or "")
    assert "0d" not in blob
    assert sum("earnings today" in x for x in r["reasons"]) == 1, r["reasons"]


# ── earnings table ──────────────────────────────────────────────────────


def _earnings(bmo):
    return BE.build(
        "2026-11-16",
        watch_tickers=["BEKE"],
        anticipated={"days": [{"date": "2026-11-16", "bmo": bmo, "amc": []}]},
        edge={"names": [{"t": "XP", "fair_move": 2.9}]},
        iv_fetch=lambda t: {},
        max_per_session=2,
    )


def test_an_earnings_row_with_nothing_measured_gives_up_its_slot():
    m = _earnings(
        [
            {"ticker": "HTHT", "score": 9},  # nothing measured, not watched
            {"ticker": "BEKE", "score": 1},  # nothing measured, but watched
            {"ticker": "XP", "score": 5},  # a typical move on record
        ]
    )
    assert [r["ticker"] for r in m["records"]] == ["BEKE", "XP"]
    assert "HTHT" not in m["note"]
    assert m["counts"]["bmo"] == 3 and m["counts"]["bmo_shown"] == 2


# ── news ────────────────────────────────────────────────────────────────

AS_OF = "2026-11-16T19:00:00Z"


def _art(title, url, publisher, tickers=(), description=""):
    return {
        "title": title,
        "url": url,
        "publisher": publisher,
        "published": "2026-11-16T18:00:00Z",
        "tickers": list(tickers),
        "description": description,
    }


def test_a_stock_measured_against_the_index_is_not_market_news():
    fool = _art(
        "McDonald's Is Underperforming the S&P 500 and Nasdaq-100. "
        "Could January Mark a Turning Point?",
        "https://www.fool.com/investing/2026/11/16/mcd",
        "The Motley Fool",
    )
    real = _art("S&P 500 falls 2% as yields jump", "https://www.reuters.com/m", "Reuters")
    sel = BN.select([fool, real], [], [], as_of=AS_OF)
    assert [r["headline"] for r in sel["market"]] == ["S&P 500 falls 2% as yields jump"]
    assert not BN.is_market_moving({"headline": "XYZ beats the S&P 500 and Nasdaq again"})
    assert BN.is_market_moving({"headline": "Nasdaq 100 slides as chip stocks sink"})


def test_a_commentary_question_is_opinion_not_news():
    q = _art("Is Nvidia Stock a Buy Before Earnings?", "https://www.fool.com/x", "The Motley Fool")
    assert BN.is_opinion_question(q)
    # the same shape from a reporting outlet is judged on its content
    assert not BN.is_opinion_question({**q, "url": "https://www.reuters.com/x"})


def test_another_companys_press_release_is_not_your_news():
    infoblox = _art(
        "Infoblox Launches Australia Realm on AWS, Giving Regulated Organizations a Local Option",
        "https://www.globenewswire.com/news-release/2026/11/16/infoblox",
        "GlobeNewswire Inc.",
        tickers=["AMZN"],
        description="Infoblox announced the launch of its Australia realm on AWS.",
    )
    own = _art(
        "Amazon Announces New Data Center Investment in Ohio",
        "https://www.businesswire.com/news/amzn",
        "Business Wire",
        tickers=["AMZN"],
    )
    by_ticker = _art(
        "Q3 Results Released",
        "https://www.globenewswire.com/q3",
        "GlobeNewswire Inc.",
        tickers=["AMZN"],
        description="SEATTLE (NASDAQ: AMZN) today reported results.",
    )
    sel = BN.select(
        [], [infoblox, own, by_ticker], ["AMZN"], as_of=AS_OF, names={"AMZN": "Amazon.Com Inc"}
    )
    heads = [r["headline"] for r in sel["watchlist"]]
    assert not any("Infoblox" in h for h in heads)
    assert any(h.startswith("Amazon Announces") for h in heads)
    assert "Q3 Results Released" in heads
    # with no company name to test against, the feed's tag stands
    sel = BN.select([], [infoblox], ["AMZN"], as_of=AS_OF)
    assert sel["watchlist"]


def test_a_story_about_another_company_is_not_your_news():
    names = {"NVDA": "Nvidia Corp", "AMZN": "Amazon.Com Inc"}
    fool = "https://www.fool.com/investing/2026/11/16/%s"
    navitas = _art(
        "Why Navitas Semiconductor Stock Sank by 12% on Monday",
        fool % "navitas",
        "The Motley Fool",
        tickers=["NVDA"],
        description="Navitas dropped on sentiment surrounding Nvidia's CEO.",
    )
    listicle = _art(
        "If I Could Buy and Hold Only 1 AI Hyperscaler Through 2030, It Would Be This One",
        fool % "hyperscaler",
        "The Motley Fool",
        tickers=["AMZN"],
        description="The article argues that Amazon is the best hyperscaler.",
    )
    about = _art(
        "Tech Leaders Want Jensen Huang to Return to Nvidia",
        "https://www.reuters.com/nvda",
        "Reuters",
        tickers=["NVDA"],
    )
    sel = BN.select([], [navitas, listicle, about], ["NVDA", "AMZN"], as_of=AS_OF, names=names)
    assert [r["headline"] for r in sel["watchlist"]] == [
        "Tech Leaders Want Jensen Huang to Return to Nvidia"
    ]


def test_brand_names_count_as_the_company():
    art = {"title": "Google unveils a new Gemini model"}
    assert BN.names_the_company(art, "GOOGL", "Alphabet Inc. Class A Common Stock")


def test_a_summary_is_not_cut_at_an_abbreviation():
    assert (
        BN.first_sentence("Huang was pushed out as Nvidia CEO on Nov. 13. Shares fell.")
        == "Huang was pushed out as Nvidia CEO on Nov. 13"
    )
    assert BN.first_sentence("A person U.S. officials named. More.") == (
        "A person U.S. officials named"
    )
    assert BN.first_sentence("No full stop here") == "No full stop here"


def test_short_names():
    assert BN.short_name("Amazon.Com Inc") == "amazon"
    assert BN.short_name("Tyson Foods, Inc.") == "tyson"
    assert BN.short_name("KE Holdings Inc. American Depositary Shares") == "ke holdings"
    assert BN.short_name("e.l.f. Beauty, Inc.") == "e.l.f"
    assert BN.short_name("") == ""


# ── subject ─────────────────────────────────────────────────────────────


def _wl():
    return BC.rank_watchlist(
        [
            {"ticker": "TSN", "grade_delta": -1, "grade_from": "C-", "grade_to": "D"},
            {"ticker": "CROX", "price_change_pct": 4.4},
            {"ticker": "NVDA"},
        ]
    )


def test_the_subject_names_what_changed():
    wl = _wl()
    subj = BC.build_subject({"regime": {"label": "TRANSITION"}}, wl, sections=("watchlist", "flow"))
    assert subj == "Tape in transition · TSN grade C- → D, CROX +4.4% move"
    assert len(subj) <= BC.SUBJECT_MAX
    assert BC.check_subject_supported(subj, wl["alert_line"], ("watchlist",)) == []


def test_the_subject_cannot_name_a_change_the_body_does_not_show():
    subj = "Tape in transition · TSN grade C- → D"
    assert BC.check_subject_supported(subj, "2 of your 16 watch list names changed", ())
