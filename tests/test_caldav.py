"""Unit tests for the CalDAV client using httpx.MockTransport (no network)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from hermes_yandex_calendar.caldav import CalDAVError, YandexCalDAVClient
from hermes_yandex_calendar.ical import Attendee, Event

MULTISTATUS_CALENDARS = """<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:response>
    <d:href>/calendars/user@yandex.ru/</d:href>
    <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>
    <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>/calendars/user@yandex.ru/events-42/</d:href>
    <d:propstat><d:prop>
      <d:resourcetype><d:collection/><c:calendar/></d:resourcetype>
      <d:displayname>My Calendar</d:displayname>
    </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
</d:multistatus>"""

REPORT_EVENTS = """<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:response>
    <d:href>/calendars/user@yandex.ru/events-42/evt-1.ics</d:href>
    <d:propstat><d:prop><c:calendar-data>BEGIN:VCALENDAR
BEGIN:VEVENT
UID:evt-1
SUMMARY:Meeting
DTSTART:20260725T140000Z
DTEND:20260725T150000Z
END:VEVENT
END:VCALENDAR</c:calendar-data></d:prop>
    <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
</d:multistatus>"""


TWO_CALENDARS = """<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:response>
    <d:href>/calendars/user@yandex.ru/events-42/</d:href>
    <d:propstat><d:prop>
      <d:resourcetype><d:collection/><c:calendar/></d:resourcetype>
      <d:displayname>Work</d:displayname>
    </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>/calendars/user@yandex.ru/events-99/</d:href>
    <d:propstat><d:prop>
      <d:resourcetype><d:collection/><c:calendar/></d:resourcetype>
      <d:displayname>Personal</d:displayname>
    </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
</d:multistatus>"""

EVENT_ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:evt-1
SUMMARY:Meeting
DTSTART:20260725T140000Z
DTEND:20260725T150000Z
END:VEVENT
END:VCALENDAR"""


def make_client(handler, *, allowed=None, login="user@yandex.ru") -> YandexCalDAVClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    return YandexCalDAVClient(login, "app-pw", client=http, allowed_calendars=allowed)


def test_discover_calendars_filters_non_calendars():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PROPFIND"
        assert request.headers["Depth"] == "1"
        assert request.url.path == "/calendars/user@yandex.ru/"
        return httpx.Response(207, text=MULTISTATUS_CALENDARS)

    calendars = make_client(handler).discover_calendars()
    assert len(calendars) == 1
    assert calendars[0].href == "/calendars/user@yandex.ru/events-42/"
    assert calendars[0].display_name == "My Calendar"


def test_list_events_uses_default_calendar_and_parses():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PROPFIND":
            return httpx.Response(207, text=MULTISTATUS_CALENDARS)
        assert request.method == "REPORT"
        assert request.url.path == "/calendars/user@yandex.ru/events-42/"
        body = request.content.decode()
        assert "time-range" in body and "20260725T000000Z" in body
        return httpx.Response(207, text=REPORT_EVENTS)

    client = make_client(handler)
    events = client.list_events(
        datetime(2026, 7, 25, tzinfo=UTC),
        datetime(2026, 7, 26, tzinfo=UTC),
    )
    assert len(events) == 1
    assert events[0].summary == "Meeting"
    assert events[0].href == "/calendars/user@yandex.ru/events-42/evt-1.ics"


def test_create_event_puts_ics():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PROPFIND":
            return httpx.Response(207, text=MULTISTATUS_CALENDARS)
        assert request.method == "PUT"
        assert request.headers["If-None-Match"] == "*"
        seen["path"] = request.url.path
        seen["body"] = request.content.decode()
        return httpx.Response(201)

    client = make_client(handler)
    event = Event(
        uid="new-1",
        summary="Call",
        start=datetime(2026, 7, 25, 10, 0, tzinfo=UTC),
        end=datetime(2026, 7, 25, 11, 0, tzinfo=UTC),
    )
    created = client.create_event(event)
    assert created.href == "/calendars/user@yandex.ru/events-42/new-1.ics"
    assert "BEGIN:VEVENT" in seen["body"]
    assert seen["path"] == "/calendars/user@yandex.ru/events-42/new-1.ics"


def test_create_event_generates_uid_when_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PROPFIND":
            return httpx.Response(207, text=MULTISTATUS_CALENDARS)
        return httpx.Response(201)

    created = make_client(handler).create_event(Event(uid="", summary="x"))
    assert created.uid.endswith("@hermes-yandex-calendar")


def test_delete_event():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/calendars/user@yandex.ru/events-42/evt-1.ics"
        return httpx.Response(204)

    make_client(handler).delete_event("/calendars/user@yandex.ru/events-42/evt-1.ics")


def test_delete_missing_href_raises():
    client = make_client(lambda r: httpx.Response(204))
    with pytest.raises(CalDAVError):
        client.delete_event("")


def test_auth_failure_raises_caldaverror():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    with pytest.raises(CalDAVError, match="Authentication failed"):
        make_client(handler).discover_calendars()


def test_transport_error_wrapped():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(CalDAVError, match="request failed"):
        make_client(handler).discover_calendars()


def test_no_calendars_raises():
    empty = """<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"></d:multistatus>"""
    client = make_client(lambda r: httpx.Response(207, text=empty))
    with pytest.raises(CalDAVError, match="No calendars"):
        client.default_calendar_href()


def test_discovery_http_error_status():
    client = make_client(lambda r: httpx.Response(500, text="boom"))
    with pytest.raises(CalDAVError, match="discovery failed: HTTP 500"):
        client.discover_calendars()


def test_malformed_discovery_xml():
    client = make_client(lambda r: httpx.Response(207, text="<<not xml"))
    with pytest.raises(CalDAVError, match="parse discovery"):
        client.discover_calendars()


def test_list_events_explicit_href_http_error():
    client = make_client(lambda r: httpx.Response(403))
    with pytest.raises(CalDAVError, match="Authentication failed"):
        client.list_events(
            datetime(2026, 7, 25, tzinfo=UTC),
            datetime(2026, 7, 26, tzinfo=UTC),
            calendar="/calendars/user@yandex.ru/events-42/",
        )


def test_list_events_report_status_error():
    client = make_client(lambda r: httpx.Response(500))
    with pytest.raises(CalDAVError, match="Listing events failed: HTTP 500"):
        client.list_events(
            datetime(2026, 7, 25, tzinfo=UTC),
            datetime(2026, 7, 26, tzinfo=UTC),
            calendar="/cal/",
        )


def test_create_event_bad_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(412)  # precondition failed (If-None-Match)

    client = make_client(handler)
    with pytest.raises(CalDAVError, match="Creating event failed: HTTP 412"):
        client.create_event(Event(uid="x", summary="y"), calendar="/cal/")


def test_delete_tolerates_404():
    client = make_client(lambda r: httpx.Response(404))
    client.delete_event("/cal/gone.ics")  # already-absent is not an error


def test_delete_bad_status():
    client = make_client(lambda r: httpx.Response(500))
    with pytest.raises(CalDAVError, match="Deleting event failed: HTTP 500"):
        client.delete_event("/cal/x.ics")


def test_url_accepts_same_origin_absolute_and_relative():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(204)

    client = make_client(handler)
    client.delete_event("https://caldav.yandex.ru/full/path.ics")
    client.delete_event("relative/path.ics")
    assert seen[0] == "https://caldav.yandex.ru/full/path.ics"
    assert seen[1] == "https://caldav.yandex.ru/relative/path.ics"


@pytest.mark.parametrize(
    "event_href",
    [
        "https://attacker.example/collect",
        "http://caldav.yandex.ru/calendars/user@yandex.ru/events-42/evt-1.ics",
        "https://caldav.yandex.ru:8443/cal/event.ics",
        "https://caldav.yandex.ru:invalid/cal/event.ics",
        "https://user:password@caldav.yandex.ru/cal/event.ics",
        "//caldav.yandex.ru/cal/event.ics",
        "https:///cal/event.ics",
    ],
)
def test_event_href_rejects_cross_origin_or_insecure_url_before_request(event_href):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(404)

    client = make_client(handler)
    with pytest.raises(CalDAVError, match="HTTPS origin"):
        client.get_event(event_href)
    assert seen == []


def test_client_rejects_insecure_base_url():
    with pytest.raises(CalDAVError, match="HTTPS origin"):
        YandexCalDAVClient("user@yandex.ru", "app-pw", base_url="http://caldav.yandex.ru")


@pytest.mark.parametrize(
    ("event_href", "message"),
    [
        ("/cal/event.ics?download=1", "without query data"),
        ("/cal/event.ics#fragment", "without query data"),
        ("https://caldav.yandex.ru", "without query data"),
        ("/cal/%FF.ics", "percent escape"),
    ],
)
def test_malformed_event_href_is_rejected_before_request(event_href, message):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(204)

    client = make_client(handler)
    with pytest.raises(CalDAVError, match=message):
        client.delete_event(event_href)
    assert seen == []


def test_same_origin_absolute_event_href_preserves_base_path():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(204)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = YandexCalDAVClient(
        "user@yandex.ru",
        "app-pw",
        base_url="https://calendar.example.test/caldav",
        client=http,
    )
    client.delete_event("https://calendar.example.test/caldav/events/work.ics")
    assert seen == ["https://calendar.example.test/caldav/events/work.ics"]


def test_path_prefixed_base_url_allows_discovered_calendar_event():
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "PROPFIND":
            return httpx.Response(207, text=TWO_CALENDARS)
        return httpx.Response(204)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = YandexCalDAVClient(
        "user@yandex.ru",
        "app-pw",
        base_url="https://calendar.example.test/caldav",
        allowed_calendars=["Work"],
        client=http,
    )
    client.delete_event("/calendars/user@yandex.ru/events-42/work.ics")
    assert seen == [
        ("PROPFIND", "/caldav/calendars/user@yandex.ru/"),
        ("DELETE", "/caldav/calendars/user@yandex.ru/events-42/work.ics"),
    ]


def test_path_prefixed_update_href_can_be_reused():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(204)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = YandexCalDAVClient(
        "user@yandex.ru", "app-pw", base_url="https://calendar.example.test/caldav", client=http
    )
    updated = client.update_event(Event(uid="event"), "/events/event.ics")
    client.delete_event(updated.href)
    assert seen == ["/caldav/events/event.ics", "/caldav/events/event.ics"]


def test_encoded_unreserved_character_is_canonicalized_on_the_wire():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.method == "PROPFIND":
            return httpx.Response(207, text=TWO_CALENDARS)
        return httpx.Response(204)

    client = make_client(handler, allowed=["Work"])
    client.delete_event("/calendars/user%40yandex.ru/events-42/event.ics")
    assert seen[-1] == "/calendars/user@yandex.ru/events-42/event.ics"


def test_encoded_separator_cannot_change_the_validated_collection():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        if request.method == "PROPFIND":
            return httpx.Response(207, text=TWO_CALENDARS)
        return httpx.Response(204)

    client = make_client(handler, allowed=["Work"])
    with pytest.raises(CalDAVError, match="not in an allowed calendar"):
        client.delete_event("/calendars/user@yandex.ru/events-42%2fevent.ics")
    assert seen == ["PROPFIND"]


@pytest.mark.parametrize(
    "event_href",
    [
        "https://cal\tdav.yandex.ru/cal/event.ics",
        "https://[::1/cal/event.ics",
    ],
)
def test_invalid_url_is_wrapped_as_caldav_error(event_href):
    with pytest.raises(CalDAVError):
        make_client(lambda request: httpx.Response(204)).delete_event(event_href)


def test_list_calendars_applies_allow_list():
    client = make_client(lambda r: httpx.Response(207, text=TWO_CALENDARS), allowed=["Personal"])
    cals = client.list_calendars()
    assert [c.display_name for c in cals] == ["Personal"]


def test_resolve_calendar_by_name():
    client = make_client(lambda r: httpx.Response(207, text=TWO_CALENDARS))
    assert client.resolve_calendar_href("personal") == "/calendars/user@yandex.ru/events-99/"
    # last path segment also resolves
    assert client.resolve_calendar_href("events-42") == "/calendars/user@yandex.ru/events-42/"


def test_resolve_default_is_first():
    client = make_client(lambda r: httpx.Response(207, text=TWO_CALENDARS))
    assert client.resolve_calendar_href(None) == "/calendars/user@yandex.ru/events-42/"


def test_resolve_unknown_calendar_raises_with_names():
    client = make_client(lambda r: httpx.Response(207, text=TWO_CALENDARS))
    with pytest.raises(CalDAVError) as exc:
        client.resolve_calendar_href("Nope")
    assert "not found. Available calendars: 'Work', 'Personal'" in str(exc.value)


def test_resolve_explicit_href_skips_discovery_when_no_allow_list():
    calls = {"propfind": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PROPFIND":
            calls["propfind"] += 1
        return httpx.Response(207, text=TWO_CALENDARS)

    client = make_client(handler)
    assert client.resolve_calendar_href("/calendars/user@yandex.ru/events-42/").endswith(
        "events-42/"
    )
    assert calls["propfind"] == 0  # no discovery round-trip


def test_allow_list_forces_validation_of_explicit_ref():
    client = make_client(lambda r: httpx.Response(207, text=TWO_CALENDARS), allowed=["Work"])
    # Personal exists but is not allowed -> rejected
    with pytest.raises(CalDAVError, match="not found"):
        client.resolve_calendar_href("Personal")


@pytest.mark.parametrize("operation", ["get", "update", "delete", "respond", "move"])
def test_allow_list_rejects_direct_event_operations_in_other_calendar(operation):
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "PROPFIND":
            return httpx.Response(207, text=TWO_CALENDARS)
        return httpx.Response(204)

    client = make_client(handler, allowed=["Work"])
    private_href = "/calendars/user@yandex.ru/events-99/private.ics"
    with pytest.raises(CalDAVError, match="not in an allowed calendar"):
        if operation == "get":
            client.get_event(private_href)
        elif operation == "update":
            client.update_event(Event(uid="private"), private_href)
        elif operation == "respond":
            client.respond_to_event(private_href, "ACCEPTED")
        elif operation == "move":
            client.move_event(private_href, "Work")
        else:
            client.delete_event(private_href)

    assert seen == [("PROPFIND", "/calendars/user@yandex.ru/")]


def test_allow_list_accepts_direct_event_in_allowed_calendar():
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "PROPFIND":
            return httpx.Response(207, text=TWO_CALENDARS)
        return httpx.Response(204)

    client = make_client(handler, allowed=["Work"])
    client.delete_event("/calendars/user@yandex.ru/events-42/work.ics")
    assert seen == [
        ("PROPFIND", "/calendars/user@yandex.ru/"),
        ("DELETE", "/calendars/user@yandex.ru/events-42/work.ics"),
    ]


def test_direct_event_reports_when_allow_list_matches_no_calendars():
    client = make_client(
        lambda request: httpx.Response(207, text=TWO_CALENDARS), allowed=["Missing"]
    )
    with pytest.raises(CalDAVError, match="No calendars available"):
        client.delete_event("/calendars/user@yandex.ru/events-42/work.ics")


def test_nested_percent_encoding_stays_in_allowed_filename():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path.decode())
        if request.method == "PROPFIND":
            return httpx.Response(207, text=TWO_CALENDARS)
        return httpx.Response(204)

    client = make_client(handler, allowed=["Work"])
    href = "/calendars/user@yandex.ru/events-42/%252e%252e%252fevents-99%252fprivate.ics"
    client.delete_event(href)
    assert seen[-1] == href


def test_allow_list_rejects_malformed_percent_escape():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PROPFIND":
            return httpx.Response(207, text=TWO_CALENDARS)
        return httpx.Response(204)

    client = make_client(handler, allowed=["Work"])
    with pytest.raises(CalDAVError, match="percent escape"):
        client.delete_event("/calendars/user@yandex.ru/events-42/bad%zz.ics")


def test_event_operation_rejects_calendar_collection_before_request():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(204)

    client = make_client(handler, allowed=["Work"])
    with pytest.raises(CalDAVError, match="event resource"):
        client.delete_event("/calendars/user@yandex.ru/events-42/")
    assert seen == []


def test_event_operation_rejects_collection_without_trailing_slash():
    seen: list[str] = []
    client = make_client(lambda request: seen.append(request.method) or httpx.Response(204))
    with pytest.raises(CalDAVError, match="event resource"):
        client.delete_event("/calendars/user@yandex.ru/events-42")
    assert seen == []


@pytest.mark.parametrize(
    "event_href",
    [
        "/calendars/user@yandex.ru/events-42/\\..\\events-99\\private.ics",
        "/calendars/user@yandex.ru/events-42/%5c..%5cevents-99%5cprivate.ics",
    ],
)
def test_allow_list_rejects_backslash_path_before_request(event_href):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(204)

    client = make_client(handler, allowed=["Work"])
    with pytest.raises(CalDAVError, match="backslash"):
        client.delete_event(event_href)
    assert seen == []


@pytest.mark.parametrize(
    "suffix",
    [".", "..", "%2e", "%2e%2e"],
)
def test_event_operation_rejects_terminal_dot_segment_before_request(suffix):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(204)

    client = make_client(handler, allowed=["Work"])
    with pytest.raises(CalDAVError, match="dot segment"):
        client.delete_event(f"/calendars/user@yandex.ru/events-42/{suffix}")
    assert seen == []


def test_get_event_parses():
    client = make_client(lambda r: httpx.Response(200, text=EVENT_ICS))
    event = client.get_event("/calendars/user@yandex.ru/events-42/evt-1.ics")
    assert event is not None
    assert event.summary == "Meeting"
    assert event.href == "/calendars/user@yandex.ru/events-42/evt-1.ics"


def test_get_event_404_returns_none():
    client = make_client(lambda r: httpx.Response(404))
    assert client.get_event("/cal/gone.ics") is None


def test_update_event_puts_without_if_none_match():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["inm"] = request.headers.get("If-None-Match")
        seen["body"] = request.content.decode()
        return httpx.Response(204)

    client = make_client(handler)
    event = Event(uid="evt-1", summary="Updated")
    updated = client.update_event(event, "/cal/evt-1.ics")
    assert seen["method"] == "PUT"
    assert seen["inm"] is None  # update overwrites, must not send If-None-Match
    assert "SUMMARY:Updated" in seen["body"]
    assert updated.href == "/cal/evt-1.ics"


def test_create_with_attendees_defaults_organizer():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PROPFIND":
            return httpx.Response(207, text=MULTISTATUS_CALENDARS)
        assert "ORGANIZER:mailto:user@yandex.ru" in request.content.decode()
        return httpx.Response(201)

    from hermes_yandex_calendar.ical import Attendee

    client = make_client(handler)
    client.create_event(Event(uid="e", summary="s", attendees=[Attendee(email="a@x.ru")]))


def test_create_with_bare_login_uses_yandex_mailbox_for_organizer():
    bodies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PROPFIND":
            return httpx.Response(207, text=MULTISTATUS_CALENDARS)
        bodies.append(request.content.decode())
        return httpx.Response(201)

    client = make_client(handler, login="ivanov")
    client.create_event(Event(uid="e", attendees=[Attendee(email="a@x.ru")]))
    assert "ORGANIZER:mailto:ivanov@yandex.ru" in bodies[0]


MEETING_ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:evt-1
SUMMARY:Sync
DTSTART:20260725T140000Z
ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:user@yandex.ru
ATTENDEE:mailto:other@x.ru
END:VEVENT
END:VCALENDAR"""


def test_respond_to_event_sets_own_partstat():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=MEETING_ICS)
        seen["body"] = request.content.decode()
        return httpx.Response(204)

    client = make_client(handler)
    client.respond_to_event("/cal/evt-1.ics", "ACCEPTED")
    body = seen["body"]
    # our own attendee flips to ACCEPTED; the other attendee stays as-is
    assert "ATTENDEE;PARTSTAT=ACCEPTED;RSVP=FALSE:mailto:user@yandex.ru" in body
    assert "ATTENDEE:mailto:other@x.ru" in body


def test_respond_preserves_server_side_non_mailbox_address():
    ics = MEETING_ICS.replace(
        "ATTENDEE:mailto:other@x.ru", "ATTENDEE;CN=Room:urn:uuid:meeting-room"
    )
    bodies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=ics)
        bodies.append(request.content.decode())
        return httpx.Response(204)

    make_client(handler).respond_to_event("/cal/evt-1.ics", "ACCEPTED")
    assert "ATTENDEE;CN=Room:urn:uuid:meeting-room" in bodies[0]


def test_respond_matches_ya_ru_login_against_yandex_ru_attendee():
    """Yandex rewrites @ya.ru to @yandex.ru, so the login must still match the ATTENDEE."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=MEETING_ICS)
        seen["body"] = request.content.decode()
        return httpx.Response(204)

    client = make_client(handler, login="User@ya.ru")
    client.respond_to_event("/cal/evt-1.ics", "ACCEPTED")
    assert "ATTENDEE;PARTSTAT=ACCEPTED;RSVP=FALSE:mailto:user@yandex.ru" in seen["body"]


def test_respond_matches_yandex_ru_login_against_ya_ru_attendee():
    seen = {}
    ics = MEETING_ICS.replace("mailto:user@yandex.ru", "mailto:USER@ya.ru")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=ics)
        seen["body"] = request.content.decode()
        return httpx.Response(204)

    client = make_client(handler, login="user@yandex.ru")
    client.respond_to_event("/cal/evt-1.ics", "DECLINED")
    assert "ATTENDEE;PARTSTAT=DECLINED;RSVP=FALSE:mailto:USER@ya.ru" in seen["body"]


def test_respond_matches_other_yandex_domain_aliases():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=MEETING_ICS)
        seen["body"] = request.content.decode()
        return httpx.Response(204)

    client = make_client(handler, login="user@yandex.com")
    client.respond_to_event("/cal/evt-1.ics", "TENTATIVE")
    assert "PARTSTAT=TENTATIVE" in seen["body"]


def test_respond_does_not_fold_unrelated_domains():
    client = make_client(lambda r: httpx.Response(200, text=MEETING_ICS), login="user@x.ru")
    with pytest.raises(CalDAVError, match="not listed as an attendee"):
        client.respond_to_event("/cal/evt-1.ics", "ACCEPTED")


def test_respond_when_not_attendee_raises():
    not_me = MEETING_ICS.replace("mailto:user@yandex.ru", "mailto:someoneelse@x.ru")
    client = make_client(lambda r: httpx.Response(200, text=not_me))
    with pytest.raises(CalDAVError, match="not listed as an attendee"):
        client.respond_to_event("/cal/evt-1.ics", "DECLINED")


def test_respond_event_missing_raises():
    client = make_client(lambda r: httpx.Response(404))
    with pytest.raises(CalDAVError, match="Event not found"):
        client.respond_to_event("/cal/gone.ics", "ACCEPTED")


def test_move_event_copies_then_deletes():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, text=EVENT_ICS)
        if request.method == "PROPFIND":
            return httpx.Response(207, text=TWO_CALENDARS)
        if request.method == "PUT":
            return httpx.Response(201)
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(405)

    client = make_client(handler)
    moved = client.move_event("/calendars/user@yandex.ru/events-42/evt-1.ics", "Personal")
    methods = [m for m, _ in calls]
    # copy (PUT into target) must precede the DELETE of the original
    assert methods.index("PUT") < methods.index("DELETE")
    assert moved.href == "/calendars/user@yandex.ru/events-99/evt-1.ics"
    assert ("DELETE", "/calendars/user@yandex.ru/events-42/evt-1.ics") in calls


def test_move_event_uid_is_escaped_inside_target_calendar():
    calls: list[tuple[str, str]] = []
    crafted = EVENT_ICS.replace("UID:evt-1", "UID:../events-7/smuggled")

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.raw_path.decode()))
        if request.method == "GET":
            return httpx.Response(200, text=crafted)
        if request.method == "PROPFIND":
            return httpx.Response(207, text=TWO_CALENDARS)
        return httpx.Response(204)

    client = make_client(handler, allowed=["Work", "Personal"])
    moved = client.move_event("/calendars/user@yandex.ru/events-42/evt-1.ics", "Personal")
    destination = "/calendars/user@yandex.ru/events-99/..%2Fevents-7%2Fsmuggled.ics"
    assert ("PUT", destination) in calls
    assert moved.href == destination


def test_basic_auth_is_not_forwarded_on_cross_origin_redirect():
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("Authorization")))
        if request.url.host == "caldav.yandex.ru":
            return httpx.Response(302, headers={"Location": "https://attacker.example/event.ics"})
        return httpx.Response(404)

    http = httpx.Client(
        auth=httpx.BasicAuth("user", "password"),
        follow_redirects=True,
        transport=httpx.MockTransport(handler),
    )
    client = YandexCalDAVClient("user", "password", client=http)
    assert client.get_event("/cal/event.ics") is None
    assert seen[0][1] is not None
    assert seen[1] == ("https://attacker.example/event.ics", None)


def test_move_event_same_calendar_is_noop():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, text=EVENT_ICS)
        return httpx.Response(207, text=TWO_CALENDARS)

    client = make_client(handler)
    client.move_event(
        "/calendars/user@yandex.ru/events-42/evt-1.ics",
        "/calendars/user@yandex.ru/events-42/",
    )
    assert "PUT" not in calls and "DELETE" not in calls


def test_move_event_encoded_and_literal_collection_names_are_equal():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, text=EVENT_ICS)
        return httpx.Response(207, text=TWO_CALENDARS)

    client = make_client(handler)
    client.move_event(
        "/calendars/user%40yandex.ru/events-42/evt-1.ics",
        "/calendars/user@yandex.ru/events-42/",
    )
    assert "PUT" not in calls and "DELETE" not in calls


def test_lifecycle_context_manager_closes():
    closed = {"v": False}

    class Spy(httpx.Client):
        def close(self):
            closed["v"] = True
            super().close()

    http = Spy(transport=httpx.MockTransport(lambda r: httpx.Response(204)))
    client = YandexCalDAVClient("u", "p", client=http)
    # client did not own `http`, so context exit must NOT close it
    with client:
        pass
    assert closed["v"] is False
    http.close()


RECURRING_WITH_OVERRIDE = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:evt-rec
SUMMARY:Weekly
DTSTART:20260725T090000Z
RRULE:FREQ=WEEKLY
END:VEVENT
BEGIN:VEVENT
UID:evt-rec
RECURRENCE-ID:20260801T090000Z
SUMMARY:Weekly (moved)
DTSTART:20260801T100000Z
END:VEVENT
END:VCALENDAR"""


def test_get_event_refuses_resources_with_recurrence_overrides():
    """Editing would keep only the first VEVENT, silently dropping the exceptions."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=RECURRING_WITH_OVERRIDE)

    with pytest.raises(CalDAVError, match="2 components"):
        make_client(handler).get_event("/cal/evt-rec.ics")


def test_move_event_copies_the_resource_verbatim():
    """Everything survives a move, including occurrences the model cannot represent."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=RECURRING_WITH_OVERRIDE)
        if request.method == "PROPFIND":
            return httpx.Response(207, text=TWO_CALENDARS)
        if request.method == "PUT":
            seen["path"] = request.url.path
            seen["body"] = request.content.decode()
            seen["if_none_match"] = request.headers.get("If-None-Match")
            return httpx.Response(201)
        seen["deleted"] = request.url.path
        return httpx.Response(204)

    client = make_client(handler)
    moved = client.move_event("/calendars/user@yandex.ru/events-99/evt-rec.ics", "events-42")

    assert seen["body"] == RECURRING_WITH_OVERRIDE
    assert seen["if_none_match"] == "*"
    assert seen["path"] == "/calendars/user@yandex.ru/events-42/evt-rec.ics"
    assert seen["deleted"] == "/calendars/user@yandex.ru/events-99/evt-rec.ics"
    assert moved.href == "/calendars/user@yandex.ru/events-42/evt-rec.ics"


def test_move_event_keeps_the_original_when_the_copy_fails():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, text=RECURRING_WITH_OVERRIDE)
        if request.method == "PROPFIND":
            return httpx.Response(207, text=TWO_CALENDARS)
        return httpx.Response(412)  # PUT rejected: something is already there

    client = make_client(handler)
    with pytest.raises(CalDAVError, match="Moving event failed"):
        client.move_event("/calendars/user@yandex.ru/events-99/evt-rec.ics", "events-42")
    assert "DELETE" not in calls
