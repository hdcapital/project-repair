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


SEARCH_ROW = {"licenceId": "1-XT3-1089", "licenceNumber": "MVRL24145",
              "licenceType": "Motor Vehicle Repairers Licence", "status": "Current",
              "granted": "1990-08-03", "expires": "2027-08-03", "licensee": "S M A Motors Pty Ltd",
              "licenseeType": "Organisation", "suburb": "ARTARMON", "state": "NSW", "postcode": "2064",
              "address": "43 HOTHAM PDE ARTARMON NSW 2064", "latitude": -33.81, "longitude": 151.18,
              "ABN": "53000158725", "ACN": "000158725"}


class SearchWrapTests(unittest.TestCase):
    def test_search_row_becomes_details_record_the_flattener_understands(self):
        body = json.dumps({"pagingInfo": {"totalRecords": 1}, "results": [SEARCH_ROW]})
        rec = ss.parse_body(body, "MVRL24145", mode="search", licence_id="1-XT3-1089")
        self.assertEqual(rec["_site"], "search")
        flat = pl.flatten({"raw": rec})
        self.assertEqual(flat["abn"], "53000158725")
        self.assertEqual(flat["acn"], "000158725")
        self.assertEqual(flat["details_postcode"], "2064")
        self.assertEqual(flat["n_premises"], 1)
        self.assertEqual(flat["start_date"], "1990-08-03")
        self.assertEqual(flat["details_expiry_date"], "2027-08-03")
        self.assertEqual(pl.entity_key(flat), "acn:000158725")
        # wrong licence in the results -> nothing
        self.assertIsNone(ss.parse_body(body, "MVRL1", mode="search"))
        self.assertIsNone(ss.parse_body(json.dumps({"results": []}), "MVRL24145", mode="search"))

    def test_response_classification(self):
        search = json.dumps({"pagingInfo": {}, "results": [SEARCH_ROW]})
        self.assertTrue(ss.is_search_response(search))
        self.assertFalse(ss.has_detail_markers(search))
        details = json.dumps(detail_body("1-XT3-1089"))
        self.assertFalse(ss.is_search_response(details))
        self.assertTrue(ss.has_detail_markers(details))

    def test_endpoint_config_strips_tracing_headers(self):
        c = {"url": "https://x/api/search", "method": "POST", "post_data": '{"search":"MVRL24145"}',
             "headers": {"accept": "application/json", "newrelic": "abc", "traceparent": "00-x",
                         "x-correlation-id": "gh-1", "user-agent": "ua"}}
        e = ss.endpoint_config(c, "MVRL24145", "1-XT3-1089", "https://x/results", "search")
        self.assertEqual(set(e["headers"]), {"accept", "user-agent"})
        self.assertEqual(e["post_data_template"], '{"search":"{licence_number}"}')
        self.assertEqual(e["mode"], "search")


class FetchHttpTests(TempData):
    CFG = {"url_template": "https://x/api/{licence_id}", "method": "GET", "headers": {}, "mode": "details"}

    def test_fetch_in_search_mode(self):
        self.write_seed([seed_row(i, f"Shop {i} Pty Ltd") for i in range(2)])
        cfg = {"url_template": "https://x/api/search", "method": "POST", "headers": {}, "mode": "search",
               "post_data_template": '{"search":"{licence_number}"}'}

        def call(c, lid, num):
            row = dict(SEARCH_ROW, licenceId=lid, licenceNumber=num)
            return 200, json.dumps({"pagingInfo": {"totalRecords": 1}, "results": [row]})

        res = ss.fetch_http(cfg, rate=1000, limit=0, call=call, sleep=lambda s: None)
        self.assertEqual(res["ok"], 2)
        recs = pl.read_jsonl(self.d / "details_site.jsonl", "licence_id")
        self.assertEqual(recs["ID-1"]["source"], "site-search")
        pl.cmd_build(None)
        lic = {r["licence_number"]: r for r in pl.read_csv(self.d / "out" / "licences_enriched.csv")}
        self.assertEqual(lic["MVRL1"]["abn"], "53000158725")
        self.assertEqual(lic["MVRL1"]["postcode"], "2064")

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
        self.assertEqual(recs["ID-3"]["source"], "site-details")
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
