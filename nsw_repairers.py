#!/usr/bin/env python3
"""
nsw_repairers.py — pull every licensed motor vehicle repairer in NSW from the
Fair Trading public register (via the official api.nsw "Motor API"), tagged by
postcode -> SA4 statistical area -> region (Sydney, Newcastle, Hunter, Mid North
Coast, etc.).

Steps
  1. Download the Australian postcode table (free CSV maintained by Matthew
     Proctor, derived from ABS/AusPost) and keep NSW delivery postcodes with
     their SA4 names.  Each SA4 is mapped to a coarse region.
  2. Authenticate to api.nsw (OAuth client-credentials).
  3. Sweep the whole register by licence-number prefix: the browse search is a
     prefix match on licence number and caps results at 200, so 'MVRL5' is
     split into MVRL50..MVRL59 (and so on) until every search is under the cap.
     ~750 calls covers every repairer licence in the state.
  4. Keep current Motor Vehicle Repairers Licences, tag each by its postcode's
     region, write CSV + a per-region summary.
  5. (Optional, --details) fetch /motor/v1/details for each licence to get ABN,
     ACN, full address, licence classes, premises and business names.

Usage
  Put your api.nsw credentials in a file called .env next to this script:
      NSW_API_KEY=xxxxxxxx
      NSW_API_SECRET=yyyyyyyy
      NSW_API_RPM=60
  python nsw_repairers.py                            # full run -> out/
  python nsw_repairers.py --regions Newcastle Hunter "Mid North Coast"
  python nsw_repairers.py --details --details-limit 500
  python nsw_repairers.py --list-regions             # show the region -> SA4 map

Every search is cached in out/cache/, so a killed run resumes where it left
off and --regions / --details re-runs cost nothing.  Delete the cache to refresh.

No third-party packages required (standard library only).
"""
from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import urllib.request
import urllib.parse
import urllib.error

# --------------------------------------------------------------------------- #
# .env + tiny HTTP helper (stdlib only)
# --------------------------------------------------------------------------- #

def load_dotenv(path: Path) -> None:
    """Read KEY=VALUE lines from .env into os.environ (existing env wins)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


class HttpResponse:
    def __init__(self, status: int, body: bytes):
        self.status_code = status
        self.content = body
        self.text = body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.text or "null")


def http_get(url: str, params: dict | None = None, headers: dict | None = None,
             timeout: int = 60) -> HttpResponse:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "nsw-repairers/1.0", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return HttpResponse(resp.status, resp.read())
    except urllib.error.HTTPError as e:
        return HttpResponse(e.code, e.read())


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
API_HOST = "https://api.onegov.nsw.gov.au"
TOKEN_URL = f"{API_HOST}/oauth/client_credential/accesstoken"
BROWSE_URL = f"{API_HOST}/motor/v1/browse"
VERIFY_URL = f"{API_HOST}/motor/v1/verify"
DETAILS_URL = f"{API_HOST}/motor/v1/details"

POSTCODE_CSV_URLS = [
    "https://raw.githubusercontent.com/matthewproctor/australianpostcodes/master/australian_postcodes.csv",
    "https://www.matthewproctor.com/Content/postcodes/australian_postcodes.csv",
]

# Sandbox credentials shown on https://api.nsw.gov.au/Product/Index/15 are
# limited to 5 calls/minute.  Register (free) for 2,500 calls/month and set
# NSW_API_KEY / NSW_API_SECRET instead.
DEFAULT_RPM = 5  # requests per minute if nothing else is specified

# SA4 name -> coarse region.  Matched case-insensitively on prefix, so
# "Sydney - Inner West", "Sydney - Blacktown" etc. all fold into "Sydney".
SA4_TO_REGION = {
    "Sydney": "Sydney",
    "Central Coast": "Central Coast",
    "Newcastle and Lake Macquarie": "Newcastle",
    "Hunter Valley exc Newcastle": "Hunter",
    "Mid North Coast": "Mid North Coast",
    "Coffs Harbour - Grafton": "Coffs Harbour - Grafton",
    "Richmond - Tweed": "Northern Rivers",
    "New England and North West": "New England / North West",
    "Far West and Orana": "Far West / Orana",
    "Central West": "Central West",
    "Illawarra": "Illawarra",
    "Southern Highlands and Shoalhaven": "Southern Highlands / Shoalhaven",
    "Capital Region": "Capital Region",
    "Riverina": "Riverina",
    "Murray": "Murray",
}

# Fallback for postcodes with no SA4 in the table (PO boxes, new estates, etc.)
# Inclusive ranges, checked in order.  Rough but good enough as a backstop.
POSTCODE_RANGE_FALLBACK = [
    ((1000, 1999), "Sydney"),          # PO boxes / large-volume receivers
    ((2000, 2249), "Sydney"),
    ((2250, 2263), "Central Coast"),
    ((2264, 2310), "Newcastle"),
    ((2311, 2339), "Hunter"),
    ((2340, 2411), "New England / North West"),
    ((2415, 2430), "Mid North Coast"),
    ((2431, 2449), "Mid North Coast"),
    ((2450, 2469), "Coffs Harbour - Grafton"),
    ((2470, 2490), "Northern Rivers"),
    ((2500, 2530), "Illawarra"),
    ((2533, 2541), "Southern Highlands / Shoalhaven"),
    ((2545, 2551), "Capital Region"),
    ((2555, 2574), "Sydney"),
    ((2575, 2580), "Southern Highlands / Shoalhaven"),
    ((2581, 2599), "Capital Region"),
    ((2619, 2633), "Capital Region"),
    ((2640, 2660), "Murray"),
    ((2661, 2669), "Riverina"),
    ((2700, 2714), "Riverina"),
    ((2715, 2739), "Murray"),
    ((2745, 2786), "Sydney"),
    ((2787, 2820), "Central West"),
    ((2821, 2879), "Far West / Orana"),
    ((2880, 2898), "Far West / Orana"),
]

REPAIRER_TYPE_RE = re.compile(r"repairer", re.I)

# --------------------------------------------------------------------------- #
# Postcodes
# --------------------------------------------------------------------------- #

def load_postcode_table(local_path: str | None, out_dir: Path) -> list[dict]:
    """Return rows of the Australian postcode CSV (downloading if needed)."""
    cache = out_dir / "australian_postcodes.csv"
    if local_path:
        text = Path(local_path).read_text(encoding="utf-8-sig")
    elif cache.exists():
        text = cache.read_text(encoding="utf-8-sig")
    else:
        text = None
        for url in POSTCODE_CSV_URLS:
            print(f"Downloading postcode table from {url} ...")
            try:
                r = http_get(url, timeout=120)
                if r.status_code != 200:
                    raise OSError(f"HTTP {r.status_code}")
                text = r.content.decode("utf-8-sig")
                break
            except (OSError, urllib.error.URLError) as exc:
                print(f"  failed: {exc}")
        if text is None:
            sys.exit("Could not download the postcode table; pass --postcodes-csv <file>")
        cache.write_text(text, encoding="utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def _pick(row: dict, *keys: str) -> str:
    for k in keys:
        if k in row and row[k]:
            return row[k].strip()
    # case-insensitive fallback
    lower = {k.lower(): v for k, v in row.items() if k}
    for k in keys:
        v = lower.get(k.lower())
        if v:
            return v.strip()
    return ""


def sa4_to_region(sa4: str) -> str:
    s = sa4.strip().lower()
    for prefix, region in SA4_TO_REGION.items():
        if s.startswith(prefix.lower()):
            return region
    return ""


def range_fallback(postcode: int) -> str:
    for (lo, hi), region in POSTCODE_RANGE_FALLBACK:
        if lo <= postcode <= hi:
            return region
    return "Border (interstate postcode)"


def build_nsw_postcodes(rows: list[dict]) -> dict[str, dict]:
    """
    postcode -> {localities, sa4, region, lga}
    Only NSW rows.  Keeps every postcode (incl. PO-box-only ones) because a
    licence can be registered against any postcode.
    """
    out: dict[str, dict] = {}
    for row in rows:
        if _pick(row, "state").upper() != "NSW":
            continue
        pc = _pick(row, "postcode").zfill(4)
        if not pc.isdigit():
            continue
        sa4 = _pick(row, "sa4name", "SA4_NAME_2021", "SA4_NAME_2016")
        lga = _pick(row, "lgaregion", "LGA_NAME_2021", "lga")
        locality = _pick(row, "locality").title()
        entry = out.setdefault(
            pc, {"postcode": pc, "localities": set(), "sa4s": Counter(), "lgas": Counter()}
        )
        if locality:
            entry["localities"].add(locality)
        if sa4:
            entry["sa4s"][sa4] += 1
        if lga:
            entry["lgas"][lga] += 1

    for pc, e in out.items():
        # Most common SA4 / LGA wins when a postcode straddles several.
        sa4 = e["sa4s"].most_common(1)[0][0] if e["sa4s"] else ""
        region = sa4_to_region(sa4) if sa4 else ""
        if not region:
            region = range_fallback(int(pc))
        e["sa4"] = sa4
        e["region"] = region
        e["localities"] = "; ".join(sorted(e["localities"]))
        e["lga"] = e["lgas"].most_common(1)[0][0] if e["lgas"] else ""
        del e["sa4s"], e["lgas"]
    return out


# --------------------------------------------------------------------------- #
# API client
# --------------------------------------------------------------------------- #
class MotorAPI:
    def __init__(self, key: str, secret: str, rpm: int):
        self.key = key
        self.secret = secret
        self.min_interval = 60.0 / max(rpm, 1)
        self._last = 0.0
        self._token = None
        self._token_time = 0.0
        self.calls = 0

    def _throttle(self):
        wait = self.min_interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

    def token(self) -> str:
        # Tokens last ~12h; refresh after 11.
        if self._token and time.time() - self._token_time < 11 * 3600:
            return self._token
        basic = base64.b64encode(f"{self.key}:{self.secret}".encode()).decode()
        self._throttle()
        r = http_get(
            TOKEN_URL,
            params={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {basic}"},
            timeout=30,
        )
        self.calls += 1
        if r.status_code != 200:
            sys.exit(f"Auth failed ({r.status_code}): {r.text[:300]}")
        self._token = r.json()["access_token"]
        self._token_time = time.time()
        return self._token

    def _get(self, url: str, params: dict):
        for attempt in range(5):
            self._throttle()
            r = http_get(
                url,
                params=params,
                headers={
                    "Authorization": f"Bearer {self.token()}",
                    "apikey": self.key,
                },
                timeout=60,
            )
            self.calls += 1
            if r.status_code == 200:
                return r.json()
            if r.status_code == 401:
                self._token = None  # force re-auth
                continue
            if r.status_code == 429 or r.status_code >= 500:
                backoff = 15 * (attempt + 1)
                print(f"  {r.status_code} on {params} - retrying in {backoff}s")
                time.sleep(backoff)
                continue
            if r.status_code == 400:
                return []  # "Please provide a valid search term" / nothing found
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        raise RuntimeError(f"Gave up on {url} {params}")

    def browse(self, text: str) -> list[dict]:
        data = self._get(BROWSE_URL, {"searchText": text})
        return data if isinstance(data, list) else []

    def verify(self, licence_number: str) -> list[dict]:
        data = self._get(VERIFY_URL, {"licenceNumber": licence_number})
        return data if isinstance(data, list) else []

    def details(self, licence_id: str) -> dict:
        data = self._get(DETAILS_URL, {"licenceid": licence_id})
        return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- #
# Scrape
# --------------------------------------------------------------------------- #

RESULT_CAP = 200          # the browse endpoint silently truncates at 200 rows
LICENCE_PREFIX = "MVRL"   # every Motor Vehicle Repairer licence number starts with this


def cached_browse(api: MotorAPI, term: str, cache_dir: Path) -> list[dict]:
    cache = cache_dir / f"{term}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    rows = api.browse(term)
    cache.write_text(json.dumps(rows))
    return rows


def cached_verify(api: MotorAPI, number: str, cache_dir: Path) -> list[dict]:
    cache = cache_dir / f"exact_{number}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    rows = api.verify(number)
    cache.write_text(json.dumps(rows))
    return rows


def sweep_prefix(api: MotorAPI, prefix: str, cache_dir: Path, found: dict[str, dict],
                 stats: dict) -> None:
    """
    Depth-first walk of the licence-number space.  The search is a prefix match
    on licence number, so 'MVRL5' hits MVRL5, MVRL50-59, MVRL500-599, ...  If a
    prefix returns the 200-row cap we can't trust it to be complete, so split it
    into ten narrower prefixes.  Rows from the capped parent are kept too (they
    cover the exact-match licence, e.g. MVRL5 itself).
    """
    rows = cached_browse(api, prefix, cache_dir)
    stats["searches"] += 1
    for r in rows:
        lid = r.get("licenceID")
        if lid and lid not in found:
            found[lid] = r
    if len(rows) >= RESULT_CAP:
        # The licence whose number IS this prefix (e.g. MVRL12) is matched only
        # by this capped search, so look it up exactly before splitting.
        for r in cached_verify(api, prefix, cache_dir):
            lid = r.get("licenceID")
            if lid and lid not in found:
                found[lid] = r
        for d in "0123456789":
            sweep_prefix(api, prefix + d, cache_dir, found, stats)
    if stats["searches"] % 50 == 0:
        print(f"  searched {stats['searches']} prefixes (last {prefix}), "
              f"{len(found)} unique licences so far, api calls: {api.calls}")


def flatten_details(d: dict) -> dict:
    ld = d.get("licenceDetail", {}) or {}
    classes = "; ".join(
        c.get("className", "") for c in d.get("licenceClasses", []) or []
        if str(c.get("isActive", "")).lower() in ("true", "y", "yes", "1", "")
    )
    premises = "; ".join(
        f"{p.get('businessName', '')} @ {p.get('businessAddress', '')}".strip(" @")
        for p in d.get("premises", []) or []
    )
    biz = "; ".join(b.get("businessName", "") for b in d.get("businessNames", []) or [])
    comp = d.get("complianceActions", {}) or {}
    return {
        "abn": ld.get("licenceeABN", ""),
        "acn": ld.get("licenceeACN", ""),
        "address_full": ld.get("address", ""),
        "start_date": ld.get("startDate", ""),
        "licence_classes": classes,
        "premises": premises,
        "business_names_full": biz,
        "public_warnings": comp.get("publicWarningsCount", ""),
        "disciplinary_actions": len(comp.get("disciplinaryActions", []) or []),
    }


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="out", help="output directory (default: out)")
    ap.add_argument("--postcodes-csv", help="local copy of australian_postcodes.csv")
    ap.add_argument("--regions", nargs="*",
                    help="only write these regions to the output (the sweep itself is always statewide)")
    ap.add_argument("--postcodes", nargs="*", help="only write these postcodes to the output")
    ap.add_argument("--rpm", type=int, default=None,
                    help=f"API requests per minute (default {DEFAULT_RPM}; sandbox key is capped at 5)")
    ap.add_argument("--include-tradespeople", action="store_true",
                    help="also keep Motor Vehicle Tradesperson certificates (individuals)")
    ap.add_argument("--include-expired", action="store_true",
                    help="keep non-current licences too")
    ap.add_argument("--details", action="store_true",
                    help="fetch /details for each licence (ABN, address, classes)")
    ap.add_argument("--details-limit", type=int, default=0,
                    help="max details calls this run (0 = no limit)")
    ap.add_argument("--list-regions", action="store_true")
    args = ap.parse_args()

    load_dotenv(Path(__file__).resolve().parent / ".env")
    load_dotenv(Path.cwd() / ".env")

    out_dir = Path(args.out)
    cache_dir = out_dir / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # ---- 1. postcodes ------------------------------------------------------
    rows = load_postcode_table(args.postcodes_csv, out_dir)
    nsw = build_nsw_postcodes(rows)
    pc_csv = out_dir / "nsw_postcodes_regions.csv"
    with pc_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["postcode", "region", "sa4", "lga", "localities"])
        w.writeheader()
        for pc in sorted(nsw):
            w.writerow({k: nsw[pc][k] for k in w.fieldnames})
    print(f"{len(nsw)} NSW postcodes -> {pc_csv}")

    if args.list_regions:
        by_region = defaultdict(list)
        for pc, e in nsw.items():
            by_region[e["region"]].append(pc)
        for region in sorted(by_region):
            pcs = sorted(by_region[region])
            print(f"{region:35s} {len(pcs):4d} postcodes  ({pcs[0]}-{pcs[-1]})")
        return

    region_filter = {r.lower() for r in args.regions} if args.regions else None
    postcode_filter = {p.zfill(4) for p in args.postcodes} if args.postcodes else None

    # ---- 2. auth -----------------------------------------------------------
    key, secret = os.getenv("NSW_API_KEY"), os.getenv("NSW_API_SECRET")
    if not key or not secret:
        sys.exit("No credentials found. Create a file named .env next to this script containing:\n"
                 "  NSW_API_KEY=your_key\n  NSW_API_SECRET=your_secret\n  NSW_API_RPM=60\n"
                 "(register free at https://api.nsw.gov.au and subscribe to 'Motor API')")
    rpm = args.rpm or int(os.getenv("NSW_API_RPM", DEFAULT_RPM))
    api = MotorAPI(key, secret, rpm)
    cached = len(list(cache_dir.glob(f"{LICENCE_PREFIX}*.json")))
    print(f"Sweeping licence numbers {LICENCE_PREFIX}0..{LICENCE_PREFIX}9 "
          f"({cached} prefixes already cached; a full first run is ~700-800 calls, "
          f"~{750 / max(rpm, 1):.0f} min at {rpm} req/min)")

    # ---- 3. sweep the register ----------------------------------------------
    raw: dict[str, dict] = {}
    stats = {"searches": 0}
    for d in "0123456789":
        sweep_prefix(api, LICENCE_PREFIX + d, cache_dir, raw, stats)
    print(f"Sweep done: {stats['searches']} searches, {len(raw)} unique records")

    type_counts = Counter((r.get("licenceType") or "?") for r in raw.values())
    print("  by licence type:", dict(type_counts))

    licences: dict[str, dict] = {}
    skipped = Counter()
    for lid, r in raw.items():
        ltype = r.get("licenceType") or ""
        if not args.include_tradespeople and not REPAIRER_TYPE_RE.search(ltype):
            skipped["not a repairer licence"] += 1
            continue
        status = (r.get("status") or "").strip().lower()
        if not args.include_expired and status not in ("current", ""):
            skipped[f"status {status}"] += 1
            continue
        pc = str(r.get("postcode") or "").strip()
        e = nsw.get(pc)
        if e:
            region, sa4, lga = e["region"], e["sa4"], e["lga"]
        elif pc:
            region, sa4, lga = "Outside NSW postcode table", "", ""
        else:
            region, sa4, lga = "Unknown (no address in register)", "", ""
        if region_filter and region.lower() not in region_filter:
            continue
        if postcode_filter and pc not in postcode_filter:
            continue
        licences[lid] = {
            "licence_id": lid,
            "licence_number": r.get("licenceNumber", ""),
            "licence_type": ltype,
            "status": r.get("status", ""),
            "licensee": (r.get("licensee") or "").strip(),
            "licence_name": r.get("licenceName", "") or "",
            "business_names": r.get("businessNames", "") or "",
            "categories": r.get("categories", "") or "",
            "classes": r.get("classes", "") or "",
            "expiry_date": r.get("expiryDate", "") or "",
            "suburb": r.get("suburb", "") or "",
            "postcode": pc,
            "region": region,
            "sa4": sa4,
            "lga": lga,
        }
    if skipped:
        print("  dropped:", dict(skipped))

    # ---- 4. details (optional) --------------------------------------------
    detail_fields = []
    if args.details:
        detail_fields = list(flatten_details({}).keys())
        dcache = out_dir / "cache_details"
        dcache.mkdir(exist_ok=True)
        todo = [lid for lid in licences if not (dcache / f"{lid}.json").exists()]
        if args.details_limit:
            todo = todo[: args.details_limit]
        print(f"Fetching details for {len(todo)} licences "
              f"({len(licences) - len(todo)} already cached) ...")
        for n, lid in enumerate(todo, 1):
            (dcache / f"{lid}.json").write_text(json.dumps(api.details(lid)))
            if n % 50 == 0:
                print(f"  details {n}/{len(todo)}  api calls: {api.calls}")
        for lid, rec in licences.items():
            p = dcache / f"{lid}.json"
            rec.update(flatten_details(json.loads(p.read_text())) if p.exists()
                       else {k: "" for k in detail_fields})

    # ---- 5. write ----------------------------------------------------------
    fields = ["licence_number", "licensee", "licence_name", "business_names", "licence_type",
              "status", "expiry_date", "classes", "categories", "suburb", "postcode", "region",
              "sa4", "lga", *detail_fields, "licence_id"]
    main_csv = out_dir / "nsw_motor_repairers.csv"
    with main_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for rec in sorted(licences.values(), key=lambda r: (r["region"], r["postcode"], r["licensee"])):
            w.writerow(rec)

    summary = Counter(r["region"] for r in licences.values())
    with (out_dir / "summary_by_region.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["region", "licences"])
        for region, n in summary.most_common():
            w.writerow([region, n])

    print(f"\n{len(licences)} licences -> {main_csv}")
    for region, n in summary.most_common():
        print(f"  {region:35s} {n:6d}")
    print(f"API calls this run: {api.calls}")


if __name__ == "__main__":
    main()
