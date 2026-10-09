#!/usr/bin/env python3
"""
pipeline.py — enrich the NSW Motor Vehicle Repairer register with the `details`
endpoint of the api.nsw "Motor API", then apply deterministic (no-AI) rules to
find service-focused repairers and estimate shops per operator.

Subcommands
  prioritise      data/register_summary.csv  -> data/queue.csv
  fetch-details   data/queue.csv             -> data/details.jsonl (adaptive rate)
  fetch-abr       data/details.jsonl         -> data/abr.jsonl     (needs ABR_GUID)
  build           everything above           -> data/out/*.csv, data/out/summary.md
  commit          git add data/ ; commit ; pull --rebase ; push    (Actions only)
  all             fetch-details, fetch-abr, build

Environment (see .env.example)
  NSW_API_KEY / NSW_API_SECRET   api.nsw credentials (required for fetch-details)
  ABR_GUID                       ABN Lookup GUID (optional; fetch-abr skipped if absent)
  RUN_SECONDS=3000               wall-clock budget for the fetch stages
  RUN_DEADLINE                   unix epoch; overrides RUN_SECONDS when set
  MONTHLY_CALL_BUDGET=0          0 = no artificial cap, otherwise stop at N calls/month
  DRY_RUN=1                      no API calls, no git pushes
  RESET_QUOTA=1                  clear quota_exhausted_until first (after a quota increase)
  DATA_DIR=data                  where state lives

Stdlib only, Python 3.10+.
"""
from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from nsw_repairers import (  # noqa: E402  (reuse the existing client pieces)
    API_HOST, DETAILS_URL, TOKEN_URL, build_nsw_postcodes, http_get, load_dotenv,
    load_postcode_table,
)

# =========================================================================== #
# Patterns — every rule the pipeline uses lives in this dict.
# Each value is a list of regex fragments, matched case-insensitively against the
# upper-cased name text.  Word boundaries are added where the fragment is a plain
# word; fragments ending in a letter match prefixes (SERVIC -> services, servicing).
# =========================================================================== #
PATTERNS: dict[str, list[str]] = {
    # ---- entity shape ---------------------------------------------------- #
    "company": [r"\bPTY\b", r"\bLTD\b", r"\bLIMITED\b", r"\bTRUST\b", r"\bTRUSTEE\b",
                r"\bINC\b", r"\bINCORPORATED\b", r"\bCO-?OP(ERATIVE)?\b", r"\bCORP(ORATION)?\b",
                r"\bP/L\b", r"\bNOMINEES\b", r"\bHOLDINGS\b", r"\bENTERPRISES\b",
                r"\bINVESTMENTS\b", r"\bGROUP\b", r"\bPARTNERSHIP\b"],
    # ---- service-shop signal ---------------------------------------------- #
    "service": [r"MECHANIC", r"SERVIC", r"\bREPAIR", r"\bAUTO\b", r"\bAUTOS\b", r"AUTOMOTIVE",
                r"AUTOMOTIV", r"WORKSHOP", r"\bGARAGE", r"\bLUBE\b", r"BRAKE", r"CLUTCH",
                r"TRANSMISSION", r"GEARBOX", r"EXHAUST", r"SUSPENSION", r"\bTUNE\b", r"TUNING",
                r"CAR CARE", r"PINK SLIP", r"\bMOTORS\b", r"MOTOR WORKS", r"\bMOTOR REPAIRS?\b",
                r"\bAUTOCARE\b", r"AUTO CARE", r"\bAUTOTECH\b", r"\bAUTO TECH\b", r"\bRADIATOR",
                r"STEERING", r"DRIVELINE", r"DIFF\b", r"\bLPG\b", r"\bEV\b", r"HYBRID",
                r"ROADWORTHY", r"REGO\b", r"INSPECTION", r"\bMECH\b"],
    # ---- exclusion families (first match wins, in this order) ------------- #
    "excl_dealer": [r"\bDEALER", r"\bSALES\b", r"MOTOR GROUP", r"\bPRESTIGE MOTORS?\b",
                    r"\bTOYOTA\b", r"\bFORD\b", r"\bHOLDEN\b", r"\bHYUNDAI\b", r"\bKIA\b",
                    r"\bMAZDA\b", r"\bNISSAN\b", r"\bHONDA\b", r"\bSUBARU\b", r"\bMITSUBISHI\b",
                    r"\bVOLKSWAGEN\b", r"\bVW\b", r"\bAUDI\b", r"\bBMW\b", r"\bMERCEDES",
                    r"\bLEXUS\b", r"\bISUZU\b", r"\bSUZUKI\b", r"\bVOLVO\b", r"\bJEEP\b",
                    r"\bLAND ?ROVER\b", r"\bJAGUAR\b", r"\bPORSCHE\b", r"\bTESLA\b", r"\bMG\b",
                    r"\bHINO\b", r"\bSCANIA\b", r"\bKENWORTH\b", r"\bMACK\b", r"\bIVECO\b",
                    r"\bRENAULT\b", r"\bPEUGEOT\b", r"\bCITROEN\b", r"\bSKODA\b", r"\bFIAT\b",
                    r"\bALFA ROMEO\b", r"\bCHRYSLER\b", r"\bDODGE\b", r"\bLDV\b", r"\bGWM\b",
                    r"\bHAVAL\b", r"\bCHERY\b", r"\bBYD\b", r"\bFERRARI\b", r"\bLAMBORGHINI\b",
                    r"\bMASERATI\b", r"\bBENTLEY\b", r"\bROLLS[- ]ROYCE\b", r"\bASTON MARTIN\b",
                    r"\bMCLAREN\b", r"\bSSANGYONG\b", r"\bDAF\b", r"\bFUSO\b", r"\bUD TRUCKS\b",
                    r"\bAUTOMOTIVE RETAIL\b", r"\bAUTOMOTIVE GROUP\b", r"\bAUTOPOOL\b",
                    r"\bAUTO GROUP\b", r"\bMOTORS GROUP\b", r"\bAUTOMOBILES\b", r"\bMOTOR COMPANY\b",
                    r"\bCAR CITY\b"],
    "excl_body": [r"\bBODY\b", r"SMASH", r"PANEL", r"\bPAINT", r"\bDENT", r"\bHAIL\b",
                  r"COLLISION", r"CRASH", r"\bSPRAY", r"BODYWORK", r"\bACCIDENT", r"SMART REPAIR"],
    "excl_tyres": [r"\bTYRE", r"\bTIRE", r"\bWHEEL", r"\bRIMS?\b", r"\bMAG\b"],
    "excl_heavy": [r"\bTRUCK", r"\bDIESEL", r"\bHEAVY", r"\bPLANT\b", r"\bTRAILER", r"\bEARTHMOV",
                   r"\bMACHINERY", r"\bTRACTOR", r"\bAGRICULTUR", r"\bFLEET\b", r"BUS\b", r"\bBUSES\b",
                   r"\bCOACH", r"\bFORKLIFT", r"\bHAULAGE", r"\bFARM\b", r"\bLOGISTICS\b",
                   r"\bCRANE"],
    "excl_specialty_vehicle": [r"MOTORCYCLE", r"MOTOR ?CYCLE", r"\bBIKE", r"\bMARINE", r"\bBOAT",
                               r"CARAVAN", r"\bRV\b", r"\bCAMPER", r"\bMOTORHOME", r"\bOUTBOARD",
                               r"\bJET ?SKI"],
    "excl_electrical": [r"ELECTRIC", r"AIR ?CON", r"A/C\b", r"WINDSCREEN", r"WINDSHIELD",
                        r"\bGLASS\b", r"\bAUDIO", r"\bBATTER", r"\bSTEREO", r"\bALARM",
                        r"\bTINT", r"\bLIGHTING\b", r"\bVOLT", r"\bGLAZ"],
    "excl_parts_towing": [r"\bTOW", r"WRECK", r"\bPARTS\b", r"\bSPARES?\b", r"DISMANTL",
                          r"\bSALVAGE", r"\bRECYCL", r"\bRETAIL", r"\bSUPERCHEAP", r"\bREPCO\b",
                          r"\bAUTOBARN\b", r"\bBURSON", r"\bAUTO ONE\b", r"\bWHOLESAL"],
    "excl_mobile": [r"\bMOBILE\b", r"\bON[- ]SITE\b", r"\bCOME TO YOU\b", r"\bROADSIDE"],
    "excl_performance": [r"PERFORMANCE", r"\bCUSTOM", r"\b4 ?WD\b", r"\b4X4\b", r"RACING",
                         r"\bRACE\b", r"MOTORSPORT", r"\bDYNO", r"\bOFF ?ROAD", r"\bTURBO",
                         r"\bRESTORATION", r"\bCLASSIC", r"\bVINTAGE", r"\bHOT ?ROD", r"\bDRAG\b",
                         r"\bFABRICAT", r"\bENGINEERING\b", r"\bWELDING"],
    "excl_other": [r"DETAIL", r"\bTRANSPORT", r"\bHIRE\b", r"\bRENTAL", r"\bCOUNCIL\b",
                   r"\bSHIRE\b", r"\bDEPARTMENT\b", r"\bGOVERNMENT\b", r"\bNSW POLICE\b",
                   r"\bAMBULANCE\b", r"\bFIRE (AND|&) RESCUE\b", r"\bTRANSIT\b", r"\bRAIL",
                   r"\bCAR WASH\b", r"\bCARWASH\b", r"\bUNIVERSITY\b", r"\bTAFE\b", r"\bSCHOOL\b",
                   r"\bCOLLEGE\b", r"\bTRAINING\b", r"\bFREIGHT", r"\bCOURIER", r"\bTAXI",
                   r"\bLIMOUSINE", r"\bFUNERAL", r"\bMINING\b", r"\bMINES?\b", r"\bPETROL",
                   r"\bFUEL", r"\bCARPARK", r"\bCAR PARK"],
}

EXCLUSION_FAMILIES = [k for k in PATTERNS if k.startswith("excl_")]

# Exclusion family -> segment label used when no licence classes are available.
FAMILY_SEGMENT = {
    "excl_dealer": "dealer",
    "excl_body": "body",
    "excl_tyres": "specialist",
    "excl_heavy": "specialist",
    "excl_specialty_vehicle": "specialist",
    "excl_electrical": "specialist",
    "excl_parts_towing": "other",
    "excl_mobile": "mobile",
    "excl_performance": "specialist",
    "excl_other": "other",
}

# Licence-class keywords -> segment family.  "service" wins, then body, then specialist.
CLASS_SEGMENTS: dict[str, list[str]] = {
    "service": [r"MECHANIC", r"BRAKE", r"TRANSMISSION", r"EXHAUST", r"STEERING", r"SUSPENSION",
                r"WHEEL ALIGN", r"RADIATOR", r"UNDERBODY", r"\bLPG\b", r"\bCNG\b", r"GAS",
                r"ENGINE", r"DRIVE ?LINE"],
    "body": [r"\bBODY", r"PANEL", r"PAINT", r"TRIM", r"DENT"],
    "specialist": [r"ELECTRIC", r"TYRE", r"TIRE", r"GLAZ", r"WINDSCREEN", r"MOTOR ?CYCLE",
                   r"TRAILER", r"CARAVAN", r"DETAIL", r"AIR ?CON", r"HEAVY", r"RESTOR"],
}

# The register's licenceClasses entry is usually just the generic licence type
# ("Motor Vehicle Repairer Licence"); such entries carry no segment signal and are ignored.
GENERIC_CLASS_RE = re.compile(r"^MOTOR VEHICLE (REPAIRER'?S?|DEALER'?S?) LICEN[CS]E$")
# Licence conditions that pin the segment (checked before name flags).
CONDITION_SEGMENTS: dict[str, str] = {
    r"MOBILE WORKSHOP|MOBILE BUSINESS|FROM A MOBILE": "mobile",
}

# Franchise / chain brands (label -> regex).
FRANCHISE_BRANDS: dict[str, str] = {
    "Ultra Tune": r"\bULTRA ?TUNE\b",
    "Midas": r"\bMIDAS\b",
    "mycar": r"\bMYCAR\b|\bMY CAR\b",
    "Kmart Tyre & Auto": r"\bKMART\b|\bK ?MART TYRE\b|\bKTAS\b",
    "Bosch Car Service": r"\bBOSCH\b",
    "Repco Authorised": r"\bREPCO\b",
    "JAX": r"\bJAX\b",
    "Tyrepower": r"\bTYRE ?POWER\b",
    "Bridgestone": r"\bBRIDGESTONE\b",
    "Goodyear": r"\bGOODYEAR\b",
    "Beaurepaires": r"\bBEAUREPAIRES\b",
    "Lube Mobile": r"\bLUBE ?MOBILE\b",
    "Pedders": r"\bPEDDERS\b",
    "Natrad": r"\bNATRAD\b",
    "ABS Auto": r"\bABS AUTO\b",
    "Autobarn": r"\bAUTOBARN\b",
    "Supercheap": r"\bSUPERCHEAP\b",
    "Bob Jane": r"\bBOB JANE\b",
    "Dunlop Super Dealer": r"\bDUNLOP\b",
    "Michelin": r"\bMICHELIN\b",
    "Carmate": r"\bCARMATE\b",
    "Auto Masters": r"\bAUTO ?MASTERS\b",
    "Snap-on": r"\bSNAP[- ]?ON\b",
    "Windscreens O'Brien": r"\bO'?BRIENS? (GLASS|WINDSCREEN|AUTO)",
    "Novus": r"\bNOVUS\b",
    "Instant Windscreens": r"\bINSTANT WINDSCREEN",
    "Marshall Batteries": r"\bMARSHALL BATTER",
    "Battery World": r"\bBATTERY WORLD\b",
    "NRMA": r"\bNRMA\b",
    "RACV": r"\bRACV\b",
    "Pit Stop": r"\bPIT ?STOP\b",
    "Tyres & More": r"\bTYRES (&|AND) MORE\b",
    "Tyreright": r"\bTYRERIGHT\b",
    "Kwik Fit": r"\bKWIK ?FIT\b",
    "Auto One": r"\bAUTO ONE\b",
}

_COMPILED: dict[str, list[re.Pattern]] = {k: [re.compile(p) for p in v] for k, v in PATTERNS.items()}
_COMPILED_CLASSES = {k: [re.compile(p) for p in v] for k, v in CLASS_SEGMENTS.items()}
_COMPILED_BRANDS = {k: re.compile(v) for k, v in FRANCHISE_BRANDS.items()}
_COMPILED_CONDITIONS = {re.compile(k): v for k, v in CONDITION_SEGMENTS.items()}

# =========================================================================== #
# Paths / small helpers
# =========================================================================== #


def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", HERE / "data"))


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(ts: dt.datetime | None = None) -> str:
    return (ts or now_utc()).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        try:
            return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None


def first_of_next_month(ts: dt.datetime | None = None) -> dt.datetime:
    ts = ts or now_utc()
    y, m = (ts.year + 1, 1) if ts.month == 12 else (ts.year, ts.month + 1)
    return dt.datetime(y, m, 1, tzinfo=dt.timezone.utc)


def env_int(name: str, default: int) -> int:
    v = os.environ.get(name, "")
    try:
        return int(v) if v.strip() else default
    except ValueError:
        return default


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def in_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS", "").lower() == "true"


def read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8") or "null") or default
    except json.JSONDecodeError:
        return default


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_jsonl(path: Path, key: str) -> dict[str, dict]:
    """Read an append-only JSONL file into {key: record}; the LAST record per key wins,
    so duplicates (from retries or rebases) are harmless."""
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn last line from a killed run
            k = rec.get(key)
            if k:
                out[str(k)] = rec
    return out


def append_jsonl(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, separators=(",", ":"), ensure_ascii=False) + "\n")
        f.flush()


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in fields})


def log(msg: str) -> None:
    print(f"[{iso()}] {msg}", flush=True)


# =========================================================================== #
# Name classification
# =========================================================================== #


def norm_name(s: str) -> str:
    s = (s or "").upper().replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    s = re.sub(r"\b(THE|PTY|LTD|LIMITED|P L|INC|INCORPORATED|CO|COMPANY)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def name_text(row: dict) -> str:
    """All the name-ish text we have for a licence, upper-cased."""
    parts = [row.get("licensee", ""), row.get("licence_name", ""), row.get("business_names", ""),
             row.get("business_names_full", ""), row.get("premises_names", "")]
    return " | ".join(p for p in parts if p and p.strip().lower() != "printed").upper()


def is_company(licensee: str) -> bool:
    u = (licensee or "").upper()
    return any(p.search(u) for p in _COMPILED["company"])


def classify_name(row: dict) -> dict:
    """Return {"company": bool, "service": bool, "exclusions": [family...], "flags": "a;b;c"}."""
    text = name_text(row)
    company = is_company(row.get("licensee", ""))
    service = any(p.search(text) for p in _COMPILED["service"])
    exclusions = [fam for fam in EXCLUSION_FAMILIES if any(p.search(text) for p in _COMPILED[fam])]
    flags = (["company"] if company else ["sole_trader"]) + (["service"] if service else []) + exclusions
    if not service and not exclusions:
        flags.append("uninformative")
    return {"company": company, "service": service, "exclusions": exclusions, "flags": ";".join(flags)}


def tier_for(cls: dict) -> int:
    """1 company+service, 2 company uninformative, 3 sole trader+service,
    4 any exclusion family, 5 sole trader uninformative."""
    if cls["exclusions"]:
        return 4
    if cls["company"]:
        return 1 if cls["service"] else 2
    return 3 if cls["service"] else 5


def segment_from_classes(classes: list[str]) -> str:
    specific = [c for c in classes if c and not GENERIC_CLASS_RE.match(c.strip().upper())]
    text = " | ".join(specific).upper()
    if not text.strip():
        return ""
    for seg in ("service", "body", "specialist"):
        if any(p.search(text) for p in _COMPILED_CLASSES[seg]):
            return seg
    return "other"


def segment_from_conditions(conditions: list[str]) -> str:
    text = " | ".join(conditions).upper()
    for pat, seg in _COMPILED_CONDITIONS.items():
        if pat.search(text):
            return seg
    return ""


def segment_from_flags(cls: dict) -> str:
    if cls["exclusions"]:
        return FAMILY_SEGMENT.get(cls["exclusions"][0], "other")
    if cls["service"]:
        return "service"
    return "unknown"


def franchise_brand(text: str) -> str:
    u = (text or "").upper()
    for label, pat in _COMPILED_BRANDS.items():
        if pat.search(u):
            return label
    return ""


# =========================================================================== #
# Budget / status files
# =========================================================================== #

DEFAULT_RPM = 60.0
MIN_RPM = 2.0
MAX_RPM = 600.0
MAX_CONSECUTIVE_ERRORS = 10   # stop the run rather than burn the queue on a systemic 4xx


def load_budget(path: Path) -> dict:
    b = read_json(path, {})
    month = now_utc().strftime("%Y-%m")
    if b.get("month") != month:
        b["month"] = month
        b["calls_used_this_month"] = 0
    b.setdefault("last_rate", DEFAULT_RPM)
    b.setdefault("calls_used_this_month", 0)
    b.setdefault("quota_exhausted_until", None)
    return b


def quota_exhausted_now(budget: dict, ts: dt.datetime | None = None) -> bool:
    until = parse_iso(budget.get("quota_exhausted_until"))
    return bool(until and until > (ts or now_utc()))


# ---- credentials: one api.nsw account or several, rotated as each one's quota runs out ---- #

def key_id(key: str) -> str:
    """Short, non-secret handle for a key, used in budget.json and logs."""
    return f"{key[:4]}…{key[-4:]}" if len(key) > 8 else key


def parse_credentials(env: dict | None = None) -> list[tuple[str, str]]:
    """NSW_API_KEY/NSW_API_SECRET (one pair) plus NSW_API_KEYS: 'key:secret' pairs separated
    by newlines or ';' (also accepts ',' or whitespace between key and secret).  Order kept,
    duplicates dropped."""
    env = os.environ if env is None else env
    creds: list[tuple[str, str]] = []
    k, s = (env.get("NSW_API_KEY") or "").strip(), (env.get("NSW_API_SECRET") or "").strip()
    if k and s:
        creds.append((k, s))
    for chunk in re.split(r"[\n;]+", env.get("NSW_API_KEYS") or ""):
        chunk = chunk.strip()
        if not chunk or chunk.startswith("#"):
            continue
        parts = re.split(r"[:,\s]+", chunk, maxsplit=1)
        if len(parts) == 2 and all(parts):
            creds.append((parts[0].strip(), parts[1].strip()))
    seen, out = set(), []
    for c in creds:
        if c[0] not in seen:
            seen.add(c[0])
            out.append(c)
    return out


def keys_exhausted(budget: dict, creds: list[tuple[str, str]], ts: dt.datetime | None = None) -> bool:
    """True when nothing can be fetched: every configured key is known to be spent.
    A key id that budget.json has never seen counts as fresh, so adding an account resumes
    fetching on the next run.  Legacy state (no per-key info, one key) falls back to the
    global quota_exhausted_until."""
    if not quota_exhausted_now(budget, ts):
        return False
    known = {kid: st for kid, st in (budget.get("keys") or {}).items() if st.get("quota_exhausted_until")}
    if not known:
        return len(creds) <= 1
    return all(quota_exhausted_now(known.get(key_id(k), {}), ts) for k, _ in creds)


# =========================================================================== #
# Adaptive API client
# =========================================================================== #


class QuotaExhausted(Exception):
    pass


class AdaptiveRate:
    """Start at `rpm`; +20% after every 100 consecutive successes; halve on a 429."""

    def __init__(self, rpm: float = DEFAULT_RPM, step_every: int = 100):
        self.rpm = max(MIN_RPM, min(MAX_RPM, float(rpm or DEFAULT_RPM)))
        self.step_every = step_every
        self.consecutive_ok = 0

    @property
    def interval(self) -> float:
        return 60.0 / self.rpm

    def on_success(self) -> None:
        self.consecutive_ok += 1
        if self.consecutive_ok % self.step_every == 0:
            self.rpm = min(MAX_RPM, round(self.rpm * 1.2, 2))

    def on_throttle(self) -> None:
        self.consecutive_ok = 0
        self.rpm = max(MIN_RPM, round(self.rpm / 2.0, 2))


QUOTA_BODY_RE = re.compile(r"quota|limit\s+exceeded|exceeded\s+(the\s+)?(call|request|monthly)", re.I)
THROTTLE_HOLDS = (60, 120, 180)  # seconds; 3 backoffs totalling >= 5 min => month is spent


class AdaptiveClient:
    """api.nsw Motor API client with adaptive rate and quota-exhaustion detection.

    `http`, `sleep` and `clock` are injectable so tests can run without a network.
    """

    def __init__(self, creds: list[tuple[str, str]] | str, rate: AdaptiveRate | str, *args,
                 key_state: dict | None = None,
                 http=http_get, sleep=time.sleep, clock=time.time, verbose=True):
        if isinstance(creds, str):            # AdaptiveClient(key, secret, rate, ...)
            creds, rate = [(creds, rate)], args[0]
        self.creds: list[tuple[str, str]] = list(creds)
        if not self.creds:
            raise ValueError("no credentials")
        self.rate = rate
        self.key_state = key_state if key_state is not None else {}   # key_id -> {...}, persisted
        self.http, self.sleep, self.clock = http, sleep, clock
        self.verbose = verbose
        self.calls = 0              # every HTTP request made (incl. token + throttled ones)
        self.throttles = 0          # number of 429s seen
        self.rotations = 0
        self._token = None
        self._token_time = 0.0
        self._last = 0.0
        self.hold_until = 0.0
        self._auth_failed: set[int] = set()
        self.idx = self._first_usable()

    # -- credentials ------------------------------------------------------- #
    @property
    def key(self) -> str:
        return self.creds[self.idx][0]

    @property
    def secret(self) -> str:
        return self.creds[self.idx][1]

    def _state(self, i: int) -> dict:
        return self.key_state.setdefault(key_id(self.creds[i][0]), {})

    def _usable(self, i: int) -> bool:
        st = self.key_state.get(key_id(self.creds[i][0]), {})
        return i not in self._auth_failed and not quota_exhausted_now(st)

    def _first_usable(self) -> int:
        for i in range(len(self.creds)):
            if self._usable(i):
                return i
        raise QuotaExhausted(f"all {len(self.creds)} keys are spent for this month")

    def _rotate(self, reason: str, detail: str = "") -> None:
        """Mark the current key spent (or broken) and move to the next usable one."""
        st = self._state(self.idx)
        if reason == "quota":
            st["quota_exhausted_until"] = iso(first_of_next_month())   # wall clock, not the rate clock
            st["quota_reason"] = detail[:200]
        else:
            self._auth_failed.add(self.idx)
            st["auth_failed_at"] = iso()
        if self.verbose:
            log(f"key {key_id(self.key)} {reason}: {detail[:120]}")
        self._token = None
        self.rotations += 1
        nxt = next((i for i in range(len(self.creds)) if self._usable(i)), None)
        if nxt is None:
            spent = sum(1 for i in range(len(self.creds)) if quota_exhausted_now(self._state(i)))
            if spent:
                raise QuotaExhausted(f"all {len(self.creds)} keys exhausted "
                                     f"({spent} quota, {len(self._auth_failed)} auth failures)")
            raise RuntimeError(f"no working credentials ({len(self._auth_failed)} auth failures)")
        self.idx = nxt
        if self.verbose:
            log(f"-> switching to key {key_id(self.key)} ({self.idx + 1}/{len(self.creds)})")

    # -- plumbing ---------------------------------------------------------- #
    def _wait(self) -> None:
        now = self.clock()
        wait = max(self.hold_until - now, self.rate.interval - (now - self._last))
        if wait > 0:
            self.sleep(wait)
        self._last = self.clock()

    def token(self) -> str:
        while True:
            if self._token and self.clock() - self._token_time < 11 * 3600:
                return self._token
            basic = base64.b64encode(f"{self.key}:{self.secret}".encode()).decode()
            self._wait()
            r = self.http(TOKEN_URL, params={"grant_type": "client_credentials"},
                          headers={"Authorization": f"Basic {basic}"}, timeout=30)
            self.calls += 1
            self._state(self.idx)["calls"] = self._state(self.idx).get("calls", 0) + 1
            if r.status_code == 200:
                self._token = r.json()["access_token"]
                self._token_time = self.clock()
                return self._token
            if r.status_code >= 400 and QUOTA_BODY_RE.search(r.text or ""):
                self._rotate("quota", f"HTTP {r.status_code} on token endpoint: {r.text}")
                continue
            if r.status_code == 429:
                raise RuntimeError("429 on token endpoint")
            self._rotate("auth", f"HTTP {r.status_code}: {r.text}")

    def get(self, url: str, params: dict) -> tuple[int, object]:
        """Return (status, parsed_body).  Raises QuotaExhausted when the month is spent.
        429 -> halve rate, hold, retry (60s, 120s, 180s); a 4th 429 means quota is gone.
        5xx -> retry up to 3 times.  Other statuses are returned to the caller."""
        throttle_streak = 0
        server_errors = 0
        while True:
            self._wait()
            try:
                r = self.http(url, params=params,
                              headers={"Authorization": f"Bearer {self.token()}", "apikey": self.key},
                              timeout=60)
            except OSError as exc:  # network blip
                server_errors += 1
                if server_errors > 3:
                    return 599, {"error": str(exc)}
                self.sleep(10 * server_errors)
                continue
            self.calls += 1
            self._state(self.idx)["calls"] = self._state(self.idx).get("calls", 0) + 1
            if r.status_code == 200:
                self.rate.on_success()
                try:
                    return 200, r.json()
                except ValueError:
                    return 200, {}
            if r.status_code >= 400 and QUOTA_BODY_RE.search(r.text or ""):
                # api.nsw signals a spent monthly quota with HTTP 408 (not 429) and the body
                # "Quota limit of 2500 per 1 month exceeded." -- treat any status the same way:
                # mark this key spent, switch to the next one and retry the same request.
                self._rotate("quota", f"HTTP {r.status_code}: {r.text or ''}")
                throttle_streak = 0
                continue
            if r.status_code == 401:
                self._token = None
                server_errors += 1
                if server_errors > 3:
                    return 401, {}
                continue
            if r.status_code == 429:
                self.throttles += 1
                body = r.text or ""
                if throttle_streak >= len(THROTTLE_HOLDS):
                    raise QuotaExhausted(
                        f"429 persisted through {throttle_streak} backoffs "
                        f"({sum(THROTTLE_HOLDS)}s): {body[:200]}")
                hold = THROTTLE_HOLDS[throttle_streak]
                throttle_streak += 1
                self.rate.on_throttle()
                self.hold_until = self.clock() + hold
                if self.verbose:
                    log(f"429 -> rate now {self.rate.rpm:.1f}/min, holding {hold}s")
                continue
            if r.status_code >= 500:
                server_errors += 1
                if server_errors > 3:
                    return r.status_code, {}
                self.sleep(15 * server_errors)
                continue
            try:
                return r.status_code, r.json()
            except ValueError:
                return r.status_code, {"error": (r.text or "")[:300]}

    def details(self, licence_id: str) -> tuple[int, object]:
        return self.get(DETAILS_URL, {"licenceid": licence_id})


# =========================================================================== #
# git checkpointing
# =========================================================================== #


def git(*args: str, check: bool = True, cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ,
               GIT_AUTHOR_NAME="github-actions[bot]",
               GIT_AUTHOR_EMAIL="41898282+github-actions[bot]@users.noreply.github.com",
               GIT_COMMITTER_NAME="github-actions[bot]",
               GIT_COMMITTER_EMAIL="41898282+github-actions[bot]@users.noreply.github.com")
    return subprocess.run(["git", *args], cwd=str(cwd or HERE), env=env, check=check,
                          capture_output=True, text=True)


def commit_and_push(message: str, *, push: bool = True) -> bool:
    """Commit data/ as github-actions[bot]; pull --rebase then push with retries.
    Returns True if a commit was made."""
    rel = os.path.relpath(data_dir(), HERE)
    git("add", "-A", "--", rel)
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        log("nothing to commit")
        return False
    git("commit", "-q", "-m", message)
    if not push:
        return True
    branch = os.environ.get("GITHUB_REF_NAME") or git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    delay = 2
    for attempt in range(5):
        pull = git("pull", "--rebase", "--autostash", "origin", branch, check=False)
        if pull.returncode != 0:
            log(f"pull --rebase failed: {pull.stderr.strip()[:300]}")
            git("rebase", "--abort", check=False)
        res = git("push", "-u", "origin", f"HEAD:{branch}", check=False)
        if res.returncode == 0:
            log(f"pushed to {branch}")
            return True
        log(f"push failed (attempt {attempt + 1}): {res.stderr.strip()[:300]}")
        time.sleep(delay)
        delay *= 2
    log("giving up on push; the commit stays local and the next run will retry")
    return True


def should_push() -> bool:
    return in_actions() and not env_flag("DRY_RUN")


# =========================================================================== #
# prioritise
# =========================================================================== #

QUEUE_FIELDS = ["rank", "licence_id", "licence_number", "licensee", "tier", "name_flags",
                "licensee_licences"]


def prioritise(seed_rows: list[dict]) -> list[dict]:
    counts = Counter(norm_name(r["licensee"]) for r in seed_rows)
    out = []
    for r in seed_rows:
        cls = classify_name(r)
        out.append({
            "licence_id": r["licence_id"],
            "licence_number": r["licence_number"],
            "licensee": r["licensee"],
            "tier": tier_for(cls),
            "name_flags": cls["flags"],
            "licensee_licences": counts[norm_name(r["licensee"])],
        })
    out.sort(key=lambda q: (q["tier"], -q["licensee_licences"], q["licensee"].upper(), q["licence_number"]))
    for i, q in enumerate(out, 1):
        q["rank"] = i
    return out


def cmd_prioritise(args) -> int:
    d = data_dir()
    seed = read_csv(d / "register_summary.csv")
    queue = prioritise(seed)
    write_csv(d / "queue.csv", queue, QUEUE_FIELDS)
    tiers = Counter(q["tier"] for q in queue)
    log(f"queue: {len(queue)} licences -> {d / 'queue.csv'}; by tier: "
        + ", ".join(f"T{t}={tiers[t]}" for t in sorted(tiers)))
    return 0


# =========================================================================== #
# fetch-details
# =========================================================================== #


def write_status(d: Path, *, remaining: int, budget: dict, extra: dict | None = None) -> dict:
    status = {
        "complete": remaining == 0,
        "remaining": remaining,
        "quota_exhausted_until": budget.get("quota_exhausted_until"),
        "last_rate": budget.get("last_rate"),
        "calls_used_this_month": budget.get("calls_used_this_month", 0),
        "updated_at": iso(),
    }
    status.update(extra or {})
    prev = read_json(d / "status.json", {})
    same = {k: v for k, v in prev.items() if k != "updated_at"} == \
        {k: v for k, v in status.items() if k != "updated_at"}
    if not same:   # avoid an hourly timestamp-only commit while paused on quota
        write_json(d / "status.json", status)
    return status


def is_quota_error(rec: dict) -> bool:
    """A details.jsonl record that was rejected for quota (should be retried, not treated as done)."""
    if not rec.get("error"):
        return False
    raw = rec.get("raw")
    text = json.dumps(raw) if isinstance(raw, (dict, list)) else str(raw or "")
    return bool(QUOTA_BODY_RE.search(text) or QUOTA_BODY_RE.search(str(rec.get("error", ""))))


DETAIL_FILES = ("details.jsonl", "details_site.jsonl")   # api.nsw records, then website records


def load_details(d: Path) -> dict[str, dict]:
    """All details records keyed by licence_id, from every source file (later files win)."""
    out: dict[str, dict] = {}
    for name in DETAIL_FILES:
        out.update(read_jsonl(d / name, "licence_id"))
    return out


def fetched_ids(details: dict[str, dict]) -> set[str]:
    """licence_ids that need no further fetching: successes and genuine per-licence errors."""
    return {k for k, v in details.items() if not is_quota_error(v)}


def ensure_queue(d: Path) -> list[dict]:
    q = d / "queue.csv"
    if not q.exists():
        log("queue.csv missing -> running prioritise")
        write_csv(q, prioritise(read_csv(d / "register_summary.csv")), QUEUE_FIELDS)
    return read_csv(q)


def fetch_details(d: Path, client: AdaptiveClient | None, *, deadline: float, clock=time.time,
                  checkpoint=None, checkpoint_every: int = 200, monthly_budget: int = 0,
                  dry_run: bool = False) -> dict:
    """Core loop.  Returns the final status dict.  `client` may be None under dry-run."""
    budget_path = d / "budget.json"
    budget = load_budget(budget_path)
    queue = ensure_queue(d)
    details_path = d / "details.jsonl"
    done = fetched_ids(load_details(d))
    todo = [q for q in queue if q["licence_id"] not in done]
    log(f"queue {len(queue)}, fetched {len(done)}, remaining {len(todo)}")

    if env_flag("RESET_QUOTA") and budget.get("quota_exhausted_until"):
        log(f"RESET_QUOTA set -> clearing quota_exhausted_until={budget['quota_exhausted_until']}")
        budget["quota_exhausted_until"] = None
        budget.pop("quota_reason", None)
        write_json(budget_path, budget)
    if client is not None:
        # per-key quota state lives in budget.json; the client reads and updates it in place
        for kid, st in (budget.get("keys") or {}).items():
            client.key_state.setdefault(kid, {}).update({k: v for k, v in st.items()
                                                         if k not in client.key_state.get(kid, {})})
        budget["keys"] = client.key_state
        paused = keys_exhausted(budget, client.creds)
        if not paused:
            try:
                client.idx = client._first_usable()
            except QuotaExhausted:
                paused = True
    else:
        paused = quota_exhausted_now(budget)
    if paused:
        log(f"quota exhausted until {budget['quota_exhausted_until']} -> nothing to do this run")
        return write_status(d, remaining=len(todo), budget=budget, extra={"reason": "quota_exhausted"})
    if budget.get("quota_exhausted_until"):
        log("a fresh key is available -> clearing the global quota pause")
        budget["quota_exhausted_until"] = None
        budget.pop("quota_reason", None)
    if not todo:
        log("queue empty -> complete")
        return write_status(d, remaining=0, budget=budget)
    if dry_run:
        log(f"DRY_RUN: would fetch {len(todo)} licences starting with {todo[0]['licence_number']}")
        return write_status(d, remaining=len(todo), budget=budget, extra={"reason": "dry_run"})

    fetched = skipped = 0
    consecutive_errors = 0
    since_checkpoint = 0
    reason = "deadline"
    t0 = clock()
    rate = client.rate
    remaining = len(todo)

    def persist():
        budget["last_rate"] = rate.rpm
        budget["updated_at"] = iso()
        write_json(budget_path, budget)
        write_status(d, remaining=remaining, budget=budget,
                     extra={"run": {"fetched": fetched, "skipped": skipped, "seconds": round(clock() - t0)}})

    try:
        for q in todo:
            if clock() >= deadline:
                reason = "deadline"
                break
            if monthly_budget and budget["calls_used_this_month"] >= monthly_budget:
                reason = "monthly_budget"
                budget["quota_exhausted_until"] = iso(first_of_next_month())
                budget["quota_reason"] = f"MONTHLY_CALL_BUDGET={monthly_budget} reached"
                log(f"MONTHLY_CALL_BUDGET {monthly_budget} reached -> pausing until next month")
                break
            calls_before = client.calls
            try:
                status, body = client.details(q["licence_id"])
            finally:
                budget["calls_used_this_month"] += client.calls - calls_before
            rec = {"licence_id": q["licence_id"], "licence_number": q["licence_number"],
                   "fetched_at": iso()}
            if status == 200:
                rec["raw"] = body
                fetched += 1
                consecutive_errors = 0
            else:
                rec["error"] = f"HTTP {status}"
                rec["raw"] = body if isinstance(body, dict) else {"body": str(body)[:300]}
                skipped += 1
                consecutive_errors += 1
                log(f"{q['licence_number']} ({q['licence_id']}): HTTP {status} -> skipped")
                if status >= 500 or status in (401, 599):
                    reason = f"http_{status}"
                    break  # persistent server/auth trouble: let the next run retry (not recorded)
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    reason = f"http_{status}_x{consecutive_errors}"
                    log(f"{consecutive_errors} consecutive HTTP errors -> stopping this run; "
                        f"the last one is not recorded so it is retried")
                    break
            append_jsonl(details_path, rec)
            remaining -= 1
            since_checkpoint += 1
            n = fetched + skipped
            if n % 100 == 0:
                elapsed = max(clock() - t0, 1)
                log(f"fetched {fetched} this run ({n / elapsed * 60:.0f}/min actual, "
                    f"target {rate.rpm:.0f}/min), remaining {remaining}, "
                    f"calls this month {budget['calls_used_this_month']}")
            if since_checkpoint >= checkpoint_every:
                persist()
                if checkpoint:
                    checkpoint(f"enrich: checkpoint {len(queue) - remaining}/{len(queue)} details")
                since_checkpoint = 0
        else:
            reason = "complete"
    except QuotaExhausted as exc:
        reason = "quota_exhausted"
        budget["quota_exhausted_until"] = iso(first_of_next_month())
        budget["quota_reason"] = str(exc)[:300]
        log(f"QUOTA EXHAUSTED: {exc} -> paused until {budget['quota_exhausted_until']}")

    persist()
    status = write_status(d, remaining=remaining, budget=budget,
                          extra={"reason": reason,
                                 "run": {"fetched": fetched, "skipped": skipped,
                                         "seconds": round(clock() - t0)}})
    log(f"fetch-details done: {fetched} fetched, {skipped} skipped, reason={reason}, "
        f"remaining={remaining}, rate={rate.rpm:.1f}/min, calls this month="
        f"{budget['calls_used_this_month']}")
    return status


def run_deadline(default_seconds: int = 3000) -> float:
    if os.environ.get("RUN_DEADLINE", "").strip():
        return float(os.environ["RUN_DEADLINE"])
    return time.time() + env_int("RUN_SECONDS", default_seconds)


def cmd_fetch_details(args) -> int:
    d = data_dir()
    dry = env_flag("DRY_RUN")
    budget = load_budget(d / "budget.json")
    if env_flag("RESET_QUOTA") and budget.get("quota_exhausted_until"):
        log(f"RESET_QUOTA set -> clearing quota_exhausted_until={budget['quota_exhausted_until']}")
        budget["quota_exhausted_until"] = None
        budget.pop("quota_reason", None)
        write_json(d / "budget.json", budget)
    creds = parse_credentials() if not dry else []
    # fast path: every configured key is spent (or, with no key info, the month is) -> no API, <20s
    if keys_exhausted(budget, creds) if creds else quota_exhausted_now(budget):
        todo = len(ensure_queue(d)) - len(fetched_ids(load_details(d)))
        log(f"all {len(creds)} configured key(s) spent; quota exhausted until "
            f"{budget['quota_exhausted_until']}; exiting without touching the API")
        write_status(d, remaining=max(todo, 0), budget=budget, extra={"reason": "quota_exhausted"})
        return 0
    client = None
    if not dry:
        if not creds:
            print("ERROR: no api.nsw credentials: set NSW_API_KEY + NSW_API_SECRET, or NSW_API_KEYS "
                  "with one 'key:secret' per line (GitHub Secrets in Actions, .env locally)",
                  file=sys.stderr)
            return 2
        log(f"{len(creds)} api.nsw key(s) configured: " + ", ".join(key_id(k) for k, _ in creds))
        client = AdaptiveClient(creds, AdaptiveRate(budget.get("last_rate", DEFAULT_RPM)),
                                key_state=budget.setdefault("keys", {}))
    checkpoint = (lambda msg: commit_and_push(msg)) if should_push() else None
    fetch_details(d, client, deadline=run_deadline(), checkpoint=checkpoint,
                  monthly_budget=env_int("MONTHLY_CALL_BUDGET", 0), dry_run=dry)
    return 0


# =========================================================================== #
# fetch-abr
# =========================================================================== #

ABR_URL = "https://abr.business.gov.au/json/AbnDetails.aspx"


def details_abns(details: dict[str, dict]) -> list[str]:
    abns = []
    for rec in details.values():
        abn = flatten(rec).get("abn", "")
        if abn and abn not in abns:
            abns.append(abn)
    return abns


def parse_jsonp(text: str) -> dict:
    m = re.search(r"\((.*)\)\s*;?\s*$", text, re.S)
    return json.loads(m.group(1) if m else text)


def fetch_abr(d: Path, guid: str, *, deadline: float, http=http_get, sleep=time.sleep,
              clock=time.time, interval: float = 0.5) -> int:
    details = load_details(d)
    have = read_jsonl(d / "abr.jsonl", "abn")
    todo = [a for a in details_abns(details) if a not in have]
    log(f"abr: {len(have)} cached, {len(todo)} to fetch")
    n = 0
    for abn in todo:
        if clock() >= deadline:
            log("abr: deadline reached")
            break
        r = http(ABR_URL, params={"abn": abn, "callback": "cb", "guid": guid}, timeout=30)
        rec = {"abn": abn, "fetched_at": iso()}
        if r.status_code == 200:
            try:
                rec["raw"] = parse_jsonp(r.text)
            except ValueError:
                rec["error"] = "unparseable"
                rec["raw"] = {"body": r.text[:300]}
        else:
            rec["error"] = f"HTTP {r.status_code}"
            rec["raw"] = {"body": r.text[:300]}
            if r.status_code in (401, 403):
                log(f"abr: HTTP {r.status_code} -> check ABR_GUID; stopping")
                break
        append_jsonl(d / "abr.jsonl", rec)
        n += 1
        if n % 100 == 0:
            log(f"abr: {n} fetched")
        sleep(interval)
    log(f"abr: fetched {n}")
    return n


def cmd_fetch_abr(args) -> int:
    guid = os.environ.get("ABR_GUID", "").strip()
    if not guid:
        log("ABR_GUID not set -> skipping fetch-abr")
        return 0
    if env_flag("DRY_RUN"):
        log("DRY_RUN -> skipping fetch-abr")
        return 0
    fetch_abr(data_dir(), guid, deadline=run_deadline())
    return 0


# =========================================================================== #
# build
# =========================================================================== #


def _get(obj, *keys, default=""):
    """Case-insensitive dict lookup trying several key names."""
    if not isinstance(obj, dict):
        return default
    lower = {str(k).lower(): v for k, v in obj.items()}
    for k in keys:
        v = lower.get(k.lower())
        if v not in (None, ""):
            return v
    return default


def _active(item: dict) -> bool:
    return str(_get(item, "isActive", "active", "status", default="")).strip().lower() \
        in ("", "true", "y", "yes", "1", "active", "current")


POSTCODE_RE = re.compile(r"\b([1-9]\d{3})\b")


def postcode_from_address(addr: str) -> str:
    m = POSTCODE_RE.findall(addr or "")
    return m[-1] if m else ""


def normalise_site_search(raw, licence_number: str = "", licence_id: str = ""):
    """The licence-check website's search response ({"pagingInfo", "results": [...]}) carries ABN,
    ACN, licensee and the registered address with postcode per row.  Reshape the row for this
    licence into the details layout flatten() reads; the address doubles as the one known site."""
    rows = raw.get("results") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return None
    for r in rows:
        if not isinstance(r, dict):
            continue
        if (licence_number and r.get("licenceNumber") == licence_number) or \
                (licence_id and r.get("licenceId") == licence_id) or \
                (not licence_number and not licence_id and len(rows) == 1):
            addr = r.get("address") or ""
            return {"licenceDetail": dict(r, licenceeABN=r.get("ABN", ""), licenceeACN=r.get("ACN", ""),
                                          startDate=r.get("granted", ""), expiryDate=r.get("expires", "")),
                    "premises": [{"type": "Registered address", "businessName": None,
                                  "businessAddress": addr, "endDate": None}] if addr else [],
                    "licenceClasses": [], "conditions": [],
                    "businessNames": [{"businessName": b} for b in (r.get("businessNameList") or [])],
                    "_site": "search"}
    return None


def _addr_text(v) -> str:
    if isinstance(v, dict):
        return re.sub(r"\s+", " ", " ".join(str(x) for x in v.values() if isinstance(x, (str, int)) and x)).strip()
    return re.sub(r"\s+", " ", str(v or "")).strip()


def normalise_site_details(raw: dict):
    """The licence-check website's details call answers {"componentData": {...}} with its own key
    names.  Reshape it, best effort, into the api.nsw details layout flatten() reads; the raw
    record is stored untouched, so this can be refined and the build re-run at any time."""
    cd = raw.get("componentData")
    if not isinstance(cd, dict):
        return None

    def pick(*names):
        for n in names:
            v = _get(cd, n, default=None)
            if v not in (None, "", [], {}):
                return v
        return None

    # premises: "locations": [{"type": "Fixed", "premises": [{"address", "suburb", "state",
    # "postcode", "startDate", "type"}]}] (plus a few flatter spellings, just in case)
    prem_items: list = []
    for loc in pick("locations", "sites") or []:
        if isinstance(loc, dict):
            inner = loc.get("premises") or loc.get("addresses") or []
            prem_items += [dict(p, _loctype=loc.get("type")) for p in inner if isinstance(p, dict)]
    for p in pick("premises", "premisesList", "businessPremises", "premisesAddresses", "addresses") or []:
        prem_items.append(p)
    premises = []
    for p in prem_items:
        if isinstance(p, dict):
            addr = _get(p, "businessAddress", "fullAddress", "premisesAddress", "addressLine", "address",
                        default=None)
            if not addr:
                addr = " ".join(str(_get(p, k)) for k in ("addressLine1", "addressLine2", "street",
                                                          "suburb", "state", "postcode") if _get(p, k))
            addr = _addr_text(addr)
            pc = str(_get(p, "postcode", "postCode")).strip()
            if pc and pc not in addr:           # keep the postcode on the address so regions resolve
                addr = f"{addr} {_get(p, 'state') or 'NSW'} {pc}".strip()
            premises.append({"type": _get(p, "type", "premisesType") or p.get("_loctype") or "",
                             "rego": _get(p, "rego", "registration") or None,
                             "businessName": _get(p, "businessName", "tradingName") or None,
                             "businessAddress": addr,
                             "endDate": _get(p, "endDate", "ceasedDate", default=None) or None})
        elif isinstance(p, str) and p.strip():
            premises.append({"type": "", "businessName": None, "businessAddress": p.strip(), "endDate": None})
    ld = dict(cd)
    ld.setdefault("licenceeABN", re.sub(r"\s+", "", str(_get(cd, "ABN", "abn", "formattedABN"))))
    ld.setdefault("licenceeACN", re.sub(r"\s+", "", str(_get(cd, "ACN", "acn", "formattedACN"))))
    ld.setdefault("startDate", _get(cd, "granted", "startDate", "grantedDate"))
    ld.setdefault("expiryDate", _get(cd, "expires", "expiryDate", "expiresDate"))
    classes = pick("licenceClasses", "classes", "licenceClass", "categories") or []
    conds = pick("conditions", "licenceConditions") or []
    biz = pick("businessNames", "businessNameList", "tradingNames") or []
    # class history: the pre-2014 repair classes ("Motor Mechanic Fixed Workshop", "Panel Beater"...)
    # survive only as Class Approved / Class Lapsed events, and they say what the shop does
    hist_classes: list[str] = []
    for ev in pick("history") or []:
        if isinstance(ev, dict) and "class" in str(ev.get("eventType", "")).lower():
            for dsc in ev.get("descriptions") or []:
                name = (dsc.get("short") if isinstance(dsc, dict) else str(dsc)) or ""
                if name and name not in hist_classes and not GENERIC_CLASS_RE.match(name.strip().upper()):
                    hist_classes.append(name)
    directors = []
    for role in pick("associatedRoles") or []:
        if isinstance(role, dict) and str(role.get("name", "")).lower() in ("director", "partner", "trustee"):
            directors += [p.get("name") for p in role.get("parties") or [] if isinstance(p, dict) and p.get("name")]
    summary = {str(s.get("type")): s.get("count") for s in (pick("complianceSummary") or [])
               if isinstance(s, dict)}
    comp = pick("complianceActions", "compliance") or {}
    if isinstance(comp, list):
        comp = {"disciplinaryActions": comp}
    comp = dict(comp)
    comp.setdefault("publicWarningsCount", summary.get("Public Warning", ""))
    comp.setdefault("disciplinaryActions", summary.get("Disciplinary Action", ""))
    return {"licenceDetail": ld,
            "premises": premises,
            "licenceClasses": classes if isinstance(classes, list) else [classes],
            "historicalClasses": hist_classes,
            "conditions": conds if isinstance(conds, list) else [conds],
            "businessNames": [b if isinstance(b, dict) else {"businessName": str(b)} for b in biz],
            "complianceActions": comp,
            "associatedParties": [{"name": n, "role": "Director"} for n in directors],
            "_site": "details"}


def flatten(rec: dict) -> dict:
    """Flatten one details.jsonl record into the enrichment columns."""
    raw = rec.get("raw") if isinstance(rec.get("raw"), dict) else {}
    if rec.get("error") or not raw:
        return {}
    if "componentData" in raw:
        raw = normalise_site_details(raw) or {}
        if not raw:
            return {}
    rows = raw.get("results")
    if isinstance(rows, list) and rows and isinstance(rows[0], dict) and "licenceNumber" in rows[0] \
            and "licenceDetail" not in rows[0] and not _get(raw, "licenceDetail", "licenceDetails", default=None):
        raw = normalise_site_search(raw, rec.get("licence_number", ""), rec.get("licence_id", "")) or {}
        if not raw:
            return {}
    ld = _get(raw, "licenceDetail", "licenceDetails", "licence", default={})
    classes = []
    for c in _get(raw, "licenceClasses", "classes", default=[]) or []:
        name = _get(c, "className", "classDescription", "class", "name", "description") if isinstance(c, dict) else str(c)
        if name and (not isinstance(c, dict) or _active(c)):
            classes.append(str(name).strip())
    premises = []
    vehicles = 0          # mobile workshops: the register lists a vehicle (rego), not an address
    for p in _get(raw, "premises", "premisesList", default=[]) or []:
        if not isinstance(p, dict):
            continue
        if str(_get(p, "endDate", default="") or "").strip():
            continue  # closed premises
        pname = str(_get(p, "businessName", "premisesName", "name")).strip()
        paddr = re.sub(r"\s+", " ", str(_get(p, "businessAddress", "address", "premisesAddress"))).strip()
        if pname or paddr:
            premises.append((pname, paddr))
        elif _get(p, "rego", "registration") or "mobile" in str(_get(p, "type")).lower():
            vehicles += 1
    conditions = []
    for c in _get(raw, "conditions", default=[]) or []:
        txt = _get(c, "description", "condition", "text") if isinstance(c, dict) else str(c)
        if txt:
            conditions.append(str(txt).strip())
    biz = []
    for b in _get(raw, "businessNames", default=[]) or []:
        name = _get(b, "businessName", "name") if isinstance(b, dict) else str(b)
        if name:
            biz.append(str(name).strip())
    comp = _get(raw, "complianceActions", "compliance", default={})
    addr = str(_get(ld, "address", "licenceeAddress", "fullAddress")).strip()
    premises_pcs = [postcode_from_premises(paddr) for _, paddr in premises if paddr]
    # the shop's postcode beats the licensee's postal one
    pc = next((x for x in premises_pcs if x), "") or str(_get(ld, "postcode", "postCode")).strip() \
        or postcode_from_address(addr)
    hist = [str(h).strip() for h in (_get(raw, "historicalClasses", default=[]) or []) if h]
    directors = [str(_get(p, "name")) for p in (_get(raw, "associatedParties", default=[]) or [])
                 if isinstance(p, dict) and str(_get(p, "role")).lower() == "director" and _get(p, "name")]
    return {
        "historical_classes": "; ".join(hist),
        "historical_classes_list": hist,
        "directors": "; ".join(directors),
        "abn": re.sub(r"\s+", "", str(_get(ld, "licenceeABN", "abn"))),
        "acn": (lambda a: a if valid_acn(a) else "")(re.sub(r"\s+", "", str(_get(ld, "licenceeACN", "acn")))),
        "address_full": addr,
        "details_postcode": pc,
        "start_date": str(_get(ld, "startDate", "licenceStartDate", "granted")),
        "details_expiry_date": str(_get(ld, "expiryDate", "licenceExpiryDate", "expires")),
        "licence_classes": "; ".join(classes),
        "classes_list": classes,
        "n_premises": len(premises),
        "mobile_vehicles": vehicles,
        "premises": "; ".join(f"{n} @ {a}".strip(" @") for n, a in premises),
        "premises_list": premises,
        "premises_postcodes": premises_pcs,
        "premises_names": " | ".join(n for n, _ in premises),
        "conditions": "; ".join(conditions),
        "conditions_list": conditions,
        "business_names_full": "; ".join(biz),
        "public_warnings": str(_get(comp, "publicWarningsCount", "publicWarnings")),
        "disciplinary_actions": len(_get(comp, "disciplinaryActions", default=[]) or [])
        if isinstance(_get(comp, "disciplinaryActions", default=[]), list)
        else str(_get(comp, "disciplinaryActions")),
    }


def flatten_abr(rec: dict) -> dict:
    raw = rec.get("raw") if isinstance(rec.get("raw"), dict) else {}
    if rec.get("error") or not raw or _get(raw, "Message"):
        return {}
    names = _get(raw, "BusinessName", default=[]) or []
    return {
        "entity_type": str(_get(raw, "EntityTypeName")),
        "entity_type_code": str(_get(raw, "EntityTypeCode")),
        "entity_name": str(_get(raw, "EntityName")),
        "abn_status": str(_get(raw, "AbnStatus")),
        "gst": str(_get(raw, "Gst")),
        "abr_state": str(_get(raw, "AddressState")),
        "abr_postcode": str(_get(raw, "AddressPostcode")),
        "abr_business_names": "; ".join(str(n) for n in names) if isinstance(names, list) else str(names),
    }


_GEO: dict | None = None


def build_localities(rows: list[dict]) -> dict[str, str]:
    """NSW locality (upper) -> postcode.  Delivery-area rows outrank PO-box rows; ties -> lowest."""
    scores: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        if (row.get("state") or "").strip().upper() != "NSW":
            continue
        loc = re.sub(r"\s+", " ", (row.get("locality") or "").strip().upper())
        pc = (row.get("postcode") or "").strip().zfill(4)
        if loc and pc.isdigit():
            w = 2 if "DELIVERY" in (row.get("type") or "").upper() else 1
            scores[loc][pc] += w
    return {loc: sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] for loc, c in scores.items()}


def geo() -> dict:
    """{"postcodes": postcode -> {region, sa4, lga}, "localities": LOCALITY -> postcode}"""
    global _GEO
    if _GEO is None:
        p = os.environ.get("POSTCODES_CSV", str(HERE / "australian_postcodes.csv"))
        if Path(p).exists():
            rows = load_postcode_table(p, data_dir())
            _GEO = {"postcodes": build_nsw_postcodes(rows), "localities": build_localities(rows)}
        else:
            _GEO = {"postcodes": {}, "localities": {}}
    return _GEO


def region_for(pc: str, table: dict | None = None) -> tuple[str, str, str]:
    table = geo()["postcodes"] if table is None else table
    pc = (pc or "").strip()
    e = table.get(pc)
    if e:
        return e["region"], e["sa4"], e["lga"]
    if pc:
        return "Outside NSW postcode table", "", ""
    return "Unknown (no address in register)", "", ""


ADDRESS_NOISE_RE = re.compile(r"\b(NSW|N\.S\.W\.|AUSTRALIA)\b|[,.]", re.I)


def postcode_from_premises(addr: str, localities: dict[str, str] | None = None) -> str:
    """Premises addresses look like '11 Waltham Street ARTARMON' (no postcode).  Use an explicit
    4-digit postcode if present, else the longest trailing word-run that is a known NSW locality."""
    localities = geo()["localities"] if localities is None else localities
    pc = postcode_from_address(addr)
    if pc:
        return pc
    words = re.sub(r"\s+", " ", ADDRESS_NOISE_RE.sub(" ", addr or "")).strip().upper().split()
    for n in (4, 3, 2, 1):
        if len(words) >= n:
            cand = " ".join(words[-n:])
            if cand in localities:
                return localities[cand]
    return ""


def valid_acn(acn: str) -> bool:
    """ASIC check digit.  Rejects the register's placeholders (999999999 is given to councils,
    universities and other non-companies, and would merge them all into one operator)."""
    if len(acn) != 9 or not acn.isdigit() or len(set(acn)) == 1:
        return False
    total = sum(int(d) * w for d, w in zip(acn[:8], range(8, 0, -1)))
    return (10 - total % 10) % 10 == int(acn[8])


def valid_abn(abn: str) -> bool:
    if len(abn) != 11 or not abn.isdigit() or len(set(abn)) == 1:
        return False
    digits = [int(abn[0]) - 1] + [int(c) for c in abn[1:]]
    return sum(d * w for d, w in zip(digits, (10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19))) % 89 == 0


def entity_key(det: dict) -> str:
    """Stable operator id from details: ACN first (a company's ABN is its ACN plus two check
    digits, and the register often has one but not the other), else ABN, else ''.
    Placeholder or malformed numbers are ignored."""
    acn, abn = det.get("acn", ""), det.get("abn", "")
    if valid_acn(acn):
        return f"acn:{acn}"
    if valid_abn(abn):
        return f"acn:{abn[2:]}" if valid_acn(abn[2:]) else f"abn:{abn}"
    return ""


SUMMARY_FIELDS = ["licence_number", "licensee", "licence_name", "business_names", "licence_type",
                  "status", "expiry_date", "classes", "categories", "suburb", "postcode", "region",
                  "sa4", "lga", "licence_id"]
ENRICH_FIELDS = ["abn", "acn", "address_full", "start_date", "licence_classes", "historical_classes",
                 "n_premises", "mobile_vehicles", "premises", "premises_regions", "conditions",
                 "business_names_full", "directors", "public_warnings", "disciplinary_actions"]
MOBILE_REGION = "Mobile (no fixed premises)"
LICENCE_FIELDS = SUMMARY_FIELDS + ENRICH_FIELDS + ["tier", "name_flags", "segment_rule",
                                                    "franchise_brand", "operator_key", "details_fetched"]


def enrich_licences(seed: list[dict], queue: list[dict], details: dict[str, dict],
                    postcodes: dict | None = None) -> list[dict]:
    qmap = {q["licence_id"]: q for q in queue}
    # Licences without details yet adopt the entity id seen on a fetched licence of the same
    # licensee name, so an operator is not split into "abn:" and "name:" halves mid-fetch.
    name_entity: dict[str, Counter] = defaultdict(Counter)
    for r in seed:
        eid = entity_key(flatten(details.get(r["licence_id"], {})))
        if eid:
            name_entity[norm_name(r["licensee"])][eid] += 1
    rows = []
    for r in seed:
        row = {k: r.get(k, "") for k in SUMMARY_FIELDS}
        det = flatten(details.get(r["licence_id"], {}))
        row["details_fetched"] = bool(det)
        for k in ENRICH_FIELDS:
            row[k] = det.get(k, "")
        if det.get("details_expiry_date") and not row["expiry_date"]:
            row["expiry_date"] = det["details_expiry_date"]
        mobile_only = bool(det.get("mobile_vehicles")) and not det.get("premises_list")
        if det.get("details_postcode"):
            row["postcode"] = det["details_postcode"]
            row["region"], row["sa4"], row["lga"] = region_for(det["details_postcode"], postcodes)
        elif mobile_only and (not row["region"] or row["region"].startswith("Unknown")):
            row["region"], row["sa4"], row["lga"] = MOBILE_REGION, "", ""
        elif not row["region"]:
            row["region"], row["sa4"], row["lga"] = region_for(row["postcode"], postcodes)
        # name flags: recompute with the extra names from details, fall back to queue
        text_row = dict(r, business_names_full=det.get("business_names_full", ""),
                        premises_names=det.get("premises_names", ""))
        cls = classify_name(text_row)
        q = qmap.get(r["licence_id"])
        row["tier"] = q["tier"] if q else tier_for(cls)
        row["name_flags"] = cls["flags"]
        # Order: current specific classes, then licence conditions, then a name exclusion family
        # (a truck depot or council that once held "Motor Mechanic Fixed Workshop" is still not a
        # service shop), then the pre-2014 class history, then the remaining name flags.
        row["segment_rule"] = (segment_from_classes(det.get("classes_list", []))
                               or ("mobile" if mobile_only else "")
                               or segment_from_conditions(det.get("conditions_list", []))
                               or (segment_from_flags(cls) if cls["exclusions"] else "")
                               or segment_from_classes(det.get("historical_classes_list", []))
                               or segment_from_flags(cls))
        row["conditions"] = det.get("conditions", "")
        row["premises_regions"] = "; ".join(sorted({region_for(x, postcodes)[0]
                                                    for x in det.get("premises_postcodes", []) if x}))
        row["franchise_brand"] = franchise_brand(name_text(text_row))
        nn = norm_name(r["licensee"])
        eid = entity_key(det) or (name_entity[nn].most_common(1)[0][0] if name_entity.get(nn) else "")
        row["operator_key"] = eid or f"name:{nn}"
        row["_premises_list"] = det.get("premises_list", [])
        row["_company"] = cls["company"]
        row["_dealer"] = "excl_dealer" in cls["exclusions"]
        rows.append(row)
    return rows


OPERATOR_FIELDS = ["operator", "abn", "acn", "entity_type", "entity_name", "gst", "n_licences",
                   "n_premises_total", "n_premises_known", "premises", "regions", "segment",
                   "franchise_brand", "is_sole_trader", "is_dealer_group", "details_coverage",
                   "licence_numbers", "operator_key"]


def build_operators(licences: list[dict], abr: dict[str, dict]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in licences:
        groups[row["operator_key"]].append(row)
    ops = []
    for key, rows in groups.items():
        abn = next((r["abn"] for r in rows if r["abn"]), "")
        acn = next((r["acn"] for r in rows if r["acn"]), "") or (key[4:] if key.startswith("acn:") else "")
        abr_info = flatten_abr(abr.get(abn, {})) if abn else {}
        seen_addr: dict[str, str] = {}
        unknown_sites = 0
        for row in rows:
            if row["details_fetched"]:
                for pname, paddr in row["_premises_list"]:
                    k = re.sub(r"[^A-Z0-9]", "", paddr.upper()) or f"{pname}|{row['licence_number']}"
                    seen_addr.setdefault(k, f"{pname} @ {paddr}".strip(" @"))
            else:
                unknown_sites += 1  # no details yet: assume one site per licence
        names = Counter(r["licensee"].strip() for r in rows)
        seg = Counter(r["segment_rule"] for r in rows).most_common(1)[0][0]
        brands = [r["franchise_brand"] for r in rows if r["franchise_brand"]]
        regions = sorted({r["region"] for r in rows if r["region"]}
                         | {x for r in rows for x in r.get("premises_regions", "").split("; ") if x})
        fetched = sum(1 for r in rows if r["details_fetched"])
        ops.append({
            "operator": abr_info.get("entity_name") or names.most_common(1)[0][0],
            "abn": abn,
            "acn": acn,
            "entity_type": abr_info.get("entity_type") or
                           ("Company" if rows[0]["_company"] or acn else "Individual/Partnership"),
            "entity_name": abr_info.get("entity_name", ""),
            "gst": abr_info.get("gst", ""),
            "n_licences": len(rows),
            "n_premises_total": len(seen_addr) + unknown_sites,
            "n_premises_known": len(seen_addr),
            "premises": "; ".join(seen_addr.values()),
            "regions": "; ".join(regions),
            "segment": seg,
            "franchise_brand": Counter(brands).most_common(1)[0][0] if brands else "",
            "is_sole_trader": not rows[0]["_company"] and not acn and
                              abr_info.get("entity_type_code", "") in ("", "IND"),
            "is_dealer_group": any(r["_dealer"] for r in rows),
            "details_coverage": f"{fetched}/{len(rows)}",
            "licence_numbers": "; ".join(sorted(r["licence_number"] for r in rows)),
            "operator_key": key,
        })
    ops.sort(key=lambda o: (-o["n_premises_total"], -o["n_licences"], o["operator"].upper()))
    return ops


SHORTLIST_FIELDS = ["block", "operator", "abn", "acn", "entity_type", "n_licences", "n_premises_total",
                    "premises", "regions", "segment", "details_coverage", "licence_numbers"]


def build_shortlist(ops: list[dict]) -> list[dict]:
    service = [o for o in ops if o["segment"] == "service" and not o["is_dealer_group"]
               and not o["franchise_brand"]]
    multi = [dict(o, block="multi_site") for o in service
             if o["n_premises_total"] >= 2 or o["n_licences"] >= 2]
    single = [dict(o, block="single_site_company") for o in service
              if not (o["n_premises_total"] >= 2 or o["n_licences"] >= 2) and not o["is_sole_trader"]]
    return multi + single


def md_table(headers: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def build_summary(licences: list[dict], ops: list[dict], shortlist: list[dict], status: dict,
                  budget: dict, queue_len: int) -> str:
    fetched = sum(1 for r in licences if r["details_fetched"])
    remaining = status.get("remaining", queue_len - fetched)
    rate = float(budget.get("last_rate") or DEFAULT_RPM)
    eta_h = remaining / rate / 60 if rate else 0
    quota = budget.get("quota_exhausted_until") or ""
    quota_state = f"paused until {quota}" if quota and quota_exhausted_now(budget) else "ok"
    lines = ["# NSW motor-repairer enrichment — summary", "",
             f"_As of {status.get('updated_at') or budget.get('updated_at') or iso()}_", "",
             "## Fetch progress", "",
             md_table(["metric", "value"], [
                 ["licences in queue", queue_len],
                 ["details fetched", fetched],
                 ["remaining", remaining],
                 ["last good rate", f"{rate:.0f} req/min"],
                 ["ETA at that rate", f"{eta_h:.1f} h of run time (~{eta_h / 50 * 60:.1f} hourly runs)"
                  if remaining else "done"],
                 ["calls this month", budget.get("calls_used_this_month", 0)],
                 ["quota state", quota_state],
                 ["api keys seen", "; ".join(
                     f"{kid}: {'spent' if quota_exhausted_now(st) else 'ok'} ({st.get('calls', 0)} calls)"
                     for kid, st in (budget.get("keys") or {}).items()) or "none recorded yet"],
                 ["complete", status.get("complete", False)],
                 ["last run reason", status.get("reason", "")],
             ]), ""]
    segs = sorted({r["segment_rule"] for r in licences})
    by_rs: dict[str, Counter] = defaultdict(Counter)
    for r in licences:
        by_rs[r["region"]][r["segment_rule"]] += 1
    lines += ["## Licences by region × segment", "",
              md_table(["region", *segs, "total"],
                       [[reg, *[by_rs[reg][s] for s in segs], sum(by_rs[reg].values())]
                        for reg in sorted(by_rs, key=lambda k: -sum(by_rs[k].values()))]
                       + [["**total**", *[sum(c[s] for c in by_rs.values()) for s in segs], len(licences)]]),
              ""]
    tiers = Counter(r["tier"] for r in licences)
    tiers_f = Counter(r["tier"] for r in licences if r["details_fetched"])
    lines += ["## Queue tiers", "",
              md_table(["tier", "meaning", "licences", "fetched"], [
                  [t, m, tiers[t], tiers_f[t]] for t, m in [
                      (1, "company + service-shop name"), (2, "company, uninformative name"),
                      (3, "sole trader + service-shop name"), (4, "exclusion family in name"),
                      (5, "sole trader, uninformative name")]]), ""]
    lines += [f"## Operators ({len(ops)}) — top 50 by premises", "",
              md_table(["operator", "abn", "segment", "licences", "premises", "regions", "brand"],
                       [[o["operator"], o["abn"], o["segment"], o["n_licences"], o["n_premises_total"],
                         o["regions"], o["franchise_brand"]] for o in ops[:50]]), ""]
    n_multi = sum(1 for s in shortlist if s["block"] == "multi_site")
    lines += ["## Shortlist", "",
              f"- independent multi-site service operators: **{n_multi}**",
              f"- single-site service companies (widen later): **{len(shortlist) - n_multi}**", ""]
    return "\n".join(lines)


def cmd_build(args) -> int:
    d = data_dir()
    out = d / "out"
    seed = read_csv(d / "register_summary.csv")
    queue = ensure_queue(d)
    details = {k: v for k, v in load_details(d).items() if not is_quota_error(v)}
    abr = read_jsonl(d / "abr.jsonl", "abn")
    budget = load_budget(d / "budget.json")
    status = read_json(d / "status.json", {})
    licences = enrich_licences(seed, queue, details)
    write_csv(out / "licences_enriched.csv", licences, LICENCE_FIELDS)
    ops = build_operators(licences, abr)
    write_csv(out / "operators.csv", ops, OPERATOR_FIELDS)
    shortlist = build_shortlist(ops)
    write_csv(out / "shortlist.csv", shortlist, SHORTLIST_FIELDS)
    if not status:
        status = {"remaining": len([q for q in queue if q["licence_id"] not in details]),
                  "complete": all(q["licence_id"] in details for q in queue)}
    (out / "summary.md").write_text(build_summary(licences, ops, shortlist, status, budget, len(queue)),
                                    encoding="utf-8")
    log(f"build: {len(licences)} licences, {len(ops)} operators, {len(shortlist)} shortlisted -> {out}")
    return 0


# =========================================================================== #
# commit / all / main
# =========================================================================== #


def default_commit_message() -> str:
    st = read_json(data_dir() / "status.json", {})
    run = os.environ.get("GITHUB_RUN_NUMBER", "local")
    return (f"enrich: run {run} - remaining {st.get('remaining', '?')}, "
            f"reason {st.get('reason', '')}, calls this month {st.get('calls_used_this_month', '?')}")


def cmd_commit(args) -> int:
    if not should_push():
        log("not in GitHub Actions (or DRY_RUN) -> skipping commit/push")
        return 0
    commit_and_push(args.message or default_commit_message())
    return 0


def cmd_all(args) -> int:
    rc = cmd_fetch_details(args)
    if rc:
        return rc
    cmd_fetch_abr(args)
    return cmd_build(args)


def main(argv: list[str] | None = None) -> int:
    load_dotenv(HERE / ".env")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prioritise").set_defaults(fn=cmd_prioritise)
    sub.add_parser("fetch-details").set_defaults(fn=cmd_fetch_details)
    sub.add_parser("fetch-abr").set_defaults(fn=cmd_fetch_abr)
    sub.add_parser("build").set_defaults(fn=cmd_build)
    sub.add_parser("all").set_defaults(fn=cmd_all)
    c = sub.add_parser("commit")
    c.add_argument("-m", "--message", default="")
    c.set_defaults(fn=cmd_commit)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
