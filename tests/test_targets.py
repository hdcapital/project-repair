"""Director grouping, scoring and tiering (targets.py)."""
from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import targets as tg  # noqa: E402

TODAY = dt.date(2026, 10, 13)
PTS = {"2170": (-33.92, 150.92), "2168": (-33.94, 150.88), "2500": (-34.42, 150.89), "2830": (-32.25, 148.60)}


def lic(n, licensee, *, op=None, directors="", seg="service", flags="company;service", pc="2170",
        start="2000-01-01T00:00:00", premises=None, dealer=False, brand=""):
    premises = premises if premises is not None else f"{n} Main Rd LIVERPOOL NSW {pc}"
    return {"licence_number": f"MVRL{n}", "licensee": licensee, "operator_key": op or f"acn:{n}",
            "directors": directors, "segment_rule": "dealer" if dealer else seg,
            "name_flags": flags + (";excl_dealer" if dealer else ""), "postcode": pc,
            "region": "Sydney" if pc.startswith("21") else "Illawarra" if pc == "2500" else "Far West / Orana",
            "premises_regions": "", "premises": premises, "n_premises": "1" if premises else "0",
            "start_date": start, "abn": "", "acn": str(n).zfill(9), "business_names": "",
            "business_names_full": "", "public_warnings": "0", "disciplinary_actions": "0",
            "franchise_brand": brand}


class GroupingTests(unittest.TestCase):
    def test_three_part_name_links_across_regions(self):
        rows = [lic(1, "Alpha Auto Pty Ltd", directors="Ryan Paul Sutton"),
                lic(2, "Beta Auto Pty Ltd", directors="RYAN PAUL SUTTON", pc="2500")]
        g = tg.group_operators(rows)
        self.assertEqual(g["acn:1"], g["acn:2"])

    def test_two_part_name_needs_shared_region_or_second_person(self):
        rows = [lic(1, "Alpha Auto Pty Ltd", directors="John Smith"),
                lic(2, "Beta Auto Pty Ltd", directors="John Smith", pc="2830"),      # other region
                lic(3, "Gamma Auto Pty Ltd", directors="John Smith", pc="2168")]     # same region as 1
        g = tg.group_operators(rows)
        self.assertNotEqual(g["acn:1"], g["acn:2"])
        self.assertEqual(g["acn:1"], g["acn:3"])
        rows.append(lic(4, "Delta Auto Pty Ltd", directors="John Smith; Mary Jane Smith", pc="2830"))
        rows[1]["directors"] = "John Smith; Mary Jane Smith"
        g = tg.group_operators(rows)
        self.assertEqual(g["acn:2"], g["acn:4"])                                 # second shared person

    def test_sole_trader_links_to_company_they_direct(self):
        rows = [lic(1, "Adrian Peter Smith", flags="sole_trader;uninformative"),
                lic(2, "Smith Brothers Automotive Pty Ltd", directors="Adrian Peter Smith", pc="2168")]
        g = tg.group_operators(rows)
        self.assertEqual(g["acn:1"], g["acn:2"])


class ScoringTests(unittest.TestCase):
    def build(self, rows, **kw):
        return {t["name"]: t for t in tg.build_targets(rows, today=TODAY, points=PTS, **kw)}

    def test_platform_bolt_on_and_exclusions(self):
        rows = [
            # two-site family group, licensed since the register began
            lic(1, "Tynan Mechanical Pty Ltd", directors="Peter John Tynan; Mary Ann Tynan",
                start="1981-03-31T00:00:00", premises="1 A St LIVERPOOL NSW 2170; 9 B St LIVERPOOL NSW 2170"),
            # single-site company 5 km away -> bolt-on
            lic(2, "Nearby Service Pty Ltd", directors="Sam Lee Wong", pc="2168", start="2015-01-01T00:00:00"),
            # single-site company 300 km away -> tier C
            lic(3, "Far Service Pty Ltd", directors="Kim Ann Lo", pc="2830"),
            # dealer shares a director with a service company -> whole group excluded
            lic(4, "Shiny Toyota Pty Ltd", directors="Paul Howard Warren", dealer=True),
            lic(5, "Warren Service Pty Ltd", directors="Paul Howard Warren",
                premises="5 C St LIVERPOOL NSW 2170; 6 D St LIVERPOOL NSW 2170"),
            # franchise and body shop
            lic(6, "Ultra Tune Liverpool Pty Ltd", brand="Ultra Tune",
                premises="7 E St LIVERPOOL NSW 2170; 8 F St LIVERPOOL NSW 2170"),
            lic(7, "Bash Smash Pty Ltd", seg="body", flags="company;excl_body"),
        ]
        t = self.build(rows)
        a = t["Tynan Mechanical Pty Ltd"]
        self.assertEqual(a["tier"], "A")
        self.assertEqual(a["n_sites"], 2)
        self.assertIn("licensed since register began", a["why"])
        self.assertIn("share surname Tynan", a["family_signal"])
        self.assertEqual(a["succession"], 35)
        self.assertEqual(t["Nearby Service Pty Ltd"]["tier"], "B")
        self.assertEqual(t["Far Service Pty Ltd"]["tier"], "C")
        dealer = next(v for k, v in t.items() if "Warren" in v["entities"])
        self.assertEqual(dealer["tier"], "X")
        self.assertEqual(dealer["n_entities"], 2)
        self.assertIn("dealer", dealer["exclusion"])
        self.assertIn("franchise", t["Ultra Tune Liverpool Pty Ltd"]["exclusion"])
        self.assertIn("mostly body", t["Bash Smash Pty Ltd"]["exclusion"])
        ranks = sorted(t.values(), key=lambda x: x["rank"])
        self.assertEqual(ranks[0]["name"], "Tynan Mechanical Pty Ltd")

    def test_overrides(self):
        rows = [lic(1, "Tynan Mechanical Pty Ltd", premises="1 A St LIVERPOOL NSW 2170; 9 B St LIVERPOOL NSW 2170"),
                lic(7, "Bash Smash Pty Ltd", seg="body", flags="company;excl_body")]
        t = self.build(rows, overrides=[{"match": "MVRL1", "action": "exclude", "note": "dealer, checked"},
                                        {"match": "bash smash pty ltd", "action": "keep", "note": "does service too"}])
        self.assertEqual(t["Tynan Mechanical Pty Ltd"]["tier"], "X")
        self.assertEqual(t["Tynan Mechanical Pty Ltd"]["exclusion"], "override: dealer, checked")
        self.assertNotEqual(t["Bash Smash Pty Ltd"]["tier"], "X")
        self.assertIn("kept by hand", t["Bash Smash Pty Ltd"]["why"])

    def test_display_name_prefers_company(self):
        rows = [lic(1, "Adrian Peter Smith", flags="sole_trader;uninformative"),
                lic(2, "Smith Brothers Automotive Pty Ltd", directors="Adrian Peter Smith", pc="2168")]
        names = [t["name"] for t in tg.build_targets(rows, today=TODAY, points=PTS)]
        self.assertEqual(names, ["Smith Brothers Automotive Pty Ltd"])

    def test_parse_date_and_distance(self):
        self.assertEqual(tg.parse_date("31/03/1981"), dt.date(1981, 3, 31))
        self.assertEqual(tg.parse_date("2006-07-10T00:00:00"), dt.date(2006, 7, 10))
        self.assertIsNone(tg.parse_date(""))
        self.assertLess(tg.km(PTS["2170"], PTS["2168"]), 10)
        self.assertGreater(tg.km(PTS["2170"], PTS["2830"]), 200)


if __name__ == "__main__":
    unittest.main()
