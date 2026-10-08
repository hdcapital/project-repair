"""Offline tests for scrape_site.py (everything except the browser)."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import pipeline as pl  # noqa: E402
import scrape_site as ss  # noqa: E402
from test_pipeline import TempData, detail_body, seed_row  # noqa: E402


class TemplateTests(unittest.TestCase):
    def test_template_and_fill_round_trip(self):
        url = "https://verify.licence.nsw.gov.au/api/licence/details?id=1-XT3-1089&type=Motor"
        tpl = ss.template_from(url, "MVRL24145", "1-XT3-1089")
        self.assertEqual(tpl, "https://verify.licence.nsw.gov.au/api/licence/details?id={licence_id}&type=Motor")
        self.assertEqual(ss.fill(tpl, "1-ABC", "MVRL1"),
                         "https://verify.licence.nsw.gov.au/api/licence/details?id=1-ABC&type=Motor")

    def test_template_handles_url_encoding_and_number(self):
        tpl = ss.template_from("https://x/api/search?q=MVRL24145", "MVRL24145", None)
        self.assertEqual(tpl, "https://x/api/search?q={licence_number}")
        tpl = ss.template_from("https://x/api/d/1-XT3%2F1", "MVRL24145", "1-XT3/1")
        self.assertEqual(tpl, "https://x/api/d/{licence_id}")
        self.assertEqual(ss.fill(tpl, "1-XT3/1", "MVRL24145"), "https://x/api/d/1-XT3%2F1")
        self.assertIsNone(ss.fill(None, "a", "b"))

    def test_looks_like_details(self):
        self.assertTrue(ss.looks_like_details('{"licenceDetail":{"licenceNumber":"MVRL1"}}', "MVRL1"))
        self.assertFalse(ss.looks_like_details('{"results":[{"licenceNumber":"MVRL1"}]}', "MVRL1"))
        self.assertFalse(ss.looks_like_details('{"licenceDetail":{"licensee":"x"}}', "MVRL1"))


class ParseBodyTests(unittest.TestCase):
    def test_unwraps_common_envelopes(self):
        inner = detail_body("ID-1")
        self.assertEqual(ss.parse_body(json.dumps(inner), "MVRL1"), inner)
        self.assertEqual(ss.parse_body(json.dumps({"data": inner}), "MVRL1"), inner)
        self.assertEqual(ss.parse_body(json.dumps({"results": [inner]}), "MVRL1"), inner)
        self.assertEqual(ss.parse_body(json.dumps([inner]), "MVRL1"), inner)
        self.assertIsNone(ss.parse_body("<html>blocked</html>", "MVRL1"))
        self.assertIsNone(ss.parse_body(json.dumps({"message": "nothing"}), "MVRL1"))


class FetchHttpTests(TempData):
    CFG = {"url_template": "https://x/api/{licence_id}", "method": "GET", "headers": {}}

    def test_fetch_resumes_and_writes_pipeline_records(self):
        rows = self.write_seed([seed_row(i, f"Shop {i} Pty Ltd") for i in range(5)])
        pl.append_jsonl(self.d / "details.jsonl", {"licence_id": "ID-0", "raw": detail_body("ID-0")})
        calls = []

        def call(cfg, lid, num):
            calls.append(lid)
            return 200, json.dumps({"data": detail_body(lid)})

        res = ss.fetch_http(self.CFG, rate=1000, limit=0, call=call, sleep=lambda s: None)
        self.assertEqual(res, {"ok": 4, "errors": 0})
        self.assertEqual(calls, ["ID-1", "ID-2", "ID-3", "ID-4"])          # ID-0 skipped
        site = pl.read_jsonl(self.d / "details_site.jsonl", "licence_id")
        self.assertEqual(sorted(site), ["ID-1", "ID-2", "ID-3", "ID-4"])    # api file untouched
        recs = pl.load_details(self.d)
        self.assertEqual(len(recs), 5)
        self.assertEqual(recs["ID-3"]["source"], "site")
        self.assertIn("licenceDetail", recs["ID-3"]["raw"])
        # the normal build consumes them unchanged
        pl.cmd_build(None)
        lic = {r["licence_number"]: r for r in pl.read_csv(self.d / "out" / "licences_enriched.csv")}
        self.assertEqual(lic["MVRL3"]["details_fetched"], "True")
        self.assertEqual(lic["MVRL3"]["postcode"], "2170")

    def test_quota_rejected_api_records_are_refetched(self):
        self.write_seed([seed_row(i, f"Shop {i} Pty Ltd") for i in range(2)])
        pl.append_jsonl(self.d / "details.jsonl", {"licence_id": "ID-0", "error": "HTTP 408",
                                                   "raw": {"message": "Quota limit of 2500 per 1 month exceeded."}})
        calls = []
        ss.fetch_http(self.CFG, rate=1000, limit=0,
                      call=lambda c, lid, num: (calls.append(lid), (200, json.dumps(detail_body(lid))))[1],
                      sleep=lambda s: None)
        self.assertEqual(calls, ["ID-0", "ID-1"])

    def test_stops_after_consecutive_failures_and_records_404s(self):
        self.write_seed([seed_row(i, f"Shop {i} Pty Ltd") for i in range(40)])
        calls = []

        def call(cfg, lid, num):
            calls.append(lid)
            return (404, "not found") if lid == "ID-0" else (403, "<html>Access denied</html>")

        res = ss.fetch_http(self.CFG, rate=1000, limit=0, call=call, sleep=lambda s: None)
        self.assertEqual(res["ok"], 0)
        self.assertEqual(len(calls), ss.MAX_CONSECUTIVE_ERRORS)             # stopped, not 40
        recs = pl.read_jsonl(self.d / "details_site.jsonl", "licence_id")
        self.assertEqual(list(recs), ["ID-0"])                               # only the 404 is recorded
        self.assertEqual(recs["ID-0"]["error"], "HTTP 404")

    def test_limit(self):
        self.write_seed([seed_row(i, f"Shop {i} Pty Ltd") for i in range(10)])
        res = ss.fetch_http(self.CFG, rate=1000, limit=3,
                            call=lambda c, lid, num: (200, json.dumps(detail_body(lid))), sleep=lambda s: None)
        self.assertEqual(res["ok"], 3)


if __name__ == "__main__":
    unittest.main()
