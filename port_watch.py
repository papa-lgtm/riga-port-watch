#!/usr/bin/env python3
"""
Riga port watch, built on the Freeport of Riga's own ship list.

Polls the public endpoint behind the authority's "Kuģi ostā" page, keeps track
of every vessel call, and records two kinds of signal:

  REPAIR     — the vessel has been at a repair or dock berth during this call
  LONG_STAY  — the vessel has been alongside longer than the configured limit

Both mean the same thing commercially: the vessel is in a repair event right
now and someone is buying parts for it.

No API key, no websocket, no coordinates. Standard library only.
"""

import csv
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
CONFIG_FILE = BASE / "config.json"
STATE_FILE = BASE / "port_state.json"
EVENTS_FILE = BASE / "port_events.csv"
RIGA = ZoneInfo("Europe/Riga")

EVENT_COLUMNS = [
    "detected", "event", "imo", "vessel_name", "flag", "gt", "length_m",
    "berths", "arrived", "days_alongside", "planned_departure", "visit_id",
]

# Field names as the source returns them, in Latvian.
F_NAME = "nosaukums"
F_IMO = "Imo"
F_FLAG = "Karogs"
F_GT = "GT"
F_LENGTH = "garums"
F_BERTHS = "PasreizejaPiestatne"
F_STATUS = "Statuss"
F_ATA = "ata"
F_ETA = "eta"
F_ETD = "etd"
F_ATD = "atd"
F_VISIT = "shipVisitId"


# ---------------------------------------------------------------- helpers

def now():
    return datetime.now(RIGA)


def iso(dt):
    return dt.strftime("%Y-%m-%d %H:%M")


def parse_ts(value):
    """The source sends naive local timestamps, or null."""
    if not value:
        return None
    try:
        return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=RIGA)
    except ValueError:
        return None


def as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_json(path, default):
    if path.exists():
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return default


def fetch(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "riga-watch/1.0 (port call monitor)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def berth_list(raw):
    """The berth field holds one or several codes, comma separated."""
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def is_repair_berth(code, prefixes):
    return any(code.upper().startswith(prefix.upper()) for prefix in prefixes)


def clean_imo(value):
    """Some entries carry no IMO number, or the word 'nav' (Latvian for none)."""
    if not value:
        return ""
    text = str(value).strip()
    if not text.isdigit():
        return ""
    return text


# ---------------------------------------------------------------- core

def interesting(vessel, rules):
    """Skip harbour craft and small vessels. Returns None if the vessel passes."""
    length = as_float(vessel.get(F_LENGTH))
    if length is not None and length < rules["min_length_m"]:
        return f"length {length:.0f} m"
    gt = as_float(vessel.get(F_GT))
    if gt is not None and gt < rules["min_gt"]:
        return f"GT {gt:.0f}"
    return None


def process(vessels, state, cfg, run_time):
    rules = cfg["rules"]
    prefixes = cfg["repair_berth_prefixes"]
    events = []
    berth_index = {}
    skipped = []
    active = []

    for vessel in vessels:
        name = (vessel.get(F_NAME) or "").strip()
        visit_id = str(vessel.get(F_VISIT) or "")
        if not visit_id:
            continue

        reason = interesting(vessel, rules)
        if reason:
            skipped.append(f"{name} ({reason})")
            continue

        berths = berth_list(vessel.get(F_BERTHS))
        for code in berths:
            berth_index.setdefault(code, []).append(name)

        arrived = parse_ts(vessel.get(F_ATA))
        departed = parse_ts(vessel.get(F_ATD))
        status = (vessel.get(F_STATUS) or "").upper()

        # A call that has not started yet carries no signal.
        if arrived is None:
            continue

        days = (run_time - arrived).total_seconds() / 86400
        repair_berths = [c for c in berths if is_repair_berth(c, prefixes)]

        record = state.get(visit_id, {
            "name": name,
            "imo": clean_imo(vessel.get(F_IMO)),
            "flagged_repair": False,
            "flagged_long_stay": False,
            "berths_seen": [],
        })
        record["name"] = name
        record["imo"] = clean_imo(vessel.get(F_IMO)) or record.get("imo", "")
        record["last_seen"] = iso(run_time)
        record["status"] = status
        record["berths_seen"] = sorted(set(record.get("berths_seen", []) + berths))

        base_row = {
            "detected": iso(run_time),
            "imo": record["imo"],
            "vessel_name": name,
            "flag": vessel.get(F_FLAG) or "",
            "gt": vessel.get(F_GT) or "",
            "length_m": vessel.get(F_LENGTH) or "",
            "berths": ", ".join(record["berths_seen"]),
            "arrived": iso(arrived),
            "days_alongside": round(days, 1),
            "planned_departure": iso(parse_ts(vessel.get(F_ETD))) if vessel.get(F_ETD) else "",
            "visit_id": visit_id,
        }

        # A call that has already ended carries no signal either.
        if departed is not None:
            state[visit_id] = record
            continue

        if repair_berths and not record["flagged_repair"]:
            record["flagged_repair"] = True
            events.append(dict(base_row, event="REPAIR"))

        if days >= rules["long_stay_days"] and not record["flagged_long_stay"]:
            record["flagged_long_stay"] = True
            events.append(dict(base_row, event="LONG_STAY"))

        state[visit_id] = record

        if departed is None:
            active.append((days, base_row, bool(repair_berths)))

    cutoff = run_time - timedelta(days=rules["forget_visit_after_days"])
    state = {
        vid: rec for vid, rec in state.items()
        if datetime.strptime(rec["last_seen"], "%Y-%m-%d %H:%M").replace(tzinfo=RIGA) > cutoff
    }

    return state, events, berth_index, active, skipped


# ---------------------------------------------------------------- output

def append_events(events):
    new_file = not EVENTS_FILE.exists()
    with open(EVENTS_FILE, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=EVENT_COLUMNS)
        if new_file:
            writer.writeheader()
        for row in events:
            writer.writerow({k: row.get(k, "") for k in EVENT_COLUMNS})


def write_current(active, run_time):
    lines = [f"# Vessels in the port of Riga — {iso(run_time)}", ""]
    lines.append("Sorted by time alongside. A star marks a repair or dock berth.")
    lines.append("")
    lines.append("| | Vessel | IMO | Flag | GT | L, m | Berths | Arrived | Days |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for days, row, repair in sorted(active, reverse=True, key=lambda x: x[0]):
        mark = "*" if repair else ""
        lines.append(
            f"| {mark} | {row['vessel_name']} | {row['imo']} | {row['flag']} "
            f"| {row['gt']} | {row['length_m']} | {row['berths']} "
            f"| {row['arrived']} | {days:.1f} |"
        )
    if not active:
        lines.append("| | _nothing_ | | | | | | | |")
    (BASE / "current.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_berths(berth_index, cfg, run_time):
    """Catalogue of every berth code seen, so the repair list can be verified."""
    prefixes = cfg["repair_berth_prefixes"]
    lines = [f"# Berth codes seen — {iso(run_time)}", ""]
    lines.append(f"Prefixes currently treated as repair berths: {', '.join(prefixes)}")
    lines.append("")
    lines.append("| Berth | Treated as repair | Vessels seen there |")
    lines.append("|---|---|---|")
    for code in sorted(berth_index):
        mark = "YES" if is_repair_berth(code, prefixes) else ""
        names = ", ".join(sorted(set(berth_index[code]))[:6])
        lines.append(f"| {code} | {mark} | {names} |")
    (BASE / "berths.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- entry point

def main():
    cfg = load_json(CONFIG_FILE, None)
    if cfg is None:
        sys.exit("config.json is missing")

    state = load_json(STATE_FILE, {})
    run_time = now()

    try:
        vessels = fetch(cfg["source_url"])
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
        sys.exit(f"could not read the port endpoint: {exc}")

    if not isinstance(vessels, list) or not vessels:
        sys.exit("the port endpoint returned nothing usable")

    print(f"received {len(vessels)} vessel records")

    state, events, berth_index, active, skipped = process(vessels, state, cfg, run_time)

    print(f"vessels currently in port after filtering: {len(active)}")
    print(f"skipped as too small: {len(skipped)}")
    if skipped:
        print("  " + "; ".join(skipped[:10]))
    print(f"distinct berth codes seen: {len(berth_index)}")

    for row in events:
        print(f"EVENT {row['event']}: {row['vessel_name']} IMO {row['imo'] or '?'} "
              f"berths {row['berths']} {row['days_alongside']} days")
    print(f"new events: {len(events)}")

    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    write_current(active, run_time)
    write_berths(berth_index, cfg, run_time)
    if events:
        append_events(events)


if __name__ == "__main__":
    main()
