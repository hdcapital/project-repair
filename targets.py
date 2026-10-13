"""
targets.py — group licensed operators by shared directors, then score and tier each group as an
acquisition target.  Deterministic rules only; called from `pipeline.py build`.

Groups
  Operators (one per ACN/ABN, see pipeline.entity_key) are joined when they share a person: a
  director, or the licensee's own name for a sole trader.  A three-part name ("Ryan Paul Sutton")
  links on its own; a two-part name ("Lauren Adlam") links only if the two operators also share a
  region or a second person, so common names do not chain unrelated businesses together.

Score (0-100) = fit (45) + succession (35) + deal-ability (20)
  fit          share of licences in the service segment (unknown names count half), number of
               sites (2-10 best), and how many other service operators sit within 25 km (the
               bolt-on pool)
  succession   years since the group's earliest licence, how few people own it, and a family or
               founder signal (a director's surname in the trading name, or directors sharing one)
  deal-ability a company rather than a sole trader, a valid ABN/ACN, no compliance actions

Exclusions (tier X, score kept for reference): any dealer or franchise entity in the group,
mobile-only, or less than half the licences in a service segment.

Tiers
  A  platform candidates: not excluded, 2+ sites
  B  bolt-ons: not excluded, one site, a company, within 25 km of a tier-A group
  C  everything else not excluded
  X  excluded (reason given)
"""
from __future__ import annotations

import csv
import datetime as dt
import math
import os
import re
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent

NEARBY_KM = 25.0
# The register's records begin on 31 March 1981; that date means "licensed then or earlier".
REGISTER_START = dt.date(1981, 3, 31)
# Focus region: groups with a site within FOCUS_KM (straight line) of FOCUS_POINT are listed first
# in every tier.  80 km straight-line is about an hour's drive from Newcastle: Singleton 77 km,
# Gosford 71 km, Dungog 73 km; Muswellbrook (126 km) and Forster (102 km) fall outside.
FOCUS_NAME = "Newcastle"
FOCUS_POINT = (-32.9283, 151.7817)
FOCUS_KM = 80.0
# Licence types in the register that mean the business sells cars (or trades them).  The search
# call files a dealer licence as licenceType "Motor" with licenceTypeFriendly "Motor Dealer's
# Licence" and a number starting MD, so all three are checked.
DEALER_LICENCE_RE = re.compile(r"dealer|wholesal|auction", re.I)


def is_dealer_licence(h: dict) -> bool:
    return bool(DEALER_LICENCE_RE.search(f"{h.get('licenceType') or ''} {h.get('licenceTypeFriendly') or ''}")
                or str(h.get("licenceNumber") or "").upper().startswith("MD"))
UNKNOWN_SERVICE_WEIGHT = 0.5
GENERIC_SURNAMES = {"AUTO", "AUTOS", "MOTOR", "MOTORS", "SERVICE", "SERVICES", "PTY", "LTD", "GROUP",
                    "HOLDINGS", "NSW", "AUSTRALIA", "AND", "THE", "CAR", "CARS", "REPAIRS", "MECHANICAL"}

TARGET_FIELDS = ["rank", "tier", "score", "fit", "succession", "dealability", "name", "why",
                 "exclusion", "in_focus", "km_to_focus", "dealer_check", "other_licences",
                 "n_entities", "n_licences", "n_sites", "regions", "nearby_service_operators",
                 "first_licensed", "tenure_years", "people", "n_people", "family_signal",
                 "service_share", "segments", "entities", "abns", "acns", "licence_numbers", "premises",
                 "group_id"]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def norm_person(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z ]", " ", (s or "").upper())).strip()


def parse_date(s: str) -> dt.date | None:
    s = (s or "").strip()
    for fmt, n in (("%Y-%m-%d", 10), ("%d/%m/%Y", 10)):
        try:
            return dt.datetime.strptime(s[:n], fmt).date()
        except ValueError:
            continue
    return None


def postcode_points(path: str | os.PathLike | None = None) -> dict[str, tuple[float, float]]:
    """Mean lat/long per postcode from australian_postcodes.csv (all states)."""
    path = path or os.environ.get("POSTCODES_CSV", str(HERE / "australian_postcodes.csv"))
    pts: dict[str, list[tuple[float, float]]] = defaultdict(list)
    if not Path(path).exists():
        return {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            try:
                lat, lon = float(row.get("lat") or row.get("Lat_precise") or ""), \
                    float(row.get("long") or row.get("Long_precise") or "")
            except ValueError:
                continue
            if lat and lon:
                pts[(row.get("postcode") or "").strip().zfill(4)].append((lat, lon))
    return {pc: (sum(a for a, _ in v) / len(v), sum(b for _, b in v) / len(v)) for pc, v in pts.items()}


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def _cell(p: tuple[float, float]) -> tuple[int, int]:
    return int(math.floor(p[0] / 0.25)), int(math.floor(p[1] / 0.25))


# --------------------------------------------------------------------------- #
# grouping
# --------------------------------------------------------------------------- #

def people_of(rows: list[dict]) -> set[str]:
    out = set()
    for r in rows:
        for d in (r.get("directors") or "").split(";"):
            if d.strip():
                out.add(norm_person(d))
        if "sole_trader" in (r.get("name_flags") or "") and not (r.get("directors") or "").strip():
            out.add(norm_person(r.get("licensee", "")))
    return {p for p in out if p}


def same_person(a: str, b: str) -> bool:
    """True when two normalised names are variants of one person: same first and last name, and the
    shorter one's middle names (or initials) appear in order in the longer one's
    ("DAMIAN YOUNG" = "DAMIAN STEPHEN YOUNG" = "DAMIAN S YOUNG")."""
    ta, tb = a.split(), b.split()
    if len(ta) > len(tb):
        ta, tb = tb, ta
    if len(ta) < 2 or ta[0] != tb[0] or ta[-1] != tb[-1]:
        return False
    rest = iter(tb[1:-1])
    return all(any(m.startswith(t) for m in rest) for t in ta[1:-1])


def dedupe_people(people: set[str]) -> set[str]:
    """Collapse name variants of one person to the fullest form, so one director listed two ways
    is not counted as two owners or as two people sharing a surname."""
    out: list[str] = []
    for p in sorted(people, key=lambda s: (-len(s.split()), -len(s), s)):
        if not any(same_person(p, q) for q in out):
            out.append(p)
    return set(out)


def regions_of(rows: list[dict]) -> set[str]:
    out = set()
    for r in rows:
        if r.get("region") and not r["region"].startswith(("Unknown", "Mobile")):
            out.add(r["region"])
        out |= {x for x in (r.get("premises_regions") or "").split("; ") if x}
    return out


def group_operators(licences: list[dict]) -> dict[str, str]:
    """operator_key -> group id (the lexically smallest operator_key in its group)."""
    ops: dict[str, list[dict]] = defaultdict(list)
    for r in licences:
        ops[r["operator_key"]].append(r)
    people = {k: people_of(v) for k, v in ops.items()}
    regions = {k: regions_of(v) for k, v in ops.items()}
    parent = {k: k for k in ops}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    by_person: dict[str, list[str]] = defaultdict(list)
    for k, ps in people.items():
        for p in ps:
            by_person[p].append(k)
    for p, ks in by_person.items():
        if len(ks) < 2:
            continue
        strong = len(p.split()) >= 3
        ks = sorted(ks)
        for i, a in enumerate(ks):
            for b in ks[i + 1:]:
                if strong or regions[a] & regions[b] or len(people[a] & people[b]) >= 2:
                    union(a, b)
    return {k: find(k) for k in ops}


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #

def _sites(rows: list[dict]) -> list[str]:
    seen: dict[str, str] = {}
    for r in rows:
        for part in (r.get("premises") or "").split("; "):
            addr = part.split(" @ ")[-1].strip()
            if addr:
                seen.setdefault(re.sub(r"[^A-Z0-9]", "", addr.upper()), addr)
    return list(seen.values())


def _points(rows: list[dict], pts: dict) -> list[tuple[float, float]]:
    pcs = set()
    for r in rows:
        for part in (r.get("premises") or "").split("; "):
            m = re.findall(r"\b(\d{4})\b", part)
            if m:
                pcs.add(m[-1])
        if not pcs and r.get("postcode"):
            pcs.add(r["postcode"])
    return [pts[p] for p in sorted(pcs) if p in pts]


def family_signal(people: set[str], names: list[str]) -> str:
    surnames = Counter(p.split()[-1] for p in people if len(p.split()) >= 2)
    shared = [s for s, n in surnames.items() if n >= 2 and s not in GENERIC_SURNAMES]
    if shared:
        return f"directors share surname {shared[0].title()}"
    words = set(re.findall(r"[A-Z]+", " ".join(names).upper()))
    hit = [s for s in surnames if len(s) >= 3 and s not in GENERIC_SURNAMES and s in words]
    if hit:
        return f"{hit[0].title()} in the trading name"
    return ""


def score_group(rows: list[dict], *, today: dt.date, nearby: int) -> dict:
    segs = Counter(r.get("segment_rule", "") for r in rows)
    n = max(len(rows), 1)
    service_share = (segs["service"] + UNKNOWN_SERVICE_WEIGHT * segs["unknown"]) / n
    sites = _sites(rows)
    n_sites = len(sites)
    people = dedupe_people(people_of(rows))
    names = sorted({r["licensee"].strip() for r in rows}
                   | {b.strip() for r in rows for b in (r.get("business_names_full") or r.get("business_names") or "").split(";") if b.strip()})
    starts = [d for d in (parse_date(r.get("start_date", "")) for r in rows) if d]
    first = min(starts) if starts else None
    tenure = round((today - first).days / 365.25, 1) if first else None
    fam = family_signal(people, names)
    company = any("company" in (r.get("name_flags") or "") for r in rows)
    has_id = any(r.get("abn") or r.get("acn") for r in rows)
    compliance = any((r.get("public_warnings") or "0") not in ("", "0") or
                     (r.get("disciplinary_actions") or "0") not in ("", "0") for r in rows)

    # fit (45)
    fit = 20 * service_share
    fit += {0: 0, 1: 0, 2: 10}.get(n_sites, 15 if n_sites <= 5 else 12 if n_sites <= 10 else 8)
    fit += min(10.0, 10 * math.log10(1 + nearby) / math.log10(51))
    # succession (35)
    succ = 0.0 if tenure is None else 15 if tenure >= 25 else 10 if tenure >= 15 else 5 if tenure >= 8 else 0
    succ += 5 if not people else 10 if len(people) <= 2 else 5 if len(people) == 3 else 0
    succ += 10 if fam else 0
    # deal-ability (20)
    deal = (10 if company else 0) + (5 if has_id else 0) + (0 if compliance else 5)

    reasons = []
    if any(r.get("segment_rule") == "dealer" or "excl_dealer" in (r.get("name_flags") or "") for r in rows):
        reasons.append("dealer entity in group")
    if any(r.get("franchise_brand") for r in rows):
        reasons.append("franchise: " + next(r["franchise_brand"] for r in rows if r.get("franchise_brand")))
    if segs["mobile"] == len(rows):
        reasons.append("mobile only")
    if service_share < 0.5 and not reasons:
        top = [s for s, _ in segs.most_common() if s not in ("service", "unknown")]
        reasons.append(f"mostly {top[0] if top else 'non-service'}")

    why = []
    if n_sites >= 2:
        why.append(f"{n_sites} sites")
    if first and first <= REGISTER_START:
        why.append("licensed since register began (1981)")
    elif tenure is not None and tenure >= 15:
        why.append(f"licensed {int(tenure)} yrs")
    if fam:
        why.append(fam)
    if people and len(people) <= 2:
        why.append(f"{len(people)} owner{'s' if len(people) > 1 else ''}")
    if nearby >= 20:
        why.append(f"{nearby} service shops within {int(NEARBY_KM)} km")

    return {
        "score": round(fit + succ + deal, 1), "fit": round(fit, 1), "succession": round(succ, 1),
        "dealability": round(deal, 1), "exclusion": "; ".join(reasons), "why": "; ".join(why),
        "n_licences": len(rows), "n_sites": n_sites, "service_share": round(service_share, 2),
        "segments": "; ".join(f"{s} {c}" for s, c in segs.most_common()),
        "first_licensed": first.isoformat() if first else "", "tenure_years": "" if tenure is None else tenure,
        "people": "; ".join(sorted(p.title() for p in people)), "n_people": len(people),
        "family_signal": fam, "_company": company, "_names": names, "_sites": sites,
    }


def load_overrides(path: str | os.PathLike | None) -> list[dict]:
    """data/overrides.csv: match,action,note.  `match` is an ACN, ABN, licence number or exact
    entity name; `action` is exclude (send to tier X) or keep (ignore the automatic exclusion)."""
    if not path or not Path(path).exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f) if (r.get("match") or "").strip() and
                (r.get("action") or "").strip().lower() in ("exclude", "keep")]


def _override_for(t: dict, rows: list[dict], overrides: list[dict]) -> dict | None:
    keys = {x.upper() for r in rows for x in (r.get("acn"), r.get("abn"), r.get("licence_number"),
                                              r.get("licensee", "").strip()) if x}
    for o in overrides:
        if o["match"].strip().upper() in keys:
            return o
    return None


def load_lookups(path: str | os.PathLike | None) -> dict[str, dict]:
    """data/licence_lookup.jsonl: one record per searched entity name (last one wins)."""
    import json
    out: dict[str, dict] = {}
    if not path or not Path(path).exists():
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("query"):
                out[norm_person(rec["query"])] = rec
    return out


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def licences_held(rows: list[dict], lookups: dict[str, dict]) -> tuple[bool, list[dict]]:
    """(were all the group's entities looked up, other licences they hold).  A search result
    counts as the entity's own licence when its ACN or ABN matches, or, if it carries neither,
    when the licensee name matches exactly."""
    names = {norm_person(r["licensee"]) for r in rows}
    acns = {_digits(r.get("acn", "")) for r in rows if r.get("acn")}
    abns = {_digits(r.get("abn", "")) for r in rows if r.get("abn")}
    own = {r["licence_number"] for r in rows}
    checked = bool(names) and all(n in lookups and not lookups[n].get("error") for n in names)
    held: dict[str, dict] = {}
    for n in names:
        for res in (lookups.get(n) or {}).get("results") or []:
            num = res.get("licenceNumber") or ""
            if not num or num in own:
                continue
            acn, abn = _digits(res.get("ACN", "")), _digits(res.get("ABN", ""))
            mine = (acn and acn in acns) or (abn and abn in abns) or \
                   (not acn and not abn and norm_person(res.get("licensee", "")) in names)
            if mine:
                held[num] = res
    return checked, sorted(held.values(), key=lambda r: r.get("licenceNumber", ""))


def lookup_queue(targets: list[dict]) -> list[str]:
    """Entity names to search, most useful first: focus-region tier A, focus-region tier B,
    then the rest of tier A."""
    def focus(t):            # bool from build_targets, "True"/"False" when read back from targets.csv
        return t["in_focus"] is True or str(t["in_focus"]).lower() == "true"

    order = []
    for keep in (lambda t: t["tier"] == "A" and focus(t), lambda t: t["tier"] == "B" and focus(t),
                 lambda t: t["tier"] == "A"):
        for t in targets:
            if keep(t):
                order += [e for e in t["entities"].split("; ") if e]
    seen, out = set(), []
    for e in order:
        if norm_person(e) not in seen:
            seen.add(norm_person(e))
            out.append(e)
    return out


def display_name(rows: list[dict]) -> str:
    """Prefer the most common company name; fall back to the licensee (a person)."""
    companies = Counter(r["licensee"].strip() for r in rows if "company" in (r.get("name_flags") or ""))
    pick = companies or Counter(r["licensee"].strip() for r in rows)
    return pick.most_common(1)[0][0]


def build_targets(licences: list[dict], *, today: dt.date | None = None,
                  points: dict | None = None, overrides: list[dict] | None = None,
                  lookups: dict[str, dict] | None = None) -> list[dict]:
    today = today or dt.date.today()
    overrides = overrides or []
    lookups = lookups or {}
    pts = postcode_points() if points is None else points
    gid = group_operators(licences)
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in licences:
        groups[gid[r["operator_key"]]].append(r)

    gpoints = {g: _points(rows, pts) for g, rows in groups.items()}
    # service-shop pool for the density measure: groups whose licences are mostly service
    pool = {g for g, rows in groups.items()
            if sum(r.get("segment_rule") == "service" for r in rows) * 2 > len(rows)}
    grid: dict[tuple[int, int], list[tuple[str, tuple[float, float]]]] = defaultdict(list)
    for g in pool:
        for p in gpoints[g]:
            grid[_cell(p)].append((g, p))

    def neighbours(g: str) -> set[str]:
        out = set()
        for p in gpoints[g]:
            cx, cy = _cell(p)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for h, q in grid.get((cx + dx, cy + dy), ()):
                        if h != g and km(p, q) <= NEARBY_KM:
                            out.add(h)
        return out

    near = {g: neighbours(g) for g in groups}
    out = []
    for g, rows in groups.items():
        s = score_group(rows, today=today, nearby=len(near[g]))
        ops = sorted({r["operator_key"] for r in rows})
        checked, held = licences_held(rows, lookups)
        current = [h for h in held if str(h.get("status", "")).lower() in ("current", "")]
        dealer = [h for h in current if is_dealer_licence(h)]
        s["dealer_check"] = ("dealer licence" if dealer else "clear") if checked else \
            ("dealer licence" if dealer else "not checked")
        # motor licences only: a sole trader's name also matches their white card, security licence...
        motor = [h for h in held if str(h.get("licenceGroup") or "Motor") == "Motor" or is_dealer_licence(h)]
        s["other_licences"] = "; ".join(f"{h.get('licenceTypeFriendly') or h.get('licenceType', '')} "
                                        f"{h.get('licenceNumber', '')}"
                                        f"{'' if h in current else ' (' + str(h.get('status', '')) + ')'}"
                                        for h in motor)
        if dealer:
            s["exclusion"] = "; ".join(x for x in (s["exclusion"], "holds a dealer licence " +
                                                   ", ".join(h["licenceNumber"] for h in dealer)) if x)
        dists = [km(p, FOCUS_POINT) for p in gpoints[g]]
        s["km_to_focus"] = round(min(dists)) if dists else ""
        s["in_focus"] = bool(dists) and min(dists) <= FOCUS_KM
        o = _override_for(s, rows, overrides)
        if o and o["action"].strip().lower() == "exclude":
            s["exclusion"] = f"override: {(o.get('note') or 'excluded by hand').strip()}"
        elif o:
            s["exclusion"] = ""
            s["why"] = "; ".join(x for x in (s["why"], f"kept by hand: {(o.get('note') or '').strip()}") if x)
        s.update({
            "group_id": g,
            "name": display_name(rows),
            "n_entities": len(ops),
            "regions": "; ".join(sorted(regions_of(rows))) or
                       ("Mobile (no fixed premises)" if any(r.get("region", "").startswith("Mobile") for r in rows) else ""),
            "nearby_service_operators": len(near[g]),
            "entities": "; ".join(sorted({r["licensee"].strip() for r in rows})),
            "abns": "; ".join(sorted({r["abn"] for r in rows if r.get("abn")})),
            "acns": "; ".join(sorted({r["acn"] for r in rows if r.get("acn")})),
            "licence_numbers": "; ".join(sorted(r["licence_number"] for r in rows)),
            "premises": "; ".join(s["_sites"]),
        })
        out.append(s)

    # tiers
    a_groups = {t["group_id"] for t in out if not t["exclusion"] and t["n_sites"] >= 2}
    for t in out:
        if t["exclusion"]:
            t["tier"] = "X"
        elif t["group_id"] in a_groups:
            t["tier"] = "A"
        elif t["_company"] and t["n_sites"] <= 1 and (near[t["group_id"]] & a_groups or t["in_focus"]):
            t["tier"] = "B"          # near a platform candidate, or anywhere in the focus region
        else:
            t["tier"] = "C"
    order = {"A": 0, "B": 1, "C": 2, "X": 3}
    out.sort(key=lambda t: (order[t["tier"]], not t["in_focus"], -t["score"], -t["n_sites"], t["name"].upper()))
    for i, t in enumerate(out, 1):
        t["rank"] = i
    return out


def summary_lines(targets: list[dict], top: int = 25) -> list[str]:
    tiers = Counter(t["tier"] for t in targets)
    excl = Counter(t["exclusion"].split(";")[0].split(":")[0] for t in targets if t["tier"] == "X")
    multi = sum(1 for t in targets if t["n_entities"] > 1)
    lines = ["## Targets (director-grouped, scored)", "",
             f"{len(targets)} operator groups; {multi} combine more than one company through shared directors.", "",
             "| tier | meaning | groups |", "|---|---|---|",
             f"| A | platform candidates: service, 2+ sites | {tiers['A']} |",
             f"| B | bolt-ons: single-site service company within {int(NEARBY_KM)} km of a tier-A group | {tiers['B']} |",
             f"| C | other service operators | {tiers['C']} |",
             f"| X | excluded | {tiers['X']} |", "",
             "Excluded because: " + ", ".join(f"{k} {v}" for k, v in excl.most_common()) + ".", "",
             f"### Top {top} tier A", "",
             "| rank | group | score | sites | companies | first licensed | why |", "|---|---|---|---|---|---|---|"]
    for t in [t for t in targets if t["tier"] == "A"][:top]:
        lines.append(f"| {t['rank']} | {t['name']} | {t['score']} | {t['n_sites']} | {t['n_entities']} | "
                     f"{t['first_licensed'][:4]} | {t['why']} |")
    focus = [t for t in targets if t["in_focus"]]
    chk = Counter(t["dealer_check"] for t in targets if t["tier"] in ("A", "B"))
    lines += ["", f"### Within about an hour of {FOCUS_NAME} ({int(FOCUS_KM)} km straight line)", "",
              f"Tier A {sum(t['tier'] == 'A' for t in focus)}, tier B {sum(t['tier'] == 'B' for t in focus)}, "
              f"tier C {sum(t['tier'] == 'C' for t in focus)}. These rank first within each tier.", "",
              f"Dealer-licence check, tiers A and B: clear {chk['clear']}, not checked {chk['not checked']}. "
              f"Groups found holding a dealer licence are in tier X.", "",
              "| rank | tier | group | score | sites | km | why |", "|---|---|---|---|---|---|---|"]
    for t in [t for t in focus if t["tier"] in ("A", "B")][:top]:
        lines.append(f"| {t['rank']} | {t['tier']} | {t['name']} | {t['score']} | {t['n_sites']} | "
                     f"{t['km_to_focus']} | {t['why']} |")
    return lines + [""]
