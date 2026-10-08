"""Multi-account rotation: drain one api.nsw key, switch to the next on the quota body."""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import pipeline as pl  # noqa: E402
from test_pipeline import FakeClock, Resp, TempData, detail_body, seed_row  # noqa: E402

QUOTA = (408, {"message": "Quota limit of 2500 per 1 month exceeded."})


class KeyedHTTP:
    """Each key gets a budget of successful details calls; beyond it, the 408 quota body.
    Keys listed in `bad_auth` fail the token call."""

    def __init__(self, budgets: dict[str, int], bad_auth=()):
        self.budgets = dict(budgets)
        self.bad_auth = set(bad_auth)
        self.calls: list[tuple[str, str, str]] = []   # (key, url, licenceid)

    def __call__(self, url, params=None, headers=None, timeout=60):
        if url == pl.TOKEN_URL:
            import base64
            key = base64.b64decode(headers["Authorization"].split()[1]).decode().split(":")[0]
            self.calls.append((key, "token", ""))
            if key in self.bad_auth:
                return Resp(401, {"error": "invalid_client"})
            return Resp(200, {"access_token": f"tok-{key}"})
        key = headers["apikey"]
        self.calls.append((key, "details", params["licenceid"]))
        if self.budgets.get(key, 0) <= 0:
            return Resp(*QUOTA)
        self.budgets[key] -= 1
        return Resp(200, detail_body(params["licenceid"]))


def client(http, clock, creds, key_state=None, rpm=600):
    return pl.AdaptiveClient(creds, pl.AdaptiveRate(rpm), key_state=key_state,
                             http=http, sleep=clock.sleep, clock=clock, verbose=False)


class ParseTests(unittest.TestCase):
    def test_parse_credentials_formats(self):
        env = {"NSW_API_KEY": "k0", "NSW_API_SECRET": "s0",
               "NSW_API_KEYS": "k1:s1\n k2 , s2 \n# comment\nk3 s3;k4:s4\n\nk0:dup\n"}
        self.assertEqual(pl.parse_credentials(env),
                         [("k0", "s0"), ("k1", "s1"), ("k2", "s2"), ("k3", "s3"), ("k4", "s4")])
        self.assertEqual(pl.parse_credentials({"NSW_API_KEYS": "k1:s1"}), [("k1", "s1")])
        self.assertEqual(pl.parse_credentials({}), [])
        self.assertEqual(pl.parse_credentials({"NSW_API_KEY": "k", "NSW_API_SECRET": ""}), [])

    def test_key_id_is_short_and_non_secret(self):
        self.assertEqual(pl.key_id("abcdefghijklmnop"), "abcd…mnop")
        self.assertEqual(pl.key_id("short"), "short")


class RotationTests(TempData):
    def test_rotates_on_quota_and_retries_same_licence(self):
        self.write_seed([seed_row(i, f"Shop {i} Pty Ltd") for i in range(7)])
        http = KeyedHTTP({"A": 3, "B": 3, "C": 10})
        clock = FakeClock()
        c = client(http, clock, [("A", "a"), ("B", "b"), ("C", "c")])
        st = pl.fetch_details(self.d, c, deadline=clock.t + 10_000, clock=clock)
        self.assertTrue(st["complete"])
        self.assertEqual(st["run"]["fetched"], 7)
        self.assertEqual(c.rotations, 2)
        details = [(k, lid) for k, kind, lid in http.calls if kind == "details"]
        # A: 3 ok + 1 quota reject (ID-3) -> B retries ID-3, 3 ok (ID-3..ID-5) + reject ID-6 -> C fetches ID-6
        self.assertEqual([k for k, _ in details], ["A"] * 4 + ["B"] * 4 + ["C"])
        self.assertEqual(details[3][1], details[4][1])                # same licence retried on B
        self.assertEqual(details[7][1], details[8][1])                # same licence retried on C
        recs = pl.read_jsonl(self.d / "details.jsonl", "licence_id")
        self.assertEqual(len(recs), 7)
        self.assertFalse(any(r.get("error") for r in recs.values()))   # nothing recorded as an error
        budget = json.loads((self.d / "budget.json").read_text())
        keys = budget["keys"]
        self.assertTrue(pl.quota_exhausted_now(keys[pl.key_id("A")]))
        self.assertTrue(pl.quota_exhausted_now(keys[pl.key_id("B")]))
        self.assertFalse(pl.quota_exhausted_now(keys[pl.key_id("C")]))
        self.assertEqual(keys[pl.key_id("C")]["calls"], 2)             # token + 1 details
        self.assertIsNone(budget["quota_exhausted_until"])             # a key is still live

    def test_all_keys_spent_pauses_month_and_next_run_makes_no_calls(self):
        self.write_seed([seed_row(i, f"Shop {i} Pty Ltd") for i in range(10)])
        http = KeyedHTTP({"A": 2, "B": 2})
        clock = FakeClock()
        creds = [("A", "a"), ("B", "b")]
        st = pl.fetch_details(self.d, client(http, clock, creds), deadline=clock.t + 10_000, clock=clock)
        self.assertEqual(st["reason"], "quota_exhausted")
        self.assertEqual(st["remaining"], 6)
        budget = json.loads((self.d / "budget.json").read_text())
        self.assertEqual(pl.parse_iso(budget["quota_exhausted_until"]), pl.first_of_next_month())
        self.assertTrue(pl.keys_exhausted(budget, creds))
        # next run, same keys: fast exit, zero HTTP calls
        http2 = KeyedHTTP({"A": 99, "B": 99})
        st2 = pl.fetch_details(self.d, client(http2, clock, creds), deadline=clock.t + 10_000, clock=clock)
        self.assertEqual(st2["reason"], "quota_exhausted")
        self.assertEqual(http2.calls, [])

    def test_adding_a_key_resumes_despite_global_pause(self):
        self.write_seed([seed_row(i, f"Shop {i} Pty Ltd") for i in range(4)])
        http = KeyedHTTP({"A": 1})
        clock = FakeClock()
        pl.fetch_details(self.d, client(http, clock, [("A", "a")]), deadline=clock.t + 10_000, clock=clock)
        self.assertTrue(pl.quota_exhausted_now(json.loads((self.d / "budget.json").read_text())))
        http2 = KeyedHTTP({"A": 0, "NEW": 10})
        st = pl.fetch_details(self.d, client(http2, clock, [("A", "a"), ("NEW", "n")]),
                              deadline=clock.t + 10_000, clock=clock)
        self.assertTrue(st["complete"])
        self.assertEqual([k for k, kind, _ in http2.calls if kind == "details"], ["NEW"] * 3)  # A skipped
        budget = json.loads((self.d / "budget.json").read_text())
        self.assertIsNone(budget["quota_exhausted_until"])

    def test_legacy_single_key_pause_still_exits_fast_but_multi_key_does_not(self):
        future = pl.iso(pl.now_utc() + dt.timedelta(days=3))
        budget = {"quota_exhausted_until": future}                     # no per-key info (pre-rotation state)
        self.assertTrue(pl.keys_exhausted(budget, [("A", "a")]))
        self.assertTrue(pl.keys_exhausted(budget, []))
        self.assertFalse(pl.keys_exhausted(budget, [("A", "a"), ("B", "b")]))

    def test_auth_failure_skips_key(self):
        self.write_seed([seed_row(i, f"Shop {i} Pty Ltd") for i in range(2)])
        http = KeyedHTTP({"BAD": 9, "GOOD": 9}, bad_auth={"BAD"})
        clock = FakeClock()
        c = client(http, clock, [("BAD", "x"), ("GOOD", "g")])
        st = pl.fetch_details(self.d, c, deadline=clock.t + 10_000, clock=clock)
        self.assertTrue(st["complete"])
        self.assertEqual([k for k, kind, _ in http.calls if kind == "details"], ["GOOD", "GOOD"])
        budget = json.loads((self.d / "budget.json").read_text())
        self.assertIn("auth_failed_at", budget["keys"][pl.key_id("BAD")])

    def test_all_keys_broken_is_an_error_not_a_pause(self):
        self.write_seed([seed_row(0, "Shop Pty Ltd")])
        http = KeyedHTTP({"X": 9, "Y": 9}, bad_auth={"X", "Y"})
        clock = FakeClock()
        with self.assertRaises(RuntimeError):
            pl.fetch_details(self.d, client(http, clock, [("X", "x"), ("Y", "y")]),
                             deadline=clock.t + 10_000, clock=clock)

    def test_cli_uses_env_keys_and_exits_fast_when_all_spent(self):
        self.write_seed([seed_row(0, "Shop Pty Ltd")])
        past_month_end = pl.iso(pl.first_of_next_month())
        pl.write_json(self.d / "budget.json", {
            "month": pl.now_utc().strftime("%Y-%m"), "quota_exhausted_until": past_month_end,
            "keys": {pl.key_id("A"): {"quota_exhausted_until": past_month_end},
                     pl.key_id("B"): {"quota_exhausted_until": past_month_end}}})
        os.environ["NSW_API_KEYS"] = "A:a;B:b"
        self.addCleanup(os.environ.pop, "NSW_API_KEYS", None)
        for k in ("NSW_API_KEY", "NSW_API_SECRET"):
            os.environ.pop(k, None)
        self.assertEqual(pl.cmd_fetch_details(None), 0)
        self.assertEqual(json.loads((self.d / "status.json").read_text())["reason"], "quota_exhausted")


    def test_cli_with_legacy_pause_and_extra_keys_fetches(self):
        """Regression: budget.json from the single-key era (global pause, no per-key info) must not
        block a run that has more keys configured than the pause knows about."""
        self.write_seed([seed_row(i, f"Shop {i} Pty Ltd") for i in range(3)])
        pl.write_json(self.d / "budget.json", {"month": pl.now_utc().strftime("%Y-%m"),
                                               "quota_exhausted_until": pl.iso(pl.first_of_next_month()),
                                               "last_rate": 300})
        os.environ["NSW_API_KEY"], os.environ["NSW_API_SECRET"] = "OLD", "o"
        os.environ["NSW_API_KEYS"] = "NEW1:n1\nNEW2:n2"
        for k in ("NSW_API_KEY", "NSW_API_SECRET", "NSW_API_KEYS"):
            self.addCleanup(os.environ.pop, k, None)
        http = KeyedHTTP({"OLD": 0, "NEW1": 2, "NEW2": 9})
        clock = FakeClock()
        real = pl.AdaptiveClient.__init__

        def patched(self_, creds, rate, *a, **kw):   # inject the stub transport into the CLI path
            kw.update(http=http, sleep=clock.sleep, clock=clock, verbose=False)
            real(self_, creds, rate, *a, **kw)

        pl.AdaptiveClient.__init__ = patched
        self.addCleanup(setattr, pl.AdaptiveClient, "__init__", real)
        self.assertEqual(pl.cmd_fetch_details(None), 0)
        st = json.loads((self.d / "status.json").read_text())
        self.assertTrue(st["complete"])
        keys_used = [k for k, kind, _ in http.calls if kind == "details"]
        self.assertEqual(keys_used, ["OLD", "NEW1", "NEW1", "NEW1", "NEW2"])   # 1 rejected probe on OLD
        budget = json.loads((self.d / "budget.json").read_text())
        self.assertTrue(pl.quota_exhausted_now(budget["keys"][pl.key_id("OLD")]))
        self.assertIsNone(budget["quota_exhausted_until"])


if __name__ == "__main__":
    unittest.main()
