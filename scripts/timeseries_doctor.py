"""
Offline diagnosis for a timeseries payload — the "Test connection" button as a CLI.

The reports in discussion #214 are about *someone's* payload: the reporter
cannot run pytest against their sensor, and no hand-written fixture is the one
they have. This tool takes their pasted file (or fetches their URL), prints
where every entry actually lands on the optimization grid, and renders the
same verdict the web UI's connection test renders — the output is made to be
pasted back into a support thread.

It imports the normalizer, the grid placement and the probe's `analyse` and
reimplements nothing: if the doctor and the "Test connection" button ever
disagreed about a payload, one of them would be lying.

Usage::

    .venv/bin/python scripts/timeseries_doctor.py \\
        --domain price --unit EUR/kWh --tz Europe/Berlin --time-frame 3600 \\
        --file payload.json --path attributes.data

    .venv/bin/python scripts/timeseries_doctor.py \\
        --domain pv --url http://ha.local/api/states/sensor.pv --token $TOK \\
        --today 2026-10-25        # play through a DST day without waiting for it

Exit code 0 when the verdict is ok, 1 otherwise.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

import pytz
import requests

# Add project root to path so we can import from src (same bootstrap as
# export_config_schema.py).
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

# pylint: disable=wrong-import-position
from src.config_web.timeseries_probe import DOMAINS, analyse  # noqa: E402
from src.interfaces.http_fetch import bearer_request_kwargs  # noqa: E402
from src.interfaces.timeseries_normalizer import (  # noqa: E402
    TimeseriesFormatError,
    extract_json_path,
    normalize_entries,
    resolve_field_names,
)
from src.timeseries_grid import (  # noqa: E402
    KIND_EXTENSIVE,
    KIND_INTENSIVE,
    TimeseriesGridError,
    horizon_slots,
    local_midnight,
    place_on_grid,
)

# How a domain's values sit on the grid; everything else about a domain comes
# from the probe's own table.
_PLACEMENT_KIND = {
    "price": KIND_INTENSIVE,
    "feed_in": KIND_INTENSIVE,
    "pv": KIND_EXTENSIVE,
}


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Diagnose a timeseries payload offline, with the probe's verdict."
    )
    parser.add_argument(
        "--domain", choices=sorted(DOMAINS), default="price",
        help="which consumer the payload is meant for (default: price)",
    )
    parser.add_argument(
        "--unit", default=None,
        help="unit of the 'value' field (default: the domain's default)",
    )
    parser.add_argument("--tz", default="UTC", help="IANA zone name (default: UTC)")
    parser.add_argument(
        "--time-frame", type=int, choices=(900, 3600), default=3600,
        help="system slot length in seconds (default: 3600)",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="path to a pasted JSON payload")
    source.add_argument("--url", help="endpoint to fetch (HA state or HTTP server)")
    parser.add_argument("--token", default="", help="bearer token for --url")
    parser.add_argument(
        "--path", default="attributes.data",
        help="JSON path to the array inside the payload (default: attributes.data)",
    )
    parser.add_argument(
        "--today", default=None,
        help="anchor date YYYY-MM-DD (default: today) — lets a DST day be "
        "played through without waiting for it",
    )
    return parser.parse_args(argv)


def _load_payload(args):
    """The decoded payload, from the file or the URL."""
    if args.file:
        with open(args.file, encoding="utf-8") as handle:
            return json.load(handle)
    response = requests.get(args.url, **bearer_request_kwargs(args.token))
    response.raise_for_status()
    return response.json()


def _anchor(args, tz):
    if args.today:
        day = datetime.strptime(args.today, "%Y-%m-%d")
        if hasattr(tz, "localize"):
            return tz.localize(day)
        return day.replace(tzinfo=tz)
    return local_midnight(tz)


def _print_form(raw_entries, entries, verdict):
    print("== Form ==")
    print(f"entries:            {len(raw_entries)}")
    spellings = resolve_field_names(raw_entries[0] if raw_entries else {})
    rendered = ", ".join(f"{k}={v!r}" for k, v in sorted(spellings.items()))
    print(f"field spellings:    {rendered or 'none recognised'}")
    if entries:
        print(f"first entry starts: {entries[0]['start'].isoformat()}")
        print(f"last entry starts:  {entries[-1]['start'].isoformat()}")
        deltas = [
            int((later["start"] - earlier["start"]).total_seconds())
            for earlier, later in zip(entries, entries[1:])
        ]
        if deltas:
            changes = [
                index + 1
                for index, delta in enumerate(deltas)
                if delta != deltas[0]
            ]
            if changes:
                print(
                    f"spacing changes:    first at entry {changes[0]} "
                    f"({deltas[0]}s -> {deltas[changes[0] - 1]}s)"
                )
    if verdict.get("resolution_seconds"):
        print(f"resolution:         {verdict['resolution_seconds']}s")
    print()


def _print_landing(entries, domain, anchor, time_frame, tz):
    """
    Where every entry lands — the table a support thread needs.

    ``tz`` must be the zone object itself, not ``anchor.tzinfo``: pytz hands
    out fixed-offset instances, and rendering slot starts through one would
    show the pre-transition offset all the way across a DST change.
    """
    spec = DOMAINS[domain]
    slot_count = horizon_slots(time_frame)
    placement = place_on_grid(
        entries, anchor, slot_count, time_frame, _PLACEMENT_KIND[domain]
    )

    print("== Landing ==")
    print(f"anchor (slot 0):    {anchor.isoformat()}")
    print(
        f"{'slot':>4}  {'local time':<25} {'coverage':>8}  "
        f"{'value':>12}  marker"
    )
    for index, (value, coverage) in enumerate(
        zip(placement.values, placement.coverage)
    ):
        slot_start = (
            anchor + timedelta(seconds=index * time_frame)
        ).astimezone(tz)
        if coverage >= 1.0:
            marker = "real"
        elif coverage > 0:
            marker = "partial"
        else:
            marker = "gap"
        shown = round(value * spec.display_factor, spec.display_round)
        print(
            f"{index:>4}  {slot_start.isoformat():<25} {coverage:>8.2f}  "
            f"{shown:>12}  {marker} {spec.display_unit if coverage > 0 else ''}"
        )
    print()
    print("== Coverage ==")
    covered = [i for i, c in enumerate(placement.coverage) if c > 0]
    if covered:
        print(
            f"covered slots:      {len(covered)} of {slot_count} "
            f"(first {covered[0]}, last {covered[-1]})"
        )
    else:
        print(f"covered slots:      0 of {slot_count}")
    print(
        f"entries dropped:    {placement.dropped} of {placement.source_count} "
        "(outside the window)"
    )
    print()


def main(argv=None):
    """Run the diagnosis; returns the process exit code."""
    args = _parse_args(argv)
    spec = DOMAINS[args.domain]
    unit = args.unit or ("W" if args.domain == "pv" else "EUR/kWh")

    try:
        tz = pytz.timezone(args.tz)
    except pytz.UnknownTimeZoneError:
        print(f"unknown timezone {args.tz!r}", file=sys.stderr)
        return 1

    try:
        payload = _load_payload(args)
    except (OSError, ValueError, requests.RequestException) as exc:
        print(f"could not load the payload: {exc}", file=sys.stderr)
        return 1

    raw_entries = payload
    if not isinstance(payload, list):
        raw_entries = extract_json_path(payload, args.path, label="Doctor")
        if raw_entries is None:
            print(
                f"path {args.path!r} does not exist in the payload", file=sys.stderr
            )
            return 1

    anchor = _anchor(args, tz)

    # The verdict — the exact code and texts behind the "Test connection"
    # button, so this tool can never disagree with the web UI.
    verdict = analyse(
        raw_entries,
        args.domain,
        unit,
        tz,
        time_frame_base=args.time_frame,
        installed_power_w=0,
        anchor=anchor,
    )

    entries = None
    try:
        entries = normalize_entries(list(raw_entries), tz, label="Doctor")
        spec.convert(entries, unit)
    except (TimeseriesFormatError, TypeError):
        entries = None

    _print_form(raw_entries, entries, verdict)
    if entries:
        try:
            _print_landing(entries, args.domain, anchor, args.time_frame, tz)
        except TimeseriesGridError:
            pass  # the verdict below carries the message

    print("== Verdict ==")
    if verdict["ok"]:
        print("OK")
    else:
        print(f"FAIL ({verdict.get('field', '-')}): {verdict.get('error')}")
    for warning in verdict.get("warnings", []):
        print(f"warning: {warning}")

    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
