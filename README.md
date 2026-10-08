# project-repair — NSW motor-repairer enrichment

`data/register_summary.csv` holds every current NSW Motor Vehicle Repairer licence
(12,613 rows, scraped by `nsw_repairers.py` from the api.nsw "Motor API").
`pipeline.py` enriches each licence with the `details` endpoint (licence classes,
premises, ABN/ACN, full address), then applies deterministic name/class rules to find
service-focused repairers and estimate shops per operator. A GitHub Actions workflow
runs it hourly, unattended, as fast as api.nsw allows, commits state back to `data/`,
and disables itself when the queue is empty. No AI, no pip, stdlib only.

## Setup (once)

1. Register at <https://api.nsw.gov.au>, subscribe to **Motor API**, copy the key and secret.
2. Repo → Settings → Secrets and variables → Actions → add secrets `NSW_API_KEY` and `NSW_API_SECRET`.
3. (Optional) add secret `ABR_GUID` from <https://abr.business.gov.au/Tools/WebServices> to enrich ABNs.
4. (Optional) add repository **variable** `MONTHLY_CALL_BUDGET` (e.g. `2400`) to cap calls per month; default 0 = no cap.
5. Repo → Actions → enable workflows if prompted.
6. Make sure the seed `data/register_summary.csv` is committed.
7. Locally: `python pipeline.py prioritise` → writes `data/queue.csv` (fetch order, most valuable first).
8. `python pipeline.py build` → writes `data/out/` from names only (no API needed).
9. Commit `data/queue.csv` + `data/out/` and push.
10. Actions → **enrich** → *Run workflow* (optionally `run_seconds=120` for a smoke test). The hourly cron takes over from there.

Locally you can also copy `.env.example` to `.env`, fill in the keys, and run
`python pipeline.py all` (fetch-details → fetch-abr → build). `DRY_RUN=1` skips API calls and pushes.

## How it runs

- **Cron `7 * * * *`**, `timeout-minutes: 55`, one run at a time (`concurrency: enrich`).
  Each run: unit tests → `fetch-details` for up to `RUN_SECONDS` (3000 s) → `fetch-abr` (rest of the
  time, if `ABR_GUID` set) → `build` → commit & push `data/` → disable the workflow if the queue is empty.
  The workflow file is `.github/workflows/enrich-register.yml` (the original `enrich.yml` was
  self-disabled by a false "complete" and GitHub keeps that state per file path).
- **Adaptive rate**: starts at 60 req/min (or the last good rate in `data/budget.json`), +20 % after every
  100 consecutive successes, halves on any 429 and holds 60 s / 120 s / 180 s.
- **Quota exhaustion**: api.nsw answers a spent monthly quota with **HTTP 408** and the body
  `Quota limit of 2500 per 1 month exceeded.` Any error status whose body mentions *quota* /
  *limit exceeded*, or a 429 that survives the three holds (≥ 5 min), is treated as the month being
  spent. `budget.json` gets `quota_exhausted_until` = first of next month (UTC); every hourly run until
  then exits in a few seconds without touching the API, and nothing is committed. The workflow is
  **not** disabled, so it resumes by itself next month. Hitting `MONTHLY_CALL_BUDGET` behaves the same
  way. The 2,500/month quota is shared with every other call made with the same key (the
  `nsw_repairers.py` sweep counts against it too).
- **Several api.nsw accounts**: put every pair in one GitHub secret `NSW_API_KEYS`, one `key:secret`
  per line (locally in `.env`, separate pairs with `;`). `NSW_API_KEY`/`NSW_API_SECRET` still count as
  the first pair. The client fetches with one key until api.nsw answers the quota body, marks that key
  spent in `budget.json` (`keys`, by a short non-secret id), switches to the next key and retries the
  same licence, so no call and no licence is wasted. A key that fails auth is skipped for the run.
  Only when every configured key is spent does the month-long pause start, and adding a new line to
  the secret resumes fetching on the next hourly run by itself. Each free account covers 2,500
  details, about 7 minutes at the rate the client reaches.
- **Got a quota increase mid-month?** Actions → **enrich** → *Run workflow* with `reset_quota=1`;
  that clears `quota_exhausted_until` and fetching resumes immediately. Without a per-minute
  throttle the client ramps past 350 req/min, so the remaining queue finishes in a single run.
- **Systemic errors**: 10 consecutive non-quota HTTP errors stop the run (the last one is not recorded,
  so it is retried) rather than burning through the queue.
- **Checkpoints**: every 200 fetches the run flushes `details.jsonl`, commits as `github-actions[bot]`,
  `git pull --rebase`, pushes. A killed run loses at most 200 fetches' worth of work.
- **Per-licence 4xx** (other than 429) is recorded in `details.jsonl` with an `error` field so it is
  not retried; delete those lines if you want a retry.

## Files

| path | what |
|---|---|
| `data/register_summary.csv` | seed: all current repairer licences (from `nsw_repairers.py`) |
| `data/queue.csv` | fetch order with `tier` (1–5) and `name_flags` |
| `data/details.jsonl` | append-only raw `details` responses (`licence_id`, `fetched_at`, `raw`) |
| `data/abr.jsonl` | append-only ABN Lookup responses per ABN |
| `data/budget.json` | `last_rate`, `calls_used_this_month` (reporting only), `quota_exhausted_until` |
| `data/status.json` | `complete`, `remaining`, `quota_exhausted_until`, `last_rate`, last run reason |
| `data/out/licences_enriched.csv` | one row per licence: summary + details columns, `segment_rule`, `tier`, `details_fetched` |
| `data/out/operators.csv` | one row per operator (grouped by ABN, else normalised licensee name), sorted by premises |
| `data/out/shortlist.csv` | `block=multi_site`: independent multi-site service operators; `block=single_site_company`: widen later |
| `data/out/summary.md` | progress, quota state, region × segment, tiers, top 50 operators |

All rules (service words, exclusion families, franchise brands, licence-class → segment) live in the
dicts at the top of `pipeline.py`.

## Reading `summary.md`

- **Fetch progress** — fetched / remaining / ETA at the last good rate / calls this month / quota state.
  `quota state: paused until …` means the month's quota is spent and runs are idling until that date.
  `complete: True` means every licence has been fetched and the workflow has disabled itself.
- **Licences by region × segment** — `segment_rule` comes from licence classes when details are fetched
  (mechanical-repair classes → `service`; body/paint only → `body`; tyre/electrical only → `specialist`),
  otherwise from name flags (`dealer`, `mobile`, `other`, or `unknown` for uninformative names).
  Expect the `unknown` column to shrink and `Unknown (no address in register)` rows to move into real
  regions as details arrive (the details address supplies the postcode).
- **Queue tiers** — 1 company + service name, 2 company uninformative, 3 sole trader + service name,
  4 exclusion family in name, 5 sole trader uninformative. Fetching goes in that order, multi-licence
  holders first within a tier.
- **Top 50 operators** — by `n_premises_total` = distinct premises addresses from details, plus one per
  licence that has no details yet.

## Alternative: the public licence-check website (no API quota)

`scrape_site.py` gets the same details from https://verify.licence.nsw.gov.au, the public site the
API mirrors. It needs a real browser once, to learn which backend call the site makes, then replays
that call directly. It writes the same `details.jsonl` records, so `build` is unchanged.

```
pip install playwright && python -m playwright install chromium   # once
python scrape_site.py discover MVRL24145         # opens the site, records the backend call -> data/site_config.json
python scrape_site.py fetch --rate 1 --limit 50  # try 50; then drop --limit
python pipeline.py build
```

`fetch` skips everything already in `details.jsonl` / `details_site.jsonl` (in details mode it
re-fetches records that only came from the search call), runs at `--rate` requests/second with
jitter, and stops after 10 consecutive refusals (403/429/5xx) so a block is noticed rather than
hammered. `--run-seconds` and `--checkpoint N` (commit every N records, Actions only) make it fit a
workflow run.

Two workflows drive it in Actions:

- **`site-scrape-test.yml`**: runs `discover` plus a small fetch and prints every backend response it
  saw; triggered by pushes to the development branch or by hand. This is how the endpoints were found:
  the site's details page calls `GET /publicregisterapi/api/v1/licence/search/details/<licence type>/<licenceId>`
  and returns `{"componentData": {...}}` with premises under `locations[].premises[]`, current classes,
  the pre-2014 class history, directors and a compliance summary. `pipeline.py` normalises that shape.
- **`site-scrape.yml`**: hourly cron (37 past) and dispatch; fetches in details mode at 2 req/s for up to
  50 minutes with a checkpoint commit every 500 records, rebuilds `data/out/`, and disables itself when
  every licence has a full details record. About 9,000 licences take two runs.

## Re-enabling for a future refresh

1. Re-run the scraper to refresh the seed: `python nsw_repairers.py` then copy `out/nsw_motor_repairers.csv`
   to `data/register_summary.csv`.
2. To re-fetch everything, delete `data/details.jsonl` (and `data/abr.jsonl`); to only pick up new
   licences, leave them in place — the queue skips anything already fetched.
3. `python pipeline.py prioritise`, commit, push.
4. Actions → **enrich** → *Enable workflow* (or `gh workflow enable enrich-register.yml`), then *Run workflow*.

## Tests

```
python -m unittest discover -s tests -v
```

Covers adaptive rate up/down, 429-throttle vs quota-exhausted detection (incl. the HTTP 408 quota
body), the consecutive-error guard, resume after a partial run, checkpoint commits every 200, JSONL
dedupe, ACN/ABN grouping, premises counting, suburb → postcode, the complete → status.json path, the
fast exit when `quota_exhausted_until` is in the future, `reset_quota`, and the offline build.
