"""momentum_scanner PDF: non-latin-1 text must never crash the run.

Both 2026-10-13 momentum runs died in generate_table_pdf on the em-dash
in the quiet-week Stockbee subtitle ("Approaching — main list empty");
fpdf2's built-in Helvetica is latin-1 only. Placeholder tickers only."""

import unittest

import momentum_scanner as ms


class PdfText(unittest.TestCase):
    def test_safe_maps_typography_to_latin1(self):
        self.assertEqual(ms._safe("a — b – c ‘d’ “e” f…"),
                         'a - b - c \'d\' "e" f...')
        self.assertEqual(ms._safe("x→y"), "xy")       # dropped, not crash

    def test_pdf_survives_em_dash_everywhere(self):
        rows = [{"ticker": "AAA", "_chg": 23.4, "dollar_vol": 1.2e8,
                 "adr_pct": 5.1, "price": 12.34, "_ern": "Oct 20 — est"}]
        for rows_case in ([], rows):
            out = ms.generate_table_pdf(
                "Title — dash",
                "Sub  |  (Approaching — main list empty)",
                ["Blurb — with dash", "plain"], rows_case, "1W")
            self.assertTrue(out.startswith(b"%PDF"), "not a PDF")


if __name__ == "__main__":
    unittest.main()
