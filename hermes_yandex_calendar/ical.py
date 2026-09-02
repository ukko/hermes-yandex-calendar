"""Minimal iCalendar (RFC 5545) VEVENT parsing and serialization.

Deliberately dependency-free and scoped to what this plugin needs: a single
VEVENT per resource with SUMMARY / DTSTART / DTEND / LOCATION / DESCRIPTION /
UID, plus ORGANIZER / ATTENDEE / TRANSP for meeting management. It is NOT a
general iCalendar implementation — it handles line (un)folding, TEXT escaping,
and the three DTSTART/DTEND forms Yandex emits (UTC ``...Z``, ``TZID=...``
local, and ``VALUE=DATE`` all-day).

Any property it does not model (RRULE, VALARM blocks, SEQUENCE, …) is preserved
verbatim in ``Event.raw_props`` and re-emitted on build, so editing an event
never silently drops recurrence rules or alarms.

No Hermes imports here so it stays unit-testable in isolation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

try:  # zoneinfo is stdlib on >=3.9; guard so a missing tzdata never crashes parsing
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - zoneinfo effectively always present on 3.11+
    ZoneInfo = None  # type: ignore[assignment]

__all__ = ["Attendee", "Event", "build_calendar", "parse_events"]

TRANSP_BUSY = "OPAQUE"
TRANSP_FREE = "TRANSPARENT"

_MAILBOX_ATOM = r"[A-Za-z0-9!#$'*+\-=^_`{|}~]+"
_MAILBOX_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_MAILBOX = re.compile(
    rf"{_MAILBOX_ATOM}(?:\.{_MAILBOX_ATOM})*@{_MAILBOX_LABEL}(?:\.{_MAILBOX_LABEL})*"
)


@dataclass
class Attendee:
    """A meeting participant (ATTENDEE / ORGANIZER)."""

    email: str
    name: str = ""
    role: str = ""  # e.g. REQ-PARTICIPANT, OPT-PARTICIPANT, CHAIR
    partstat: str = ""  # e.g. NEEDS-ACTION, ACCEPTED, DECLINED, TENTATIVE
    rsvp: bool | None = None


@dataclass
class Event:
    """A calendar event. ``href`` is the CalDAV resource path (set by the client)."""

    uid: str
    summary: str = ""
    start: datetime | date | None = None
    end: datetime | date | None = None
    location: str = ""
    description: str = ""
    all_day: bool = False
    organizer: Attendee | None = None
    attendees: list[Attendee] = field(default_factory=list)
    transp: str = ""  # OPAQUE (busy) / TRANSPARENT (free); "" == unset
    href: str = ""
    # Verbatim, already-unfolded content lines for properties we don't model.
    raw_props: list[str] = field(default_factory=list)


# --- line handling ---------------------------------------------------------


def _unfold(text: str) -> list[str]:
    """Undo RFC 5545 line folding: a line beginning with space/tab continues the prior."""
    raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines: list[str] = []
    for line in raw:
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _fold(line: str) -> str:
    """Fold a content line to <=75 octets, continuation lines prefixed with a space."""
    if "\r" in line or "\n" in line:
        raise ValueError("iCalendar content lines must not contain a line break.")
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    out: list[str] = []
    chunk = bytearray()
    for ch in line:
        b = ch.encode("utf-8")
        # keep the multibyte char whole; 74 leaves room for the leading space on next line
        limit = 75 if not out else 74
        if len(chunk) + len(b) > limit:
            out.append(chunk.decode("utf-8"))
            chunk = bytearray()
        chunk += b
    out.append(chunk.decode("utf-8"))
    return "\r\n ".join(out)


def _unescape(value: str) -> str:
    result: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            result.append({"n": "\n", "N": "\n", ",": ",", ";": ";", "\\": "\\"}.get(nxt, nxt))
            i += 2
            continue
        result.append(ch)
        i += 1
    return "".join(result)


def _escape(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")


def _split_prop(line: str) -> tuple[str, dict[str, str], str]:
    """Return (NAME, params, value) for a content line like ``DTSTART;TZID=X:val``."""
    name_part, _, value = line.partition(":")
    pieces = name_part.split(";")
    name = pieces[0].upper()
    params: dict[str, str] = {}
    for piece in pieces[1:]:
        key, _, val = piece.partition("=")
        params[key.upper()] = val.strip('"')
    return name, params, value


def _param_value(value: str) -> str:
    """Quote a parameter value if it contains characters that require it (RFC 5545)."""
    if any(c in value for c in ',;:"'):
        return '"' + value.replace('"', "") + '"'
    return value


def _parse_cal_address(value: str, params: dict[str, str]) -> Attendee:
    """Parse an ORGANIZER/ATTENDEE value (``mailto:x@y``) plus its parameters."""
    email = value.strip()
    if email.lower().startswith("mailto:"):
        email = email[len("mailto:") :]
    rsvp_raw = params.get("RSVP", "").upper()
    rsvp = True if rsvp_raw == "TRUE" else False if rsvp_raw == "FALSE" else None
    return Attendee(
        email=email,
        name=params.get("CN", ""),
        role=params.get("ROLE", ""),
        partstat=params.get("PARTSTAT", ""),
        rsvp=rsvp,
    )


def _validate_cal_address(attendee: Attendee) -> None:
    values = (attendee.email, attendee.name, attendee.role, attendee.partstat)
    if any("\r" in value or "\n" in value for value in values):
        raise ValueError("Calendar addresses must not contain a line break.")
    if _MAILBOX.fullmatch(attendee.email) is None:
        raise ValueError(f"Invalid calendar email address: {attendee.email!r}.")


def _format_cal_address(prop: str, attendee: Attendee) -> str:
    """Serialize an ATTENDEE/ORGANIZER content line."""
    _validate_cal_address(attendee)
    parts = [prop]
    if attendee.name:
        parts.append(f"CN={_param_value(attendee.name)}")
    if attendee.role:
        parts.append(f"ROLE={_param_value(attendee.role)}")
    if attendee.partstat:
        parts.append(f"PARTSTAT={_param_value(attendee.partstat)}")
    if attendee.rsvp is not None:
        parts.append(f"RSVP={'TRUE' if attendee.rsvp else 'FALSE'}")
    return ";".join(parts) + f":mailto:{attendee.email}"


# --- datetime handling -----------------------------------------------------


def _localize(dt: datetime, tzid: str | None) -> datetime:
    """Attach the TZID's zone, or leave the value floating if it is unusable."""
    if not tzid or ZoneInfo is None:
        return dt
    try:
        return dt.replace(tzinfo=ZoneInfo(tzid))
    except Exception:  # unknown zone -> keep naive rather than fail the parse
        return dt


def _parse_dt(value: str, params: dict[str, str]) -> tuple[datetime | date | None, bool]:
    """Parse a DTSTART/DTEND value. Returns (value, is_all_day)."""
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or (len(value) == 8 and "T" not in value):
        try:
            return date(int(value[0:4]), int(value[4:6]), int(value[6:8])), True
        except ValueError:
            return None, True
    is_utc = value.endswith("Z")
    try:
        dt = datetime.strptime(value[:-1] if is_utc else value, "%Y%m%dT%H%M%S")
    except ValueError:
        return None, False
    if is_utc:
        return dt.replace(tzinfo=UTC), False
    return _localize(dt, params.get("TZID")), False


def _format_dt(value: datetime | date, all_day: bool) -> tuple[str, str]:
    """Return (param_suffix, formatted_value) for a DTSTART/DTEND line."""
    if all_day or (isinstance(value, date) and not isinstance(value, datetime)):
        return ";VALUE=DATE", f"{value.year:04d}{value.month:02d}{value.day:02d}"
    dt: datetime = value  # type: ignore[assignment]
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC)
        return "", dt.strftime("%Y%m%dT%H%M%SZ")
    # naive == floating local time; emit without Z
    return "", dt.strftime("%Y%m%dT%H%M%S")


# --- public API ------------------------------------------------------------

_TEXT_PROPS = {"SUMMARY": "summary", "LOCATION": "location", "DESCRIPTION": "description"}


def _apply_property(event: Event, line: str) -> None:
    """Fold one VEVENT content line into ``event``.

    Anything this model does not cover (RRULE, SEQUENCE, X- extensions, …) is kept
    verbatim in ``raw_props`` so it survives a parse/build round-trip.
    """
    name, params, value = _split_prop(line)
    if name == "UID":
        event.uid = _unescape(value)
    elif name in _TEXT_PROPS:
        setattr(event, _TEXT_PROPS[name], _unescape(value))
    elif name == "DTSTART":
        event.start, event.all_day = _parse_dt(value, params)
    elif name == "DTEND":
        end, all_day = _parse_dt(value, params)
        event.end = end
        event.all_day = event.all_day or all_day
    elif name == "TRANSP":
        event.transp = value.strip().upper()
    elif name == "ORGANIZER":
        event.organizer = _parse_cal_address(value, params)
    elif name == "ATTENDEE":
        event.attendees.append(_parse_cal_address(value, params))
    else:
        event.raw_props.append(line)


def parse_events(text: str) -> list[Event]:
    """Parse every VEVENT found in an iCalendar document."""
    events: list[Event] = []
    current: Event | None = None
    nested: list[str] = []  # open sub-components inside the VEVENT (VALARM, …)
    for line in _unfold(text):
        upper = line.upper()
        if current is None:
            if upper.startswith("BEGIN:VEVENT"):
                current = Event(uid="")
        elif nested:
            # A sub-component has its own SUMMARY/DESCRIPTION/TRIGGER, which must
            # not be read as the event's. Keep the block verbatim instead.
            current.raw_props.append(line)
            if upper.startswith("END:") and upper[4:].strip() == nested[-1]:
                nested.pop()
        elif upper.startswith("BEGIN:"):
            nested.append(upper[len("BEGIN:") :].strip())
            current.raw_props.append(line)
        elif upper.startswith("END:VEVENT"):
            events.append(current)
            current = None
        elif line:
            _apply_property(current, line)
    return events


def build_calendar(
    event: Event,
    *,
    prodid: str = "-//hermes-yandex-calendar//EN",
    dtstamp: datetime | None = None,
) -> str:
    """Serialize a single Event into a VCALENDAR document (CRLF-terminated).

    ``dtstamp`` (if given and the event carries no DTSTAMP of its own) is emitted
    as the required RFC 5545 DTSTAMP; the client passes ``now`` on create/update.
    """
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{prodid}",
        "BEGIN:VEVENT",
        f"UID:{_escape(event.uid)}",
        *_event_lines(event, dtstamp),
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def _event_lines(event: Event, dtstamp: datetime | None) -> list[str]:
    """The VEVENT body: modelled properties first, then everything kept verbatim."""
    lines: list[str] = []
    has_dtstamp = any(p.upper().startswith("DTSTAMP") for p in event.raw_props)
    if dtstamp is not None and not has_dtstamp:
        lines.append(f"DTSTAMP:{dtstamp.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}")
    for prop, value in (
        ("SUMMARY", event.summary),
        ("TRANSP", event.transp),
        ("LOCATION", event.location),
        ("DESCRIPTION", event.description),
    ):
        if value:
            # TRANSP carries an enum token, so escaping is a no-op for it.
            lines.append(f"{prop}:{_escape(value)}")
    for prop, moment in (("DTSTART", event.start), ("DTEND", event.end)):
        if moment is not None:
            suffix, val = _format_dt(moment, event.all_day)
            lines.append(f"{prop}{suffix}:{val}")
    if event.organizer is not None:
        lines.append(_format_cal_address("ORGANIZER", event.organizer))
    lines.extend(_format_cal_address("ATTENDEE", a) for a in event.attendees)
    lines.extend(event.raw_props)
    return lines
