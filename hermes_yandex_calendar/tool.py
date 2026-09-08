"""Standalone Hermes tools for Yandex Calendar.

Each handler has the signature ``handle(args: dict, **kwargs) -> str``, returns
a JSON string, and NEVER raises — every failure path becomes
``{"error": "..."}`` so the agent gets a usable message instead of a crash.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .caldav import CalDAVError
from .config import MissingCredentials, build_client
from .ical import TRANSP_BUSY, TRANSP_FREE, Attendee, Event

TOOLSET = "yandex_calendar"

# -- schemas ----------------------------------------------------------------

_DATETIME_HINT = (
    "ISO 8601 date or datetime, e.g. '2026-07-25' or '2026-07-25T14:00:00+03:00'. "
    "A datetime without an offset is treated as UTC."
)
_CALENDAR_HINT = (
    "Target calendar: its name (as shown by yandex_calendar_list_calendars) or href. "
    "Omit to use the default (first) calendar."
)
# Plain strings only: a union item type ("string" or "object") is rejected by
# strict function-calling validators. Handlers still accept objects if a model
# sends them anyway.
_ATTENDEE_HINT = "Each entry is an email ('a@x.ru') or a 'Name <a@x.ru>' string."
_MAILBOX_ATOM = r"[A-Za-z0-9!#$'*+\-=^_`{|}~]+"
_MAILBOX_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_MAILBOX = re.compile(
    rf"{_MAILBOX_ATOM}(?:\.{_MAILBOX_ATOM})*@{_MAILBOX_LABEL}(?:\.{_MAILBOX_LABEL})*"
)

LIST_CALENDARS_SCHEMA: dict[str, Any] = {
    "name": "yandex_calendar_list_calendars",
    "description": (
        "List the Yandex calendars this plugin can use (name + href). Use a returned "
        "name or href as the 'calendar' argument of the other tools."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

LIST_SCHEMA: dict[str, Any] = {
    "name": "yandex_calendar_list_events",
    "description": (
        "List events from the user's Yandex Calendar within a time range. "
        "Returns each event's summary, start/end, location, description, attendees, "
        "busy status, and href (the href is needed to update or delete an event)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "start": {
                "type": "string",
                "description": f"Range start ({_DATETIME_HINT}). Defaults to now.",
            },
            "end": {
                "type": "string",
                "description": f"Range end ({_DATETIME_HINT}). Defaults to 7 days after start.",
            },
            "calendar": {"type": "string", "description": _CALENDAR_HINT},
        },
        "required": [],
    },
}

CREATE_SCHEMA: dict[str, Any] = {
    "name": "yandex_calendar_create_event",
    "description": "Create a new event in the user's Yandex Calendar.",
    "parameters": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Event title."},
            "start": {"type": "string", "description": f"Event start ({_DATETIME_HINT})."},
            "end": {
                "type": "string",
                "description": f"Event end ({_DATETIME_HINT}). Defaults to 1 hour after start.",
            },
            "location": {"type": "string", "description": "Optional location."},
            "description": {"type": "string", "description": "Optional description / notes."},
            "all_day": {"type": "boolean", "description": "If true, create an all-day event."},
            "calendar": {"type": "string", "description": _CALENDAR_HINT},
            "attendees": {
                "type": "array",
                "items": {"type": "string"},
                "description": f"Optional attendees to invite. {_ATTENDEE_HINT}",
            },
            "busy": {
                "type": "boolean",
                "description": "Show the time as busy (true, default) or free (false).",
            },
        },
        "required": ["summary", "start"],
    },
}

UPDATE_SCHEMA: dict[str, Any] = {
    "name": "yandex_calendar_update_event",
    "description": (
        "Update an existing event (identified by its href from "
        "yandex_calendar_list_events). Only the fields you provide are changed; "
        "attendees are added/removed incrementally and unrelated properties "
        "(recurrence, alarms) are preserved."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "event_href": {"type": "string", "description": "The event resource href to update."},
            "summary": {"type": "string", "description": "New title."},
            "start": {"type": "string", "description": f"New start ({_DATETIME_HINT})."},
            "end": {"type": "string", "description": f"New end ({_DATETIME_HINT})."},
            "location": {"type": "string", "description": "New location."},
            "description": {"type": "string", "description": "New description / notes."},
            "all_day": {"type": "boolean", "description": "Mark as an all-day event."},
            "busy": {
                "type": "boolean",
                "description": "Change busy status: true = busy, false = free.",
            },
            "add_attendees": {
                "type": "array",
                "items": {"type": "string"},
                "description": f"Attendees to add. {_ATTENDEE_HINT}",
            },
            "remove_attendees": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Emails of attendees to remove.",
            },
        },
        "required": ["event_href"],
    },
}

RESPOND_SCHEMA: dict[str, Any] = {
    "name": "yandex_calendar_respond_event",
    "description": (
        "Respond to a meeting invitation: accept, decline, or tentatively accept it. "
        "Sets the account owner's participation status on the event identified by its href."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "event_href": {"type": "string", "description": "The event resource href."},
            "response": {
                "type": "string",
                "enum": ["accept", "decline", "tentative"],
                "description": "How to respond to the invitation.",
            },
        },
        "required": ["event_href", "response"],
    },
}

MOVE_SCHEMA: dict[str, Any] = {
    "name": "yandex_calendar_move_event",
    "description": (
        "Move an event to a different calendar. The event keeps its details, attendees, "
        "recurrence, and alarms."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "event_href": {"type": "string", "description": "The event resource href to move."},
            "calendar": {
                "type": "string",
                "description": (
                    "Destination calendar: its name (from yandex_calendar_list_calendars) or href."
                ),
            },
        },
        "required": ["event_href", "calendar"],
    },
}

DELETE_SCHEMA: dict[str, Any] = {
    "name": "yandex_calendar_delete_event",
    "description": (
        "Delete an event from the user's Yandex Calendar by its href "
        "(obtain the href from yandex_calendar_list_events). The href identifies "
        "both the event and the calendar it lives in."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "event_href": {"type": "string", "description": "The event resource href to delete."},
        },
        "required": ["event_href"],
    },
}


# -- parsing / serialization ------------------------------------------------


def _parse_dt(value: str) -> datetime | date:
    """Parse an ISO 8601 date or datetime. Naive datetimes are assumed UTC."""
    value = value.strip()
    try:
        if len(value) == 10 and value.count("-") == 2:
            return date.fromisoformat(value)
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError(f"Invalid date/time {value!r}: {exc}") from exc


def _parse_attendee(item: Any) -> Attendee:
    """Accept an email string, a 'Name <email>' string, or an attendee object."""
    if isinstance(item, dict):
        email = str(item.get("email", "")).strip()
        if not email:
            raise ValueError("Attendee object is missing 'email'.")
        attendee = Attendee(
            email=email,
            name=str(item.get("name", "")).strip(),
            role=str(item.get("role", "")).strip(),
            partstat=str(item.get("partstat", "")).strip(),
        )
        _validate_input_attendee(attendee)
        return attendee
    text = str(item).strip()
    if "<" in text and ">" in text:
        name = text[: text.index("<")].strip()
        email = text[text.index("<") + 1 : text.index(">")].strip()
        attendee = Attendee(email=email, name=name)
        _validate_input_attendee(attendee)
        return attendee
    if not text:
        raise ValueError("Attendee email is empty.")
    attendee = Attendee(email=text.removeprefix("mailto:"))
    _validate_input_attendee(attendee)
    return attendee


def _validate_input_attendee(attendee: Attendee) -> None:
    values = (attendee.email, attendee.name, attendee.role, attendee.partstat)
    if any("\r" in value or "\n" in value for value in values):
        raise ValueError("Calendar addresses must not contain a line break.")
    if _MAILBOX.fullmatch(attendee.email) is None:
        raise ValueError(f"Invalid attendee email address: {attendee.email!r}.")


def _iso(value: datetime | date | None) -> str | None:
    return None if value is None else value.isoformat()


def _attendee_to_dict(attendee: Attendee) -> dict[str, Any]:
    return {
        "email": attendee.email,
        "name": attendee.name,
        "role": attendee.role,
        "partstat": attendee.partstat,
    }


def _event_to_dict(event: Event) -> dict[str, Any]:
    busy: bool | None = None
    if event.transp:
        busy = event.transp.upper() != TRANSP_FREE
    return {
        "uid": event.uid,
        "summary": event.summary,
        "start": _iso(event.start),
        "end": _iso(event.end),
        "location": event.location,
        "description": event.description,
        "all_day": event.all_day,
        "busy": busy,
        "organizer": _attendee_to_dict(event.organizer) if event.organizer else None,
        "attendees": [_attendee_to_dict(a) for a in event.attendees],
        "href": event.href,
    }


def _error(message: str) -> str:
    return json.dumps({"error": message})


def _transp_for(busy: Any) -> str:
    return TRANSP_BUSY if bool(busy) else TRANSP_FREE


# -- handlers ---------------------------------------------------------------


def handle_list_calendars(args: dict[str, Any], **_kwargs: Any) -> str:
    try:
        with build_client() as client:
            calendars = client.list_calendars()
        return json.dumps(
            {
                "count": len(calendars),
                "calendars": [{"name": c.display_name, "href": c.href} for c in calendars],
            }
        )
    except MissingCredentials as exc:
        return _error(str(exc))
    except (CalDAVError, ValueError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error listing calendars: {exc}")


def _range_from_args(args: dict[str, Any]) -> tuple[datetime | date, datetime | date]:
    """The time range to query: defaults to the next seven days from now."""
    start = _parse_dt(args["start"]) if args.get("start") else datetime.now(UTC)
    if args.get("end"):
        return start, _parse_dt(args["end"])
    base = (
        start
        if isinstance(start, datetime)
        else datetime(start.year, start.month, start.day, tzinfo=UTC)
    )
    return start, base + timedelta(days=7)


def handle_list(args: dict[str, Any], **_kwargs: Any) -> str:
    try:
        start, end = _range_from_args(args)
        calendar = (args.get("calendar") or "").strip() or None
        with build_client() as client:
            events = client.list_events(start, end, calendar=calendar)
        events.sort(key=lambda e: (e.start is None, _iso(e.start) or ""))
        return json.dumps({"count": len(events), "events": [_event_to_dict(e) for e in events]})
    except MissingCredentials as exc:
        return _error(str(exc))
    except (CalDAVError, ValueError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error listing events: {exc}")


def _span_for_create(args: dict[str, Any]) -> tuple[datetime | date, datetime | date, bool]:
    """Work out (start, end, all_day) from the create arguments.

    A bare date with no ``end`` becomes a whole day; a datetime defaults to an
    hour. A bare date always implies an all-day event, even unflagged.
    """
    all_day = bool(args.get("all_day"))
    start = _parse_dt(args["start"])
    is_date_only = not isinstance(start, datetime)
    if args.get("end"):
        return start, _parse_dt(args["end"]), all_day
    if is_date_only:
        return start, start + timedelta(days=1), True
    return start, start + timedelta(hours=1), all_day


def _event_from_create_args(args: dict[str, Any]) -> Event:
    start, end, all_day = _span_for_create(args)
    return Event(
        uid="",
        summary=(args.get("summary") or "").strip(),
        start=start,
        end=end,
        location=(args.get("location") or "").strip(),
        description=(args.get("description") or "").strip(),
        all_day=all_day,
        attendees=[_parse_attendee(a) for a in (args.get("attendees") or [])],
        transp=_transp_for(args.get("busy", True)) if "busy" in args else "",
    )


def handle_create(args: dict[str, Any], **_kwargs: Any) -> str:
    try:
        if not (args.get("summary") or "").strip():
            return _error("'summary' is required.")
        if not args.get("start"):
            return _error("'start' is required.")
        event = _event_from_create_args(args)
        calendar = (args.get("calendar") or "").strip() or None
        with build_client() as client:
            created = client.create_event(event, calendar=calendar)
        return json.dumps({"created": True, "event": _event_to_dict(created)})
    except MissingCredentials as exc:
        return _error(str(exc))
    except (CalDAVError, ValueError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error creating event: {exc}")


def handle_update(args: dict[str, Any], **_kwargs: Any) -> str:
    try:
        href = (args.get("event_href") or "").strip()
        if not href:
            return _error("'event_href' is required.")
        with build_client() as client:
            event = client.get_event(href)
            if event is None:
                return _error(f"Event not found: {href}")
            _apply_updates(event, args)
            updated = client.update_event(event, href)
        return json.dumps({"updated": True, "event": _event_to_dict(updated)})
    except MissingCredentials as exc:
        return _error(str(exc))
    except (CalDAVError, ValueError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error updating event: {exc}")


_UPDATABLE_TEXT = ("summary", "location", "description")


def _apply_field_updates(event: Event, args: dict[str, Any]) -> None:
    """Apply the scalar fields present in ``args`` (an empty string clears one)."""
    for field in _UPDATABLE_TEXT:
        if args.get(field) is not None:
            setattr(event, field, str(args[field]).strip())
    if "all_day" in args:
        event.all_day = bool(args["all_day"])
    if args.get("start"):
        event.start = _parse_dt(args["start"])
    if args.get("end"):
        event.end = _parse_dt(args["end"])
    if "busy" in args:
        event.transp = _transp_for(args["busy"])


def _apply_attendee_updates(event: Event, args: dict[str, Any]) -> None:
    """Add and remove attendees incrementally, matching e-mails case-insensitively."""
    for item in args.get("add_attendees") or []:
        new = _parse_attendee(item)
        if not any(a.email.lower() == new.email.lower() for a in event.attendees):
            event.attendees.append(new)
    remove = {str(e).strip().lower() for e in (args.get("remove_attendees") or [])}
    if remove:
        event.attendees = [a for a in event.attendees if a.email.lower() not in remove]


def _apply_updates(event: Event, args: dict[str, Any]) -> None:
    """Mutate ``event`` in place with the provided update fields."""
    _apply_field_updates(event, args)
    _apply_attendee_updates(event, args)


_RESPONSE_PARTSTAT = {
    "accept": "ACCEPTED",
    "accepted": "ACCEPTED",
    "decline": "DECLINED",
    "declined": "DECLINED",
    "tentative": "TENTATIVE",
}


def handle_respond(args: dict[str, Any], **_kwargs: Any) -> str:
    try:
        href = (args.get("event_href") or "").strip()
        if not href:
            return _error("'event_href' is required.")
        response = (args.get("response") or "").strip().lower()
        partstat = _RESPONSE_PARTSTAT.get(response)
        if partstat is None:
            return _error("'response' must be one of: accept, decline, tentative.")
        with build_client() as client:
            updated = client.respond_to_event(href, partstat)
        return json.dumps({"responded": True, "status": partstat, "event": _event_to_dict(updated)})
    except MissingCredentials as exc:
        return _error(str(exc))
    except (CalDAVError, ValueError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error responding to event: {exc}")


def handle_move(args: dict[str, Any], **_kwargs: Any) -> str:
    try:
        href = (args.get("event_href") or "").strip()
        if not href:
            return _error("'event_href' is required.")
        calendar = (args.get("calendar") or "").strip()
        if not calendar:
            return _error("'calendar' (destination) is required.")
        with build_client() as client:
            moved = client.move_event(href, calendar)
        return json.dumps({"moved": True, "event": _event_to_dict(moved)})
    except MissingCredentials as exc:
        return _error(str(exc))
    except (CalDAVError, ValueError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error moving event: {exc}")


def handle_delete(args: dict[str, Any], **_kwargs: Any) -> str:
    try:
        href = (args.get("event_href") or "").strip()
        if not href:
            return _error("'event_href' is required.")
        with build_client() as client:
            client.delete_event(href)
        return json.dumps({"deleted": True, "event_href": href})
    except MissingCredentials as exc:
        return _error(str(exc))
    except (CalDAVError, ValueError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error deleting event: {exc}")
