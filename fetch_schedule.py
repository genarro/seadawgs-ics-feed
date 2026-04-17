#!/usr/bin/env python3
"""Fetch the NWIBL Sea Dogs (30 and older Weeknights) schedule and emit an .ics feed.

The NWIBL site is powered by TeamLinkt. The schedule on /nwibl/Schedule is rendered
client-side by a DataTable that POSTs to /leagues/getAllEvents/35370 and gets back
JSON whose row cells are HTML fragments. We call that endpoint directly, then use
BeautifulSoup to pull team names, the venue name, the lat/long, and the event id
out of the embedded HTML.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

LOCAL_TZ = ZoneInfo("America/Los_Angeles")

# --- Site-specific constants ---------------------------------------------------

ASSOCIATION_ID = 35370          # NWIBL on TeamLinkt
SEASON_ID = 51887               # 2026 Season
HIERARCHY_ID = 276411           # 30 and older (Weeknights) division
TEAM_ID = 807525                # Sea Dogs
TEAM_NAME = "Sea Dogs"

ENDPOINT = f"https://nwibl.org/leagues/getAllEvents/{ASSOCIATION_ID}"
REFERER = "https://nwibl.org/nwibl/Schedule"

# --- Helpers -------------------------------------------------------------------

EVENT_ID_RE = re.compile(r"/event/\d+/(\d+)")
LATLON_RE = re.compile(r"q=(-?\d+\.\d+),(-?\d+\.\d+)")
TIME_RANGE_RE = re.compile(
    r"(\d{1,2}:\d{2}\s*[AP]M)\s*-\s*(\d{1,2}:\d{2}\s*[AP]M)", re.I
)


def fetch_rows() -> list[dict]:
    """POST to the TeamLinkt DataTable endpoint and return the row list."""
    payload = {
        "team_id": TEAM_ID,
        "season_id": SEASON_ID,
        "type": "schedule",
        "is_league_site": 1,
        "show_team_links": 1,
        "show_games_only": 1,
        "schedule_type": "regular_season",
        "length": 500,
        "start": 0,
        "status": "upcoming",
        "filters[]": HIERARCHY_ID,
    }
    headers = {
        "User-Agent": "nwibl-ics-feed/1.0 (+https://nwibl.org/nwibl/Schedule)",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": REFERER,
    }
    r = requests.post(ENDPOINT, data=payload, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json().get("data", [])


def parse_row(row: dict) -> dict | None:
    """Turn one JSON row into a dict ready for ICS emission.

    The JSON also includes a unix timestamp at row["6"], but TeamLinkt encodes
    local wall-clock as PST without DST awareness, so during DST the timestamp is
    one hour earlier than what the site actually shows. We trust the displayed
    date and time strings ("Wed Apr 22, 2026" + "7:30 PM - 10:30 PM") and treat
    them as America/Los_Angeles.
    """
    date_str = (row.get("0") or "").strip()
    time_str = (row.get("1") or "").strip()
    if not date_str or not time_str:
        return None

    m = TIME_RANGE_RE.search(time_str)
    if not m:
        return None

    try:
        date_part = datetime.strptime(date_str, "%a %b %d, %Y").date()
        start_t = datetime.strptime(m.group(1).strip(), "%I:%M %p").time()
        end_t = datetime.strptime(m.group(2).strip(), "%I:%M %p").time()
    except ValueError:
        return None

    start = datetime.combine(date_part, start_t, tzinfo=LOCAL_TZ)
    end = datetime.combine(date_part, end_t, tzinfo=LOCAL_TZ)
    if end <= start:
        end = start + timedelta(hours=3)

    home = BeautifulSoup(row.get("3", "") or "", "html.parser").get_text(strip=True)
    away = BeautifulSoup(row.get("4", "") or "", "html.parser").get_text(strip=True)

    loc_html = row.get("5", "") or ""
    loc_soup = BeautifulSoup(loc_html, "html.parser")
    location = loc_soup.get_text(strip=True)
    geo = None
    a = loc_soup.find("a", href=True)
    if a:
        gm = LATLON_RE.search(a["href"])
        if gm:
            geo = (float(gm.group(1)), float(gm.group(2)))

    type_html = row.get("2", "") or ""
    type_soup = BeautifulSoup(type_html, "html.parser")
    event_url = None
    event_id = None
    link = type_soup.find("a", href=True)
    if link:
        event_url = link["href"]
        em = EVENT_ID_RE.search(event_url)
        if em:
            event_id = em.group(1)

    is_home = TEAM_NAME.lower() in home.lower()
    opponent = away if is_home else home
    summary = f"{TEAM_NAME} {'vs' if is_home else '@'} {opponent}".strip()

    return {
        "start": start,
        "end": end,
        "summary": summary,
        "location": location,
        "geo": geo,
        "event_url": event_url,
        "event_id": event_id,
        "home": home,
        "away": away,
    }


# --- ICS emission --------------------------------------------------------------

def _ics_escape(s: str) -> str:
    return (
        s.replace("\\", "\\\\")
         .replace(";", r"\;")
         .replace(",", r"\,")
         .replace("\n", r"\n")
    )


def _fold(line: str) -> str:
    """RFC 5545 line folding at 75 octets."""
    out = []
    raw = line.encode("utf-8")
    while len(raw) > 75:
        out.append(raw[:75].decode("utf-8", errors="ignore"))
        raw = b" " + raw[75:]
    out.append(raw.decode("utf-8", errors="ignore"))
    return "\r\n".join(out)


def _fmt_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_ics(events: list[dict]) -> str:
    # DTSTAMP is required by RFC 5545. We deliberately use a per-event stable value
    # (the event's own start time) rather than "now", so re-running the scraper
    # produces a byte-identical file when the upstream schedule hasn't changed.
    # That keeps git diffs (and thus commits) limited to real schedule edits.
    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//nwibl-ics-feed//Sea Dogs//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:NWIBL {TEAM_NAME} Schedule",
        f"X-WR-CALDESC:NWIBL {TEAM_NAME} (30 and older Weeknights) games",
        "X-WR-TIMEZONE:America/Los_Angeles",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
    ]

    for ev in events:
        if ev["event_id"]:
            uid = f"nwibl-{ASSOCIATION_ID}-{ev['event_id']}@nwibl.org"
        else:
            seed = f"{ev['start'].isoformat()}|{ev['home']}|{ev['away']}"
            uid = "nwibl-" + hashlib.sha1(seed.encode()).hexdigest()[:16] + "@nwibl.org"

        desc_parts = [f"{ev['away']} @ {ev['home']}"]
        if ev["event_url"]:
            desc_parts.append(f"Details: {ev['event_url']}")
        description = "\n".join(desc_parts)

        block = [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{_fmt_utc(ev['start'])}",
            f"DTSTART:{_fmt_utc(ev['start'])}",
            f"DTEND:{_fmt_utc(ev['end'])}",
            f"SUMMARY:{_ics_escape(ev['summary'])}",
            f"DESCRIPTION:{_ics_escape(description)}",
        ]
        if ev["location"]:
            block.append(f"LOCATION:{_ics_escape(ev['location'])}")
        if ev["geo"]:
            block.append(f"GEO:{ev['geo'][0]:.6f};{ev['geo'][1]:.6f}")
        if ev["event_url"]:
            block.append(f"URL:{ev['event_url']}")
        block.append("END:VEVENT")
        lines.extend(block)

    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(l) for l in lines) + "\r\n"


# --- Main ----------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_out = Path(__file__).resolve().parent / "seadogs.ics"
    parser.add_argument(
        "-o", "--output", type=Path, default=default_out,
        help=f"Output .ics path (default: {default_out})",
    )
    args = parser.parse_args()

    rows = fetch_rows()
    events = [e for e in (parse_row(r) for r in rows) if e]
    if not events:
        print("WARN: no events parsed; not overwriting output", file=sys.stderr)
        return 1

    ics = build_ics(events)

    # Atomic write so the HTTP server never serves a half-written file.
    tmp = args.output.with_suffix(args.output.suffix + ".tmp")
    tmp.write_text(ics, encoding="utf-8")
    os.replace(tmp, args.output)
    print(f"Wrote {len(events)} events to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
