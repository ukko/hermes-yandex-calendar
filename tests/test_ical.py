"""Unit tests for the iCalendar parse/serialize helpers (no network)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from hermes_yandex_calendar.ical import Attendee, Event, build_calendar, parse_events


def test_parse_utc_event():
    text = (
        "BEGIN:VCALENDAR\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:abc-123\r\n"
        "SUMMARY:Standup\r\n"
        "DTSTART:20260725T090000Z\r\n"
        "DTEND:20260725T093000Z\r\n"
        "LOCATION:Room 1\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    (event,) = parse_events(text)
    assert event.uid == "abc-123"
    assert event.summary == "Standup"
    assert event.location == "Room 1"
    assert event.start == datetime(2026, 7, 25, 9, 0, tzinfo=UTC)
    assert event.end == datetime(2026, 7, 25, 9, 30, tzinfo=UTC)
    assert event.all_day is False


def test_parse_all_day_event():
    text = (
        "BEGIN:VEVENT\r\n"
        "UID:day-1\r\n"
        "SUMMARY:Holiday\r\n"
        "DTSTART;VALUE=DATE:20260101\r\n"
        "DTEND;VALUE=DATE:20260102\r\n"
        "END:VEVENT\r\n"
    )
    (event,) = parse_events(text)
    assert event.all_day is True
    assert event.start == date(2026, 1, 1)
    assert event.end == date(2026, 1, 2)


def test_parse_tzid_event():
    text = (
        "BEGIN:VEVENT\r\nUID:tz-1\r\nDTSTART;TZID=Europe/Moscow:20260725T120000\r\nEND:VEVENT\r\n"
    )
    (event,) = parse_events(text)
    assert isinstance(event.start, datetime)
    # Moscow is UTC+3, so noon local == 09:00 UTC
    assert event.start.astimezone(UTC).hour == 9


def test_unfolding_and_escaping():
    text = (
        "BEGIN:VEVENT\r\n"
        "UID:fold-1\r\n"
        "DESCRIPTION:line one\\nline two\\; still here and fol\r\n"
        " ded tail\r\n"
        "END:VEVENT\r\n"
    )
    (event,) = parse_events(text)
    # RFC 5545 unfolding removes CRLF + one leading space and concatenates directly
    # (no space inserted) — the word split across the fold is rejoined.
    assert event.description == "line one\nline two; still here and folded tail"


def test_multiple_events():
    text = "BEGIN:VEVENT\r\nUID:a\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:b\r\nEND:VEVENT\r\n"
    events = parse_events(text)
    assert [e.uid for e in events] == ["a", "b"]


def test_build_roundtrip():
    event = Event(
        uid="rt-1",
        summary="Lunch, with a comma",
        start=datetime(2026, 7, 25, 12, 0, tzinfo=UTC),
        end=datetime(2026, 7, 25, 13, 0, tzinfo=UTC),
        location="Cafe",
        description="notes; here",
    )
    ics = build_calendar(event)
    assert "BEGIN:VCALENDAR" in ics and ics.endswith("\r\n")
    assert "SUMMARY:Lunch\\, with a comma" in ics
    assert "DTSTART:20260725T120000Z" in ics
    (parsed,) = parse_events(ics)
    assert parsed.summary == "Lunch, with a comma"
    assert parsed.description == "notes; here"
    assert parsed.start == event.start


def test_build_all_day():
    event = Event(
        uid="ad-1", summary="Trip", start=date(2026, 5, 1), end=date(2026, 5, 3), all_day=True
    )
    ics = build_calendar(event)
    assert "DTSTART;VALUE=DATE:20260501" in ics
    assert "DTEND;VALUE=DATE:20260503" in ics


def test_build_folds_long_lines():
    event = Event(uid="long-1", summary="x" * 200)
    ics = build_calendar(event)
    for line in ics.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75


def test_naive_datetime_has_no_z():
    event = Event(uid="naive-1", start=datetime(2026, 7, 25, 8, 0))
    ics = build_calendar(event)
    assert "DTSTART:20260725T080000\r\n" in ics


def test_parse_attendees_organizer_transp():
    text = (
        "BEGIN:VEVENT\r\n"
        "UID:m-1\r\n"
        "SUMMARY:Sync\r\n"
        "TRANSP:TRANSPARENT\r\n"
        "ORGANIZER;CN=Boss:mailto:boss@yandex.ru\r\n"
        "ATTENDEE;CN=Ann;ROLE=REQ-PARTICIPANT;PARTSTAT=ACCEPTED;RSVP=TRUE:mailto:ann@x.ru\r\n"
        "ATTENDEE:mailto:bob@x.ru\r\n"
        "END:VEVENT\r\n"
    )
    (event,) = parse_events(text)
    assert event.transp == "TRANSPARENT"
    assert event.organizer.email == "boss@yandex.ru"
    assert event.organizer.name == "Boss"
    assert [a.email for a in event.attendees] == ["ann@x.ru", "bob@x.ru"]
    ann = event.attendees[0]
    assert ann.name == "Ann" and ann.partstat == "ACCEPTED" and ann.rsvp is True
    assert event.attendees[1].rsvp is None


def test_parse_quoted_calendar_address_parameters():
    text = (
        "BEGIN:VEVENT\r\n"
        "UID:quoted\r\n"
        'ATTENDEE;CN="Alice; VP: Sales";PARTSTAT=NEEDS-ACTION:mailto:alice@x.ru\r\n'
        "END:VEVENT\r\n"
    )
    (event,) = parse_events(text)
    assert event.attendees[0].name == "Alice; VP: Sales"
    assert event.attendees[0].partstat == "NEEDS-ACTION"


def test_build_attendees_roundtrip():
    event = Event(
        uid="m-2",
        summary="Plan",
        organizer=Attendee(email="me@yandex.ru", name="Me"),
        attendees=[Attendee(email="a@x.ru", name="A, B", role="REQ-PARTICIPANT", rsvp=True)],
        transp="OPAQUE",
    )
    ics = build_calendar(event)
    assert "TRANSP:OPAQUE" in ics
    assert "ORGANIZER;CN=Me:mailto:me@yandex.ru" in ics
    assert 'CN="A, B"' in ics  # comma forces quoting
    (parsed,) = parse_events(ics)
    assert parsed.organizer.email == "me@yandex.ru"
    assert parsed.attendees[0].email == "a@x.ru"
    assert parsed.attendees[0].name == "A, B"
    assert parsed.attendees[0].rsvp is True


@pytest.mark.parametrize(
    "attendee",
    [
        Attendee(email="safe@example.com\r\nX-INJECTED:YES"),
        Attendee(email="safe@example.com", name="Safe\r\nX-INJECTED:YES"),
        Attendee(email="safe@example.com", role="REQ-PARTICIPANT\r\nX-INJECTED:YES"),
        Attendee(email="safe@example.com", partstat="ACCEPTED\r\nX-INJECTED:YES"),
    ],
)
def test_build_rejects_line_break_in_calendar_address(attendee):
    event = Event(uid="injection", attendees=[attendee])
    with pytest.raises(ValueError, match="line break"):
        build_calendar(event)


@pytest.mark.parametrize(
    "email",
    [
        "not-an-email",
        "first@second@example.com",
        "mailto:safe@example.com",
        "safe@example.com?subject=unexpected",
        "safe@example.com;mailto:other@example.com",
    ],
)
def test_build_preserves_server_calendar_address(email):
    event = Event(uid="invalid-address", attendees=[Attendee(email=email)])
    assert email in build_calendar(event)


def test_build_normalizes_text_line_breaks_before_escaping():
    event = Event(uid="multiline", description="first\r\nsecond\rthird")
    ics = build_calendar(event)
    assert "DESCRIPTION:first\\nsecond\\nthird\r\n" in ics


def test_build_rejects_line_break_in_raw_property():
    event = Event(uid="event", raw_props=["X-CUSTOM:value\r\nX-INJECTED:YES"])
    with pytest.raises(ValueError, match="line break"):
        build_calendar(event)


def test_raw_props_are_preserved_on_roundtrip():
    text = (
        "BEGIN:VEVENT\r\n"
        "UID:r-1\r\n"
        "SUMMARY:Weekly\r\n"
        "DTSTART:20260101T090000Z\r\n"
        "RRULE:FREQ=WEEKLY;BYDAY=MO\r\n"
        "SEQUENCE:3\r\n"
        "BEGIN:VALARM\r\n"
        "ACTION:DISPLAY\r\n"
        "TRIGGER:-PT15M\r\n"
        "END:VALARM\r\n"
        "END:VEVENT\r\n"
    )
    (event,) = parse_events(text)
    assert "RRULE:FREQ=WEEKLY;BYDAY=MO" in event.raw_props
    assert "BEGIN:VALARM" in event.raw_props and "END:VALARM" in event.raw_props
    # editing the summary must not drop the recurrence rule or the alarm
    event.summary = "Weekly (renamed)"
    ics = build_calendar(event)
    assert "RRULE:FREQ=WEEKLY;BYDAY=MO" in ics
    assert "BEGIN:VALARM" in ics and "ACTION:DISPLAY" in ics
    assert "SUMMARY:Weekly (renamed)" in ics


def test_dtstamp_added_when_requested_and_not_duplicated():
    with_stamp = build_calendar(Event(uid="s-1"), dtstamp=datetime(2026, 7, 25, 12, tzinfo=UTC))
    assert "DTSTAMP:20260725T120000Z" in with_stamp
    # an event that already carries a DTSTAMP keeps its own, no duplicate
    ev = Event(uid="s-2", raw_props=["DTSTAMP:20200101T000000Z"])
    out = build_calendar(ev, dtstamp=datetime(2026, 7, 25, 12, tzinfo=UTC))
    assert out.count("DTSTAMP") == 1
    assert "DTSTAMP:20200101T000000Z" in out


VALARM_ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:evt-alarm
SUMMARY:Standup
DESCRIPTION:Daily sync notes
DTSTART:20260725T090000Z
RRULE:FREQ=WEEKLY;BYDAY=MO
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:Reminder popup
TRIGGER:-PT10M
END:VALARM
END:VEVENT
END:VCALENDAR"""


def test_valarm_properties_do_not_leak_into_the_event():
    """A VALARM carries its own DESCRIPTION; it must not become the event's."""
    event = parse_events(VALARM_ICS)[0]
    assert event.summary == "Standup"
    assert event.description == "Daily sync notes"


def test_valarm_block_is_preserved_verbatim():
    event = parse_events(VALARM_ICS)[0]
    rebuilt = build_calendar(event)
    assert "BEGIN:VALARM" in rebuilt
    assert "DESCRIPTION:Reminder popup" in rebuilt
    assert "TRIGGER:-PT10M" in rebuilt
    assert "END:VALARM" in rebuilt
    assert "RRULE:FREQ=WEEKLY;BYDAY=MO" in rebuilt
    # the event's own DESCRIPTION is still emitted exactly once
    assert rebuilt.count("DESCRIPTION:Daily sync notes") == 1


def test_nested_component_round_trips_through_reparse():
    event = parse_events(VALARM_ICS)[0]
    reparsed = parse_events(build_calendar(event))[0]
    assert reparsed.description == "Daily sync notes"
    assert any("VALARM" in line for line in reparsed.raw_props)
