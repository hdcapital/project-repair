"""Unit tests for pipeline.py — run with `python -m unittest discover -s tests -v`.

Everything runs offline: the API client takes a stub `http`, `sleep` and `clock`.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import pipeline as pl  # noqa: E402


# --------------------------------------------------------------------------- #
# stubs
# --------------------------------------------------------------------------- #
class Resp:
    def __init__(self, status, body):
        self.status_code = status
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        return json.loads(self.text)


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def detail_body(lid, abn="11222333444", premises=1, classes=("Motor Vehicle Repairer Licence",),
                suburb="LIVERPOOL", conditions=()):
    """Shaped like the real /details response: postal address blank, premises carry a suburb
    but no postcode, businessName null, classes usually just the generic licence type."""
    return {
        "licenceDetail": {"addressType": "Postal", "address": "", "licensee": "X", "licenceeABN": abn,
                          "licenceeACN": (abn[2:] if len(abn) == 11 else f"ACN{abn}"), "startDate": "01/01/2015", "expiryDate": "01/01/2027"},
        "licenceClasses": [{"className": c, "isActive": "True", "description": c} for c in classes],
        "conditions": [{"description": c, "isActive": "True"} for c in conditions],
        "premises": [{"type": "Fixed", "businessName": None, "businessAddress": f"{i} Main Rd {suburb}",
                      "endDate": None} for i in range(1, premises + 1)]
                    + [{"type": "Fixed", "businessName": None, "businessAddress": None, "endDate": None}],
        "businessNames": [{"businessName": "Acme Auto"}],
        "complianceActions": {"publicWarningsCount": 0, "disciplinaryActions": []},
    }


POSTCODE_FIXTURE = """postcode,locality,state,type,sa4name,lgaregion
2170,LIVERPOOL,NSW,Delivery Area,Sydney - South West,Liverpool
2170,LIVERPOOL,NSW,Post Office Boxes,Sydney - South West,Liverpool
2500,WOLLONGONG,NSW,Delivery Area,Illawarra,Wollongong
2765,MARSDEN PARK,NSW,Delivery Area,Sydney - Blacktown,Blacktown
3000,MELBOURNE,VIC,Delivery Area,Melbourne - Inner,Melbourne
"""


class StubHTTP:
    """Scripted responses for the details endpoint; the token endpoint always succeeds."""

    def __init__(self, script=None, default=None):
        self.script = list(script or [])   # list of (status, body) consumed in order for details calls
        self.default = default
        self.calls = []

    def __call__(self, url, params=None, headers=None, timeout=60):
        self.calls.append((url, params))
        if url == pl.TOKEN_URL:
            return Resp(200, {"access_token": "tok"})
        if self.script:
            status, body = self.script.pop(0)
            return Resp(status, body)
        if self.default:
            return Resp(*self.default(params))
        return Resp(200, detail_body(params["licenceid"]))


def make_client(http, clock, rpm=60.0):
    rate = pl.AdaptiveRate(rpm)
    return pl.AdaptiveClient("k", "s", rate, http=http, sleep=clock.sleep, clock=clock, verbose=False)


SEED_FIELDS = pl.SUMMARY_FIELDS


def seed_row(i, licensee, **kw):
    r = {k: "" for k in SEED_FIELDS}
    r.update(licence_number=f"MVRL{i}", licensee=licensee, licence_type="Motor Vehicle Repairers Licence",
             status="Current", licence_id=f"ID-{i}", region="Sydney", postcode="2000")
    r.update(kw)
    return r


class TempData(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        os.environ["DATA_DIR"] = str(self.d)
        (self.d / "postcodes.csv").write_text(POSTCODE_FIXTURE)   # tiny table keeps build fast
        os.environ["POSTCODES_CSV"] = str(self.d / "postcodes.csv")
        pl._GEO = None
        self.addCleanup(self.tmp.cleanup)

    def write_seed(self, rows):
        pl.write_csv(self.d / "register_summary.csv", rows, SEED_FIELDS)
        pl.write_csv(self.d / "queue.csv", pl.prioritise(rows), pl.QUEUE_FIELDS)
        return rows


# --------------------------------------------------------------------------- #
# adaptive rate
# --------------------------------------------------------------------------- #
class AdaptiveRateTests(unittest.TestCase):
    def test_rate_up_after_100_successes(self):
        r = pl.AdaptiveRate(60)
        for _ in range(99):
            r.on_success()
        self.assertEqual(r.rpm, 60)
        r.on_success()
        self.assertAlmostEqual(r.rpm, 72)
        for _ in range(100):
            r.on_success()
        self.assertAlmostEqual(r.rpm, 86.4)

    def test_halve_on_throttle_and_reset_streak(self):
        r = pl.AdaptiveRate(100)
        for _ in range(50):
            r.on_success()
        r.on_throttle()
        self.assertEqual(r.rpm, 50)
        self.assertEqual(r.consecutive_ok, 0)
        for _ in range(100):
            r.on_success()
        self.assertEqual(r.rpm, 60)

    def test_client_applies_rate_through_sleep(self):
        clock = FakeClock()
        http = StubHTTP()
        c = make_client(http, clock, rpm=120)
        t0 = clock.t
        for i in range(5):
            c.details(f"L{i}")
        # 0.5s interval -> ~2s across 5 calls (the token call shares the throttle)
        self.assertGreaterEqual(clock.t - t0, 2.0)
        self.assertEqual(c.calls, 6)  # 1 token + 5 details


# --------------------------------------------------------------------------- #
# 429 handling
# --------------------------------------------------------------------------- #
class ThrottleTests(unittest.TestCase):
    def test_per_minute_throttle_halves_holds_and_recovers(self):
        clock = FakeClock()
        http = StubHTTP(script=[(429, {"fault": {"faultstring": "Spike arrest violation"}}),
                                (200, detail_body("L1"))])
        c = make_client(http, clock, rpm=120)
        t0 = clock.t
        status, body = c.details("L1")
        self.assertEqual(status, 200)
        self.assertEqual(c.rate.rpm, 60)                  # halved
        self.assertGreaterEqual(clock.t - t0, 60)          # held 60s
        self.assertEqual(c.throttles, 1)

    def test_quota_body_is_exhaustion(self):
        clock = FakeClock()
        http = StubHTTP(script=[(429, {"fault": {"faultstring": "Rate limit quota violation. Quota limit exceeded."}})])
        c = make_client(http, clock)
        with self.assertRaises(pl.QuotaExhausted):
            c.details("L1")

    def test_persistent_429_is_exhaustion_after_three_backoffs(self):
        clock = FakeClock()
        http = StubHTTP(script=[(429, "slow down")] * 4)
        c = make_client(http, clock, rpm=60)
        t0 = clock.t
        with self.assertRaises(pl.QuotaExhausted):
            c.details("L1")
        self.assertGreaterEqual(clock.t - t0, 300)         # 60+120+180 = 360s of holds
        self.assertEqual(c.throttles, 4)
        self.assertEqual(c.rate.rpm, 7.5)                  # halved three times

    def test_three_429s_then_success_is_not_exhaustion(self):
        clock = FakeClock()
        http = StubHTTP(script=[(429, "x"), (429, "x"), (429, "x"), (200, detail_body("L1"))])
        c = make_client(http, clock)
        status, _ = c.details("L1")
        self.assertEqual(status, 200)


# --------------------------------------------------------------------------- #
# fetch loop: resume, checkpoint, dedupe, status
# --------------------------------------------------------------------------- #
class FetchLoopTests(TempData):
    def test_resume_after_partial_run(self):
        rows = self.write_seed([seed_row(i, f"Shop {i} Pty Ltd") for i in range(10)])
        clock = FakeClock()
        # first run: deadline after 4 fetches (each call advances the clock 1s at 60 rpm)
        c = make_client(StubHTTP(), clock)
        st = pl.fetch_details(self.d, c, deadline=clock.t + 4.5, clock=clock)
        self.assertFalse(st["complete"])
        done1 = set(pl.read_jsonl(self.d / "details.jsonl", "licence_id"))
        self.assertTrue(0 < len(done1) < 10)
        self.assertEqual(st["remaining"], 10 - len(done1))
        # second run: no deadline -> finishes the rest, never re-fetches
        http2 = StubHTTP()
        c2 = make_client(http2, clock)
        st = pl.fetch_details(self.d, c2, deadline=clock.t + 10_000, clock=clock)
        fetched_ids = {p["licenceid"] for u, p in http2.calls if u == pl.DETAILS_URL}
        self.assertTrue(fetched_ids.isdisjoint(done1))
        self.assertEqual(len(fetched_ids), 10 - len(done1))
        self.assertEqual(st["remaining"], 0)
        self.assertTrue(st["complete"])
        status = json.loads((self.d / "status.json").read_text())
        self.assertTrue(status["complete"])
        self.assertEqual(status["remaining"], 0)
        budget = json.loads((self.d / "budget.json").read_text())
        self.assertEqual(budget["calls_used_this_month"], 10 + 2)  # 10 details + 2 token calls
        self.assertEqual(budget["last_rate"], 60)

    def test_periodic_checkpoint_commit(self):
        self.write_seed([seed_row(i, f"Shop {i} Pty Ltd") for i in range(450)])
        clock = FakeClock()
        commits = []
        c = make_client(StubHTTP(), clock, rpm=600)
        pl.fetch_details(self.d, c, deadline=clock.t + 10_000, clock=clock,
                         checkpoint=commits.append, checkpoint_every=200)
        self.assertEqual(len(commits), 2)
        self.assertIn("200/450", commits[0])
        self.assertIn("400/450", commits[1])

    def test_quota_exhaustion_sets_budget_and_exits_cleanly(self):
        self.write_seed([seed_row(i, f"Shop {i} Pty Ltd") for i in range(5)])
        clock = FakeClock()
        http = StubHTTP(script=[(200, detail_body("ID-0")), (429, "quota exceeded for this month")])
        c = make_client(http, clock)
        st = pl.fetch_details(self.d, c, deadline=clock.t + 10_000, clock=clock)
        self.assertEqual(st["reason"], "quota_exhausted")
        self.assertEqual(st["remaining"], 4)
        budget = json.loads((self.d / "budget.json").read_text())
        until = pl.parse_iso(budget["quota_exhausted_until"])
        self.assertEqual(until, pl.first_of_next_month())
        self.assertEqual(until.day, 1)
        self.assertGreater(until, pl.now_utc())

    def test_exit_fast_when_quota_exhausted_until_in_future(self):
        self.write_seed([seed_row(i, f"Shop {i} Pty Ltd") for i in range(5)])
        future = pl.iso(pl.now_utc() + dt.timedelta(days=3))
        pl.write_json(self.d / "budget.json", {"month": pl.now_utc().strftime("%Y-%m"),
                                               "quota_exhausted_until": future, "last_rate": 90})
        http = StubHTTP()
        clock = FakeClock()
        c = make_client(http, clock)
        st = pl.fetch_details(self.d, c, deadline=clock.t + 10_000, clock=clock)
        self.assertEqual(http.calls, [])                   # API never touched
        self.assertEqual(st["reason"], "quota_exhausted")
        self.assertEqual(st["remaining"], 5)
        self.assertEqual(st["last_rate"], 90)
        # the CLI path too (no credentials needed)
        os.environ.pop("NSW_API_KEY", None)
        self.assertEqual(pl.cmd_fetch_details(None), 0)

    def test_expired_quota_block_is_ignored(self):
        self.write_seed([seed_row(0, "Shop Pty Ltd")])
        past = pl.iso(pl.now_utc() - dt.timedelta(days=1))
        pl.write_json(self.d / "budget.json", {"month": pl.now_utc().strftime("%Y-%m"),
                                               "quota_exhausted_until": past})
        clock = FakeClock()
        st = pl.fetch_details(self.d, make_client(StubHTTP(), clock), deadline=clock.t + 100, clock=clock)
        self.assertTrue(st["complete"])

    def test_monthly_budget_cap(self):
        self.write_seed([seed_row(i, f"Shop {i} Pty Ltd") for i in range(10)])
        clock = FakeClock()
        st = pl.fetch_details(self.d, make_client(StubHTTP(), clock), deadline=clock.t + 10_000,
                              clock=clock, monthly_budget=4)
        self.assertEqual(st["reason"], "monthly_budget")
        budget = json.loads((self.d / "budget.json").read_text())
        self.assertGreaterEqual(budget["calls_used_this_month"], 4)
        self.assertIsNotNone(budget["quota_exhausted_until"])

    def test_4xx_is_logged_and_skipped(self):
        self.write_seed([seed_row(i, f"Shop {i} Pty Ltd") for i in range(3)])
        clock = FakeClock()
        http = StubHTTP(script=[(404, {"message": "not found"})])
        st = pl.fetch_details(self.d, make_client(http, clock), deadline=clock.t + 10_000, clock=clock)
        self.assertTrue(st["complete"])
        self.assertEqual(st["run"]["skipped"], 1)
        recs = pl.read_jsonl(self.d / "details.jsonl", "licence_id")
        self.assertEqual(len(recs), 3)
        self.assertEqual(sum(1 for r in recs.values() if r.get("error")), 1)

    def test_dry_run_makes_no_calls(self):
        self.write_seed([seed_row(0, "Shop Pty Ltd")])
        clock = FakeClock()
        st = pl.fetch_details(self.d, None, deadline=clock.t + 100, clock=clock, dry_run=True)
        self.assertEqual(st["reason"], "dry_run")
        self.assertFalse((self.d / "details.jsonl").exists())


class JsonlTests(TempData):
    def test_dedupe_last_record_wins_and_torn_line_ignored(self):
        p = self.d / "x.jsonl"
        pl.append_jsonl(p, {"licence_id": "A", "v": 1})
        pl.append_jsonl(p, {"licence_id": "B", "v": 1})
        pl.append_jsonl(p, {"licence_id": "A", "v": 2})
        with p.open("a") as f:
            f.write('{"licence_id": "C", "v"')  # killed mid-write
        recs = pl.read_jsonl(p, "licence_id")
        self.assertEqual(set(recs), {"A", "B"})
        self.assertEqual(recs["A"]["v"], 2)


# --------------------------------------------------------------------------- #
# prioritise
# --------------------------------------------------------------------------- #
class PrioritiseTests(unittest.TestCase):
    def test_tiers_and_multi_licence_ordering(self):
        rows = [
            seed_row(1, "Aviana Pty Ltd"),                                   # tier 2
            seed_row(2, "John Smith"),                                       # tier 5
            seed_row(3, "John Smith", business_names="Smith Automotive"),    # tier 3
            seed_row(4, "Westside Mechanical Pty Ltd"),                      # tier 1
            seed_row(5, "Eastside Smash Repairs Pty Ltd"),                   # tier 4 (body)
            seed_row(6, "Big Fleet Pty Ltd"),                                # tier 1, 2 licences
            seed_row(7, "Big Fleet Pty Ltd", business_names="Big Fleet Service Centre"),
            seed_row(8, "Mega Toyota Pty Ltd"),                              # tier 4 (dealer)
        ]
        q = pl.prioritise(rows)
        tiers = {r["licence_number"]: r["tier"] for r in q}
        self.assertEqual(tiers["MVRL4"], 1)
        self.assertEqual(tiers["MVRL1"], 2)
        self.assertEqual(tiers["MVRL3"], 3)
        self.assertEqual(tiers["MVRL5"], 4)
        self.assertEqual(tiers["MVRL8"], 4)
        self.assertEqual(tiers["MVRL2"], 5)
        # Big Fleet (2 licences) ranks first within tier 1... but it hits excl_heavy (FLEET) -> tier 4
        self.assertEqual(tiers["MVRL6"], 4)
        flags = {r["licence_number"]: r["name_flags"] for r in q}
        self.assertIn("excl_body", flags["MVRL5"])
        self.assertIn("excl_dealer", flags["MVRL8"])
        self.assertIn("uninformative", flags["MVRL1"])
        # within tier 4, the 2-licence holder goes first
        t4 = [r["licence_number"] for r in q if r["tier"] == 4]
        self.assertEqual(t4[:2], ["MVRL6", "MVRL7"])
        self.assertEqual([r["rank"] for r in q], list(range(1, 9)))


# --------------------------------------------------------------------------- #
# build: ABN grouping, premises, segments
# --------------------------------------------------------------------------- #
class BuildTests(TempData):
    def test_abn_grouping_and_premises_counting(self):
        rows = self.write_seed([
            seed_row(1, "Alpha Autos Pty Ltd"),
            seed_row(2, "ALPHA AUTOS PTY LTD"),       # same ABN -> one operator
            seed_row(3, "Beta Mechanical Pty Ltd"),   # no details -> grouped by name
            seed_row(4, "Beta Mechanical Pty Ltd"),
            seed_row(5, "Gamma Smash Pty Ltd"),
        ])
        details = {
            "ID-1": {"licence_id": "ID-1", "raw": detail_body("ID-1", abn="111", premises=2)},
            "ID-2": {"licence_id": "ID-2", "raw": detail_body("ID-2", abn="111", premises=1)},  # Shop 1 dup address
            "ID-5": {"licence_id": "ID-5", "raw": detail_body("ID-5", abn="555", premises=1,
                                                              classes=("Panel beater", "Vehicle painter"))},
            "ID-3": {"licence_id": "ID-3", "raw": detail_body("ID-3", abn="333", premises=1, suburb="WOLLONGONG",
                                                              conditions=("Restricted to carrying on a business "
                                                                          "from a mobile workshop",))},
        }
        queue = pl.read_csv(self.d / "queue.csv")
        lic = pl.enrich_licences(rows, queue, details)
        by = {r["licence_number"]: r for r in lic}
        self.assertEqual(by["MVRL1"]["n_premises"], 2)                # the null-address premises is dropped
        self.assertEqual(by["MVRL1"]["abn"], "111")
        self.assertEqual(by["MVRL1"]["acn"], "ACN111")
        self.assertTrue(by["MVRL1"]["details_fetched"])
        self.assertFalse(by["MVRL4"]["details_fetched"])
        self.assertEqual(by["MVRL1"]["segment_rule"], "service")      # generic class -> name flags
        self.assertEqual(by["MVRL5"]["segment_rule"], "body")         # specific classes beat the name
        self.assertEqual(by["MVRL3"]["segment_rule"], "mobile")       # condition beats the name
        self.assertEqual(by["MVRL4"]["segment_rule"], "service")      # name only
        self.assertEqual(by["MVRL1"]["postcode"], "2170")             # suburb -> postcode from premises
        self.assertEqual(by["MVRL1"]["region"], "Sydney")
        self.assertEqual(by["MVRL3"]["region"], "Illawarra")
        self.assertEqual(by["MVRL4"]["region"], "Sydney")             # seed region kept when no details
        self.assertEqual(by["MVRL1"]["licence_classes"], "Motor Vehicle Repairer Licence")
        self.assertEqual(by["MVRL1"]["acn"], "ACN111")
        self.assertEqual(by["MVRL1"]["operator_key"], "acn:ACN111")        # ACN beats ABN
        self.assertEqual(by["MVRL4"]["operator_key"], "acn:ACN333")        # unfetched licence adopts
        ops = pl.build_operators(lic, {})                                   # its namesake's entity id
        byop = {o["operator_key"]: o for o in ops}
        alpha = byop["acn:ACN111"]
        self.assertEqual(alpha["n_licences"], 2)
        self.assertEqual(alpha["n_premises_total"], 2)                # 3 premises, 2 distinct addresses
        self.assertEqual(alpha["abn"], "111")
        self.assertEqual(alpha["segment"], "service")
        beta = byop["acn:ACN333"]
        self.assertEqual(beta["n_licences"], 2)
        self.assertEqual(beta["n_premises_total"], 2)                 # 1 known + 1 unknown site
        self.assertEqual(beta["segment"], "mobile")                   # tie -> first seen (MVRL3)
        self.assertIn("Illawarra", beta["regions"])
        self.assertNotIn("name:BETA MECHANICAL", byop)
        short = pl.build_shortlist(ops)
        blocks = {s["operator_key"]: s["block"] for s in short}
        self.assertEqual(blocks["acn:ACN111"], "multi_site")
        self.assertNotIn("acn:ACN555", blocks)                        # body shop excluded
        self.assertNotIn("acn:ACN333", blocks)                        # mobile excluded

    def test_entity_key(self):
        self.assertEqual(pl.entity_key({"acn": "135710940", "abn": ""}), "acn:135710940")
        self.assertEqual(pl.entity_key({"acn": "", "abn": "53000158725"}), "acn:000158725")
        self.assertEqual(pl.entity_key({"acn": "000158725", "abn": "53000158725"}), "acn:000158725")
        self.assertEqual(pl.entity_key({"acn": "", "abn": "12345"}), "abn:12345")
        self.assertEqual(pl.entity_key({}), "")

    def test_segment_from_classes(self):
        self.assertEqual(pl.segment_from_classes(["Mechanical repairer", "Panel beater"]), "service")
        self.assertEqual(pl.segment_from_classes(["Panel beater", "Vehicle painter"]), "body")
        self.assertEqual(pl.segment_from_classes(["Auto electrician"]), "specialist")
        self.assertEqual(pl.segment_from_classes(["Tyre fitter"]), "specialist")
        self.assertEqual(pl.segment_from_classes([]), "")
        self.assertEqual(pl.segment_from_classes(["Motor Vehicle Repairer Licence"]), "")   # generic
        self.assertEqual(pl.segment_from_classes(["Motor Vehicle Repairer's Licence", "Tyre fitter"]),
                         "specialist")

    def test_postcode_from_premises(self):
        loc = {"LIVERPOOL": "2170", "MARSDEN PARK": "2765", "PARK": "9999", "WOLLONGONG": "2500"}
        self.assertEqual(pl.postcode_from_premises("11 Waltham Street LIVERPOOL", loc), "2170")
        self.assertEqual(pl.postcode_from_premises("14 DARLING ST MARSDEN PARK", loc), "2765")  # longest wins
        self.assertEqual(pl.postcode_from_premises("Cnr Bourke & Flinders Sts WOLLONGONG, NSW", loc), "2500")
        self.assertEqual(pl.postcode_from_premises("1 Smith St LIVERPOOL NSW 2170", loc), "2170")
        self.assertEqual(pl.postcode_from_premises("1 Smith St NOWHERE", loc), "")
        self.assertEqual(pl.postcode_from_premises("", loc), "")

    def test_franchise_and_dealer_excluded_from_shortlist(self):
        rows = self.write_seed([
            seed_row(1, "Ultra Tune Parramatta Pty Ltd"),
            seed_row(2, "Ultra Tune Parramatta Pty Ltd"),
            seed_row(3, "Sunny Toyota Pty Ltd", business_names="Sunny Toyota Service"),
            seed_row(4, "Sunny Toyota Pty Ltd"),
            seed_row(5, "Solo Service Centre Pty Ltd"),
            seed_row(6, "Jane Doe", business_names="Jane's Mechanical"),
        ])
        lic = pl.enrich_licences(rows, pl.read_csv(self.d / "queue.csv"), {})
        ops = pl.build_operators(lic, {})
        short = pl.build_shortlist(ops)
        keys = {s["operator_key"]: s["block"] for s in short}
        self.assertNotIn("name:ULTRA TUNE PARRAMATTA", keys)
        self.assertNotIn("name:SUNNY TOYOTA", keys)
        self.assertEqual(keys["name:SOLO SERVICE CENTRE"], "single_site_company")
        self.assertNotIn("name:JANE DOE", keys)                       # sole trader single site

    def test_build_cli_offline_with_zero_details(self):
        self.write_seed([seed_row(i, f"Shop {i} Pty Ltd") for i in range(3)])
        self.assertEqual(pl.cmd_build(None), 0)
        out = self.d / "out"
        for f in ("licences_enriched.csv", "operators.csv", "shortlist.csv", "summary.md"):
            self.assertTrue((out / f).exists(), f)
        lic = pl.read_csv(out / "licences_enriched.csv")
        self.assertEqual(len(lic), 3)
        self.assertEqual(lic[0]["abn"], "")
        self.assertEqual(lic[0]["details_fetched"], "False")
        self.assertIn("remaining | 3", (out / "summary.md").read_text())

    def test_abr_flatten_and_jsonp(self):
        body = 'cb({"Abn":"11222333444","EntityName":"ACME PTY LTD","EntityTypeName":"Australian Private Company",' \
               '"EntityTypeCode":"PRV","Gst":"2001-07-01","AddressState":"NSW","AddressPostcode":"2170",' \
               '"BusinessName":["Acme Auto"],"Message":""})'
        raw = pl.parse_jsonp(body)
        info = pl.flatten_abr({"abn": "11222333444", "raw": raw})
        self.assertEqual(info["entity_type_code"], "PRV")
        self.assertEqual(info["abr_business_names"], "Acme Auto")

    def test_fetch_abr_skips_known_and_respects_deadline(self):
        pl.append_jsonl(self.d / "details.jsonl", {"licence_id": "A", "raw": detail_body("A", abn="111")})
        pl.append_jsonl(self.d / "details.jsonl", {"licence_id": "B", "raw": detail_body("B", abn="222")})
        pl.append_jsonl(self.d / "details.jsonl", {"licence_id": "C", "raw": detail_body("C", abn="111")})
        pl.append_jsonl(self.d / "abr.jsonl", {"abn": "111", "raw": {}})
        calls = []

        def http(url, params=None, headers=None, timeout=30):
            calls.append(params["abn"])
            return Resp(200, 'cb({"Abn":"%s","EntityName":"X"})' % params["abn"])

        clock = FakeClock()
        n = pl.fetch_abr(self.d, "guid", deadline=clock.t + 100, http=http, sleep=clock.sleep, clock=clock)
        self.assertEqual(calls, ["222"])
        self.assertEqual(n, 1)
        self.assertEqual(set(pl.read_jsonl(self.d / "abr.jsonl", "abn")), {"111", "222"})


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------- #
# Lessons from the first full run: api.nsw signals a spent quota with HTTP 408
# --------------------------------------------------------------------------- #
class QuotaRegressionTests(TempData):
    QUOTA_408 = (408, {"message": "Quota limit of 2500 per 1 month exceeded."})

    def test_408_quota_body_is_exhaustion(self):
        clock = FakeClock()
        c = make_client(StubHTTP(script=[self.QUOTA_408]), clock)
        with self.assertRaises(pl.QuotaExhausted):
            c.details("L1")

    def test_quota_408_mid_run_pauses_and_records_nothing_for_the_rest(self):
        self.write_seed([seed_row(i, f"Shop {i} Pty Ltd") for i in range(50)])
        clock = FakeClock()
        http = StubHTTP(script=[(200, detail_body("ID-0")), (200, detail_body("ID-1"))] + [self.QUOTA_408] * 48)
        st = pl.fetch_details(self.d, make_client(http, clock), deadline=clock.t + 10_000, clock=clock)
        self.assertEqual(st["reason"], "quota_exhausted")
        self.assertEqual(st["remaining"], 48)
        self.assertFalse(st["complete"])
        recs = pl.read_jsonl(self.d / "details.jsonl", "licence_id")
        self.assertEqual(len(recs), 2)                               # nothing recorded as an error
        self.assertEqual(len(http.script), 47)                        # exactly one rejected call made
        budget = json.loads((self.d / "budget.json").read_text())
        self.assertTrue(pl.quota_exhausted_now(budget))

    def test_consecutive_errors_stop_the_run(self):
        self.write_seed([seed_row(i, f"Shop {i} Pty Ltd") for i in range(100)])
        clock = FakeClock()
        http = StubHTTP(script=[(403, {"message": "forbidden"})] * 100)
        st = pl.fetch_details(self.d, make_client(http, clock), deadline=clock.t + 10_000, clock=clock)
        self.assertTrue(st["reason"].startswith("http_403_x"))
        self.assertFalse(st["complete"])
        recs = pl.read_jsonl(self.d / "details.jsonl", "licence_id")
        self.assertEqual(len(recs), pl.MAX_CONSECUTIVE_ERRORS - 1)   # the last one is retried
        self.assertEqual(st["remaining"], 100 - (pl.MAX_CONSECUTIVE_ERRORS - 1))

    def test_quota_rejected_records_are_retried_not_done(self):
        rows = self.write_seed([seed_row(i, f"Shop {i} Pty Ltd") for i in range(3)])
        pl.append_jsonl(self.d / "details.jsonl", {"licence_id": "ID-0", "raw": detail_body("ID-0")})
        pl.append_jsonl(self.d / "details.jsonl", {"licence_id": "ID-1", "error": "HTTP 408",
                                                   "raw": {"message": "Quota limit of 2500 per 1 month exceeded."}})
        pl.append_jsonl(self.d / "details.jsonl", {"licence_id": "ID-2", "error": "HTTP 404", "raw": {}})
        details = pl.read_jsonl(self.d / "details.jsonl", "licence_id")
        self.assertEqual(pl.fetched_ids(details), {"ID-0", "ID-2"})
        clock = FakeClock()
        http = StubHTTP()
        st = pl.fetch_details(self.d, make_client(http, clock), deadline=clock.t + 1000, clock=clock)
        self.assertEqual([p["licenceid"] for u, p in http.calls if u == pl.DETAILS_URL], ["ID-1"])
        self.assertTrue(st["complete"])
        # build treats the quota-rejected record as not fetched
        pl.cmd_build(None)
        lic = {r["licence_number"]: r for r in pl.read_csv(self.d / "out" / "licences_enriched.csv")}
        self.assertEqual(lic["MVRL1"]["details_fetched"], "True")   # refetched above
        self.assertEqual(lic["MVRL2"]["details_fetched"], "False")  # genuine 404 stays unfetched

    def test_reset_quota_clears_the_pause(self):
        self.write_seed([seed_row(0, "Shop Pty Ltd")])
        future = pl.iso(pl.now_utc() + dt.timedelta(days=3))
        pl.write_json(self.d / "budget.json", {"month": pl.now_utc().strftime("%Y-%m"),
                                               "quota_exhausted_until": future, "last_rate": 90})
        os.environ["RESET_QUOTA"] = "1"
        self.addCleanup(os.environ.pop, "RESET_QUOTA", None)
        clock = FakeClock()
        st = pl.fetch_details(self.d, make_client(StubHTTP(), clock), deadline=clock.t + 100, clock=clock)
        self.assertTrue(st["complete"])
        self.assertIsNone(json.loads((self.d / "budget.json").read_text())["quota_exhausted_until"])
