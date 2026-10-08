#!/usr/bin/env python3
"""
scrape_site.py — get licence details from the public NSW licence-check website
(https://verify.licence.nsw.gov.au) instead of the quota-limited api.nsw Motor API.

The website is a JavaScript app that fetches each licence's details from its own backend.
This script first *discovers* that backend call by driving a real browser once, then replays
it directly (fast, no browser) for every licence still missing from data/details.jsonl.
Records are written in exactly the format pipeline.py already reads, so `pipeline.py build`
needs no changes.

Steps
  1. pip install playwright && python -m playwright install chromium      (once, locally)
  2. python scrape_site.py discover MVRL24145
        opens the site, searches that licence, opens its details page, records the backend
        request(s) that returned the licence data -> data/site_config.json, and prints a
        sample of the JSON.  Use --headed to watch it.
  3. python scrape_site.py fetch [--rate 1.0] [--limit N]
        replays the discovered endpoint for every licence not yet in details.jsonl.
        Add --browser to drive Chromium per licence instead (slower; use when the direct
        replay is refused with 401/403).
  4. python pipeline.py build

Behaviour
  - resumable: licences already in details.jsonl (except quota-rejected ones) are skipped
  - polite: --rate requests/second (default 1.0) with +-30 % jitter; stops after 10
    consecutive 403/429/5xx so a block is noticed instead of hammered
  - records: {"licence_id","licence_number","fetched_at","source":"site","raw":{...}}
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pipeline as pl  # noqa: E402

SITE = "https://verify.licence.nsw.gov.au"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
MARKERS = ("licenceDetail", "licenceNumber", "licensee")   # a response with these carries licence data
DETAIL_MARKERS = ("premises", "licenceDetail", "conditions", "associatedParties", "licenceClasses")
TRACE_HEADERS = ("newrelic", "traceparent", "tracestate", "x-correlation-id", "x-request-id")
MAX_CONSECUTIVE_ERRORS = 10


def config_path() -> Path:
    return pl.data_dir() / "site_config.json"


def log(msg: str) -> None:
    pl.log(msg)


# --------------------------------------------------------------------------- #
# discover: drive the browser once, capture the backend call
# --------------------------------------------------------------------------- #

def looks_like_details(body: str, licence_number: str) -> bool:
    return licence_number in body and sum(m in body for m in MARKERS) >= 2


def is_search_response(body: str) -> bool:
    return '"pagingInfo"' in body or '"totalRecords"' in body


def has_detail_markers(body: str) -> bool:
    return sum(m in body for m in DETAIL_MARKERS) >= 2


def endpoint_config(c: dict, licence_number: str, licence_id: str | None, page_url: str, mode: str) -> dict:
    return {
        "mode": mode,
        "page_url": page_url,
        "method": c["method"],
        "url_template": template_from(c["url"], licence_number, licence_id),
        "post_data_template": (template_from(c["post_data"], licence_number, licence_id)
                               if c["post_data"] else None),
        "headers": {k: v for k, v in c["headers"].items() if k.lower() not in TRACE_HEADERS},
    }


def template_from(url: str, licence_number: str, licence_id: str | None) -> str:
    """Replace the licence identifier in a captured URL with a placeholder."""
    for value, token in ((licence_id, "{licence_id}"), (licence_number, "{licence_number}")):
        if value and value in url:
            return url.replace(value, token)
        if value and urllib.parse.quote(value, safe="") in url:
            return url.replace(urllib.parse.quote(value, safe=""), token)
    return url


def discover(licence_number: str, headed: bool = False, timeout_s: int = 60,
             debug_dir: Path | None = None) -> dict:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright is not installed: pip install playwright && python -m playwright install chromium")

    captured: list[dict] = []
    seed = {r["licence_number"]: r for r in pl.read_csv(pl.data_dir() / "register_summary.csv")}
    licence_id = seed.get(licence_number, {}).get("licence_id")
    if debug_dir:
        debug_dir.mkdir(parents=True, exist_ok=True)
    shots = [0]

    def snap(page, label):
        if debug_dir:
            shots[0] += 1
            try:
                page.screenshot(path=str(debug_dir / f"{shots[0]:02d}-{label}.png"), full_page=True)
                (debug_dir / f"{shots[0]:02d}-{label}.html").write_text(page.content(), encoding="utf-8")
            except Exception as exc:
                log(f"screenshot {label} failed: {exc}")

    def settle(page):
        try:
            page.wait_for_load_state("networkidle", timeout=timeout_s * 1000)
        except Exception:
            page.wait_for_timeout(3000)

    def on_response(resp):
        try:
            ct = resp.headers.get("content-type", "")
            if "json" not in ct and "javascript" not in ct and "text" not in ct:
                return
            body = resp.text()
        except Exception:
            return
        if debug_dir and ("json" in ct or resp.request.resource_type in ("xhr", "fetch")):
            # every data-ish response, so the right call can be found by hand if the markers miss
            pl.append_jsonl(debug_dir / "responses.jsonl", {
                "url": resp.url, "status": resp.status, "method": resp.request.method,
                "content_type": ct, "post_data": resp.request.post_data, "body_head": body[:600]})
        if looks_like_details(body, licence_number):
            req = resp.request
            captured.append({
                "url": req.url, "method": req.method,
                "headers": {k: v for k, v in req.headers.items()
                            if k.lower() not in ("content-length", "cookie", "host")},
                "post_data": req.post_data, "status": resp.status, "body": body,
            })
            log(f"captured {req.method} {req.url} ({len(body)} bytes)")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed,
                                    executable_path=os.environ.get("CHROMIUM_PATH") or None)
        page = browser.new_page(user_agent=UA)
        page.on("response", on_response)
        log(f"opening {SITE}")
        page.goto(SITE, timeout=timeout_s * 1000)
        settle(page)
        snap(page, "home")
        log(f"landed on {page.url}; title {page.title()!r}")
        # The search box: try the obvious selectors, then anything that looks like a search input.
        box = None
        for sel in ("input[type=search]", "input[placeholder*='icence' i]", "input[placeholder*='earch' i]",
                    "input[name*='search' i]", "input[aria-label*='earch' i]", "input[type=text]", "input"):
            if page.locator(sel).count():
                box = page.locator(sel).first
                log(f"search box: {sel}")
                break
        if box is None:
            snap(page, "no-search-box")
            browser.close()
            sys.exit("could not find the search box; see the screenshots")
        box.fill(licence_number)
        box.press("Enter")
        settle(page)
        snap(page, "after-search")
        results_url = page.url
        log(f"after search: {results_url}")

        def have_details() -> bool:
            return any(has_detail_markers(c["body"]) and not is_search_response(c["body"]) for c in captured)

        # results list -> open the licence's own page (the card, its link, or its title)
        for sel in (f"a:has-text('{licence_number}')", ".nsw-result-card a", ".nsw-result-card__title a",
                    "a[href*='details']", ".nsw-result-card", f"text={licence_number}"):
            if have_details():
                break
            loc = page.locator(sel)
            if not loc.count():
                continue
            try:
                loc.first.click(timeout=8_000, force=sel.startswith(("text=", ".nsw-result-card")))
                try:
                    page.wait_for_url(lambda u: u != results_url, timeout=15_000)
                except Exception:
                    pass
                settle(page)
                snap(page, "after-click")
                log(f"clicked {sel!r} -> {page.url}")
            except Exception as exc:
                log(f"click {sel!r} failed: {str(exc)[:160]}")
        if not have_details():
            # some builds expose the details page directly
            for url in (f"{SITE}/details/{licence_id or licence_number}",
                        f"{SITE}/licence/{licence_id or licence_number}",
                        f"{SITE}/details/Motor/{licence_id or licence_number}"):
                try:
                    page.goto(url, timeout=timeout_s * 1000)
                    settle(page)
                    snap(page, "direct-url")
                    log(f"direct url {url} -> {page.url}")
                except Exception as exc:
                    log(f"direct url {url} failed: {str(exc)[:120]}")
                if have_details():
                    break
        final_url = page.url
        browser.close()

    if not captured:
        sys.exit("no backend response carrying the licence details was seen; check the screenshots "
                 "and responses.jsonl in the debug dir, and send me the request that returns the "
                 "licence JSON")
    details = [c for c in captured if has_detail_markers(c["body"]) and not is_search_response(c["body"])]
    searches = [c for c in captured if is_search_response(c["body"])]
    best_d = max(details, key=lambda c: len(c["body"])) if details else None
    best_s = max(searches, key=lambda c: len(c["body"])) if searches else None
    cfg = {
        "discovered_at": pl.iso(),
        "discovered_with": licence_number,
        "final_page_url": final_url,
        "details": endpoint_config(best_d, licence_number, licence_id, final_url, "details") if best_d else None,
        "search": endpoint_config(best_s, licence_number, licence_id, results_url, "search") if best_s else None,
    }
    pl.write_json(config_path(), cfg)
    log(f"saved {config_path()}")
    print(json.dumps(cfg, indent=2))
    for label, c in (("details", best_d), ("search", best_s)):
        if c:
            try:
                sample = json.loads(c["body"])
            except json.JSONDecodeError:
                sample = {"_text": c["body"][:2000]}
            print(f"\nsample of the {label} JSON (first 3000 chars):")
            print(json.dumps(sample, indent=1)[:3000])
    if not best_d:
        print("\nWARNING: no details endpoint found; `fetch` will use the search endpoint, which gives "
              "ABN/ACN/address/postcode but not premises or classes.")
    for label in ("details", "search"):
        e = cfg[label]
        if e and "{licence" not in e["url_template"] and "{licence" not in (e["post_data_template"] or ""):
            print(f"\nWARNING: the {label} request carries no licence id/number; it may be session-bound.")
    return cfg


# --------------------------------------------------------------------------- #
# fetch: replay the discovered call (HTTP) or drive the browser per licence
# --------------------------------------------------------------------------- #

def fill(template: str | None, licence_id: str, licence_number: str) -> str | None:
    if template is None:
        return None
    return (template.replace("{licence_id}", urllib.parse.quote(licence_id, safe=""))
                    .replace("{licence_number}", urllib.parse.quote(licence_number, safe="")))


def http_call(cfg: dict, licence_id: str, licence_number: str, timeout: int = 30) -> tuple[int, str]:
    url = fill(cfg["url_template"], licence_id, licence_number)
    data = fill(cfg.get("post_data_template"), licence_id, licence_number)
    headers = dict(cfg.get("headers") or {})
    headers.setdefault("user-agent", UA)
    headers.setdefault("accept", "application/json, text/plain, */*")
    headers.setdefault("referer", cfg.get("page_url") or SITE)
    req = urllib.request.Request(url, data=data.encode() if data else None,
                                 headers=headers, method=cfg.get("method", "GET"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        return 599, str(e)


OUT_FILE = "details_site.jsonl"   # kept apart from the api.nsw file so the two never conflict in git


def out_path() -> Path:
    return pl.data_dir() / OUT_FILE


def todo_licences(limit: int = 0) -> list[dict]:
    d = pl.data_dir()
    queue = pl.ensure_queue(d)
    done = pl.fetched_ids(pl.load_details(d))
    todo = [q for q in queue if q["licence_id"] not in done]
    return todo[:limit] if limit else todo


def wrap_search_result(obj, licence_number: str, licence_id: str | None = None):
    """Turn a search response into a details-shaped record for the licence.  The search row has
    ABN/ACN, licensee and the registered address with postcode, which flatten() reads from
    licenceDetail; the address doubles as the one known premises."""
    rows = obj.get("results") if isinstance(obj, dict) else obj
    if not isinstance(rows, list):
        return None
    for r in rows:
        if not isinstance(r, dict):
            continue
        if r.get("licenceNumber") == licence_number or (licence_id and r.get("licenceId") == licence_id):
            addr = r.get("address") or ""
            return {"licenceDetail": dict(r, licenceeABN=r.get("ABN", ""), licenceeACN=r.get("ACN", ""),
                                          startDate=r.get("granted", ""), expiryDate=r.get("expires", "")),
                    "premises": [{"type": "Registered address", "businessName": None,
                                  "businessAddress": addr, "endDate": None}] if addr else [],
                    "licenceClasses": [], "conditions": [], "businessNames": [],
                    "_site": "search"}
    return None


def parse_body(body: str, licence_number: str, mode: str = "details", licence_id: str | None = None):
    """The site may wrap the payload (e.g. {"data": {...}}) or return the details object itself."""
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        return None
    if mode == "search":
        return wrap_search_result(obj, licence_number, licence_id)
    if isinstance(obj, dict):
        if "licenceDetail" in obj or "licenceDetails" in obj:
            return obj
        for v in obj.values():
            if isinstance(v, dict) and ("licenceDetail" in v or "licenceDetails" in v):
                return v
            if isinstance(v, list):
                for item in v:
                    if isinstance(item, dict) and ("licenceDetail" in item or "licenceDetails" in item):
                        return item
        return obj if licence_number in body else None
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        return obj[0]
    return None


def fetch_http(cfg: dict, rate: float, limit: int, call=http_call, sleep=time.sleep) -> dict:
    d = pl.data_dir()
    todo = todo_licences(limit)
    log(f"site fetch: {len(todo)} licences to do at {rate}/s")
    ok = errors = consecutive = 0
    t0 = time.time()
    for n, q in enumerate(todo, 1):
        status, body = call(cfg, q["licence_id"], q["licence_number"])
        rec = {"licence_id": q["licence_id"], "licence_number": q["licence_number"],
               "fetched_at": pl.iso(), "source": f"site-{cfg.get('mode', 'details')}"}
        payload = (parse_body(body, q["licence_number"], cfg.get("mode", "details"), q["licence_id"])
                   if status == 200 else None)
        if payload is not None:
            rec["raw"] = payload
            ok += 1
            consecutive = 0
            pl.append_jsonl(out_path(), rec)
        else:
            errors += 1
            consecutive += 1
            log(f"{q['licence_number']}: HTTP {status} {body[:120]!r}")
            if status == 404:
                rec["error"] = "HTTP 404"
                rec["raw"] = {"body": body[:300]}
                pl.append_jsonl(out_path(), rec)
            if consecutive >= MAX_CONSECUTIVE_ERRORS:
                log(f"{consecutive} consecutive failures (last HTTP {status}) -> stopping; "
                    "the site is probably refusing us. Try --browser, a lower --rate, or later.")
                break
        if n % 100 == 0:
            log(f"{n}/{len(todo)} done, {ok} ok, {errors} errors, "
                f"{n / max(time.time() - t0, 1):.2f}/s")
        sleep(max(0.0, (1.0 / rate) * random.uniform(0.7, 1.3)))
    log(f"site fetch done: {ok} ok, {errors} errors")
    return {"ok": ok, "errors": errors}


def fetch_browser(cfg: dict, rate: float, limit: int, headed: bool = False) -> dict:
    """Per-licence browser navigation; the page's own backend response is captured."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright is not installed: pip install playwright && python -m playwright install chromium")
    d = pl.data_dir()
    todo = todo_licences(limit)
    page_tpl = cfg.get("page_url_template") or cfg.get("page_url", SITE)
    log(f"site fetch (browser): {len(todo)} licences; page template {page_tpl}")
    ok = errors = consecutive = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed,
                                    executable_path=os.environ.get("CHROMIUM_PATH") or None)
        page = browser.new_page(user_agent=UA)
        holder: dict = {}

        def on_response(resp):
            try:
                body = resp.text()
            except Exception:
                return
            if holder.get("number") and looks_like_details(body, holder["number"]):
                holder["body"] = body

        page.on("response", on_response)
        for n, q in enumerate(todo, 1):
            holder.clear()
            holder["number"] = q["licence_number"]
            url = fill(page_tpl, q["licence_id"], q["licence_number"]) if "{licence" in page_tpl else None
            try:
                if url:
                    page.goto(url, wait_until="networkidle", timeout=60_000)
                else:
                    page.goto(SITE, wait_until="networkidle", timeout=60_000)
                    box = page.locator("input[type=search], input[type=text]").first
                    box.fill(q["licence_number"])
                    box.press("Enter")
                    page.wait_for_load_state("networkidle", timeout=60_000)
                    if "body" not in holder:
                        page.get_by_text(q["licence_number"]).first.click(timeout=10_000)
                        page.wait_for_load_state("networkidle", timeout=60_000)
            except Exception as exc:
                log(f"{q['licence_number']}: navigation failed: {str(exc)[:120]}")
            payload = parse_body(holder.get("body", ""), q["licence_number"]) if holder.get("body") else None
            if payload is not None:
                pl.append_jsonl(out_path(), {"licence_id": q["licence_id"],
                                                      "licence_number": q["licence_number"],
                                                      "fetched_at": pl.iso(), "source": "site",
                                                      "raw": payload})
                ok += 1
                consecutive = 0
            else:
                errors += 1
                consecutive += 1
                if consecutive >= MAX_CONSECUTIVE_ERRORS:
                    log(f"{consecutive} consecutive failures -> stopping")
                    break
            if n % 50 == 0:
                log(f"{n}/{len(todo)} done, {ok} ok, {errors} errors")
            time.sleep(max(0.0, (1.0 / rate) * random.uniform(0.7, 1.3)))
        browser.close()
    log(f"site fetch (browser) done: {ok} ok, {errors} errors")
    return {"ok": ok, "errors": errors}


# --------------------------------------------------------------------------- #

def main(argv=None) -> int:
    pl.load_dotenv(HERE / ".env")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("discover", help="drive the browser once and record the backend call")
    s.add_argument("licence_number", nargs="?", default="MVRL24145")
    s.add_argument("--headed", action="store_true")
    s.add_argument("--debug-dir", default="site_debug",
                   help="screenshots, page HTML and every data response go here (default site_debug/)")
    f = sub.add_parser("fetch", help="fetch every licence not yet in details.jsonl")
    f.add_argument("--rate", type=float, default=1.0, help="requests per second (default 1)")
    f.add_argument("--limit", type=int, default=0, help="stop after N licences (0 = all)")
    f.add_argument("--browser", action="store_true", help="drive Chromium per licence")
    f.add_argument("--headed", action="store_true")
    f.add_argument("--mode", choices=("auto", "details", "search"), default="auto",
                   help="which discovered endpoint to replay (auto = details if found, else search)")
    args = ap.parse_args(argv)
    if args.cmd == "discover":
        discover(args.licence_number, headed=args.headed,
                 debug_dir=Path(args.debug_dir) if args.debug_dir else None)
        return 0
    site = pl.read_json(config_path(), {})
    if not site:
        sys.exit(f"{config_path()} missing: run `python scrape_site.py discover` first")
    if "url_template" in site:          # config written by an older discover
        site = {"details": dict(site, mode="details"), "search": None}
    if args.mode == "auto":
        cfg = site.get("details") or site.get("search")
    else:
        cfg = site.get(args.mode)
    if not cfg:
        sys.exit(f"no {args.mode} endpoint in {config_path()}; run discover again")
    log(f"using the {cfg.get('mode')} endpoint: {cfg['method']} {cfg['url_template']}")
    if args.browser:
        fetch_browser(cfg, args.rate, args.limit, headed=args.headed)
    else:
        fetch_http(cfg, args.rate, args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
