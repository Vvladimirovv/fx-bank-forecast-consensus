"""Offline tests for the pure functions in collect.py. No network, no API keys.

    python -m unittest discover -s tests -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import collect  # noqa: E402

CFG = {
    "instruments": {"EUR/USD": [1.0, 1.4], "USD/JPY": [100, 200], "XAU/USD": [1500, 6000]},
    "horizons": ["3M", "6M", "12M"],
    "settings": {"same_target_pct": 0.5},
}
EVIDENCE = "We see EUR/USD at 1.18 by end of the quarter."


def row(**kw):
    base = {"pair": "EUR/USD", "target": 1.18, "horizon": "3M", "evidence": EVIDENCE}
    base.update(kw)
    return base


class WeightOf(unittest.TestCase):
    allow = {"ing.com": 3, "think.ing.com": 1, "reuters.com": 2}

    def test_most_specific_entry_wins(self):
        self.assertEqual(collect.weight_of("https://think.ing.com/x", self.allow), 1)

    def test_parent_domain_matches_subdomain(self):
        self.assertEqual(collect.weight_of("https://www.ing.com/a", self.allow), 3)

    def test_unknown_domain_is_zero(self):
        self.assertEqual(collect.weight_of("https://example.com", self.allow), 0)

    def test_lookalike_suffix_is_not_a_match(self):
        self.assertEqual(collect.weight_of("https://notreuters.com", self.allow), 0)


class NormPair(unittest.TestCase):
    def test_variants(self):
        for raw in ("eurusd", "EUR-USD", "eur_usd", " EUR / USD "):
            self.assertEqual(collect.norm_pair(raw), "EUR/USD")

    def test_aliases(self):
        self.assertEqual(collect.norm_pair("gold"), "XAU/USD")
        self.assertEqual(collect.norm_pair("crude"), "WTI")

    def test_none_is_empty(self):
        self.assertEqual(collect.norm_pair(None), "")


class Sane(unittest.TestCase):
    def test_accepts_valid_row(self):
        ok, why = collect.sane(row(), CFG)
        self.assertTrue(ok, why)

    def test_inverted_quote_is_fixed(self):
        r = row(pair="USD/EUR", target=0.85)
        ok, _ = collect.sane(r, CFG)
        self.assertTrue(ok)
        self.assertEqual(r["pair"], "EUR/USD")
        self.assertAlmostEqual(r["target"], 1 / 0.85, places=5)

    def test_comma_decimal(self):
        r = row(target="1,18")
        self.assertTrue(collect.sane(r, CFG)[0])
        self.assertEqual(r["target"], 1.18)

    def test_rejects_out_of_band(self):
        self.assertFalse(collect.sane(row(target=11.8), CFG)[0])  # misread decimal

    def test_rejects_untracked_pair(self):
        self.assertFalse(collect.sane(row(pair="EUR/CHF", target=0.95), CFG)[0])

    def test_rejects_unmapped_horizon(self):
        self.assertFalse(collect.sane(row(horizon="2 weeks"), CFG)[0])

    def test_rejects_short_evidence(self):
        self.assertFalse(collect.sane(row(evidence="EUR/USD 1.18"), CFG)[0])

    def test_rejects_non_numeric_target(self):
        self.assertFalse(collect.sane(row(target="n/a"), CFG)[0])


def call(bank, domain, weight, target=1.18, age=1):
    return {"bank": bank, "pair": "EUR/USD", "horizon": "3M", "target": target,
            "weight": weight, "age": age, "domain": domain}


class CrossCheck(unittest.TestCase):
    def test_primary_source(self):
        out = collect.cross_check([call("ING", "ing.com", 3)], CFG)
        self.assertEqual(out[0]["status"], "primary source")

    def test_confirmed_by_two_domains(self):
        out = collect.cross_check([call("ING", "a.com", 1), call("ING", "b.com", 1)], CFG)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["status"], "confirmed by 2 domains")

    def test_single_tier1(self):
        out = collect.cross_check([call("ING", "reuters.com", 2)], CFG)
        self.assertEqual(out[0]["status"], "single tier-1 source")

    def test_weak_single_source_is_quarantined(self):
        out = collect.cross_check([call("ING", "fxempire.com", 1)], CFG)
        self.assertTrue(out[0]["status"].startswith("quarantine"))

    def test_different_targets_stay_separate(self):
        out = collect.cross_check([call("ING", "a.com", 2, 1.18), call("ING", "a.com", 2, 1.25)], CFG)
        self.assertEqual(len(out), 2)


class ExistingKeys(unittest.TestCase):
    def test_missing_workbook_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(collect.existing_keys(Path(d) / "nope.xlsx"), set())


if __name__ == "__main__":
    unittest.main()
