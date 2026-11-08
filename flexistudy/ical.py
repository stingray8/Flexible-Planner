from __future__ import annotations

import re
import ssl
import subprocess
import sys
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import certifi
except ImportError:  # The system trust store remains the secure fallback.
    certifi = None

try:
    import truststore
except ImportError:
    truststore = None


def _unfold(text: str) -> list[str]:
    lines = text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    for line in lines:
        if line.startswith((" ", "\t")) and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _parse_dt(value: str, params: str = "") -> datetime:
    value = value.strip()
    if re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value, "%Y%m%d").astimezone()
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone()
    parsed = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    match = re.search(r"(?:^|;)TZID=([^;:]+)", params)
    if match:
        try:
            # Keep the named zone attached so weekly recurrences cross daylight
            # saving boundaries at the same wall-clock class time.
            return parsed.replace(tzinfo=ZoneInfo(match.group(1)))
        except ZoneInfoNotFoundError:
            pass
    return parsed.astimezone()


def _first(fields: dict[str, list[tuple[str, str]]], key: str, default: tuple[str, str] | None = None) -> tuple[str, str]:
    values = fields.get(key)
    if values:
        return values[0]
    if default is None:
        raise KeyError(key)
    return default


def _weekly_starts(fields: dict[str, list[tuple[str, str]]], start: datetime) -> list[datetime]:
    rule_value = _first(fields, "RRULE", ("", ""))[0]
    parts = dict(part.split("=", 1) for part in rule_value.split(";") if "=" in part)
    if parts.get("FREQ") != "WEEKLY":
        return [start]
    day_numbers = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
    weekdays = [day_numbers[x] for x in parts.get("BYDAY", "").split(",") if x in day_numbers] or [start.weekday()]
    until = _parse_dt(parts["UNTIL"]) if parts.get("UNTIL") else start + timedelta(days=370)
    until_local = until.astimezone(start.tzinfo)
    exceptions = {_parse_dt(value, params).astimezone(start.tzinfo).replace(tzinfo=None) for value, params in fields.get("EXDATE", [])}
    cursor = start.replace(hour=0, minute=0, second=0, microsecond=0)
    output: list[datetime] = []
    while cursor <= until_local and len(output) < 1000:
        if cursor.weekday() in weekdays:
            occurrence = cursor.replace(hour=start.hour, minute=start.minute, second=start.second)
            if occurrence >= start and occurrence <= until_local and occurrence.replace(tzinfo=None) not in exceptions:
                output.append(occurrence)
        cursor += timedelta(days=1)
    return output


def parse_ics(text: str, feed: dict[str, Any]) -> list[dict[str, Any]]:
    raw_events: list[dict[str, list[tuple[str, str]]]] = []
    current: dict[str, list[tuple[str, str]]] | None = None
    timezone_match = re.search(r"^(?:X-WR-TIMEZONE|X-LIC-LOCATION|TZID):([^\r\n]+)", text, re.MULTILINE)
    calendar_timezone = timezone_match.group(1).strip() if timezone_match else ""
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT" and current is not None:
            raw_events.append(current)
            current = None
        elif current is not None and ":" in line:
            keypart, value = line.split(":", 1)
            pieces = keypart.split(";", 1)
            current.setdefault(pieces[0], []).append((value, pieces[1] if len(pieces) > 1 else ""))

    # University exports often use floating DTSTART values while also shipping
    # one VTIMEZONE. Attach that named zone so recurrences retain wall time over DST.
    if calendar_timezone:
        for fields in raw_events:
            for key in ("DTSTART", "DTEND", "EXDATE", "RECURRENCE-ID"):
                if key in fields:
                    fields[key] = [
                        (value, params if "TZID=" in params or value.endswith("Z") or "T" not in value else f"{params};TZID={calendar_timezone}".strip(";"))
                        for value, params in fields[key]
                    ]

    replaced: set[tuple[str, str]] = set()
    for fields in raw_events:
        if "RECURRENCE-ID" in fields:
            uid = _first(fields, "UID", ("", ""))[0]
            recurrence = _parse_dt(*_first(fields, "RECURRENCE-ID"))
            replaced.add((uid, recurrence.isoformat(timespec="seconds")))

    events: list[dict[str, Any]] = []
    for index, fields in enumerate(raw_events):
        try:
            start = _parse_dt(*_first(fields, "DTSTART"))
            all_day = "VALUE=DATE" in _first(fields, "DTSTART")[1]
            default_end = ((start + timedelta(hours=1)).strftime("%Y%m%dT%H%M%S"), "")
            end = _parse_dt(*_first(fields, "DTEND", default_end))
            duration = end - start
            uid = _first(fields, "UID", (f"{feed['id']}-{index}", ""))[0]
            title = _first(fields, "SUMMARY", ("Calendar event", ""))[0].replace("\\,", ",")
            category = _first(fields, "CATEGORIES", (feed.get("name", "Calendar"), ""))[0].split(",", 1)[0].replace("\\,", ",")
            starts = [start] if "RECURRENCE-ID" in fields else _weekly_starts(fields, start)
            for occurrence in starts:
                occurrence_key = occurrence.isoformat(timespec="seconds")
                if (uid, occurrence_key) in replaced and "RECURRENCE-ID" not in fields:
                    continue
                recurring = "RRULE" in fields or "RECURRENCE-ID" in fields
                suffix = f"-{occurrence.strftime('%Y%m%dT%H%M%S')}" if recurring else ""
                events.append({
                    "id": f"ical-{feed['id']}-{uid}{suffix}", "title": title,
                    "start": occurrence.isoformat(timespec="seconds"), "end": (occurrence + duration).isoformat(timespec="seconds"),
                    "source": feed.get("tag", feed.get("name", "Calendar")), "feed_id": feed["id"],
                    "enforcement": feed.get("enforcement", "hard"), "calendar_name": category or feed.get("name", "Calendar"),
                    "kind": feed.get("kind", "calendar"), "all_day": all_day, "primary": False,
                })
        except (KeyError, ValueError):
            pass
    return events


def fetch_feed(feed: dict[str, Any], timeout: int = 8) -> list[dict[str, Any]]:
    url = feed["url"]
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    request = urllib.request.Request(url, headers={"User-Agent": "FlexiStudy-Local/1.0"})
    # Prefer the operating system's native trust store (macOS Keychain on a
    # Mac). This handles Apple CalDAV chains and managed-device roots. The
    # fallback merges Python's system roots with Certifi's maintained bundle.
    # Both paths retain certificate and hostname verification.
    if truststore:
        context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    else:
        context = ssl.create_default_context()
        if certifi:
            context.load_verify_locations(cafile=certifi.where())
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            return parse_ics(response.read().decode("utf-8", errors="replace"), feed)
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        is_certificate_error = isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(exc)
        if not is_certificate_error or sys.platform != "darwin" or not Path("/usr/bin/curl").exists():
            raise
        return _fetch_with_macos_curl(url, feed, timeout, exc)


def _fetch_with_macos_curl(url: str, feed: dict[str, Any], timeout: int, original_error: Exception) -> list[dict[str, Any]]:
    """Use macOS system TLS when Python's separately bundled OpenSSL rejects a chain.

    The URL is supplied through curl's stdin config so private calendar tokens do
    not appear in the process list. Certificate and hostname checks stay enabled.
    """
    if any(char in url for char in "\r\n\0"):
        raise ValueError("Calendar URL contains invalid control characters")
    escaped_url = url.replace("\\", "\\\\").replace('"', '\\"')
    config = (
        f'url = "{escaped_url}"\n'
        "location\n"
        "silent\n"
        "show-error\n"
        "fail\n"
        f"max-time = {max(3, int(timeout))}\n"
        "max-filesize = 26214400\n"
        'user-agent = "FlexiStudy-Local/1.0"\n'
    )
    try:
        result = subprocess.run(
            ["/usr/bin/curl", "--config", "-"], input=config.encode(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout + 3, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise urllib.error.URLError(f"Native macOS calendar fetch failed: {exc}") from original_error
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise urllib.error.URLError(f"Python TLS and macOS TLS both rejected the calendar: {detail}") from original_error
    return parse_ics(result.stdout.decode("utf-8", errors="replace"), feed)

