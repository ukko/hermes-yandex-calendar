"""Unit tests for the tool handlers: JSON shape + never-raise contract."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from hermes_yandex_calendar import tool
from hermes_yandex_calendar.caldav import CalDAVError, YandexCalDAVClient
from hermes_yandex_calendar.config import MissingCredentials
from hermes_yandex_calendar.ical import Event


class FakeClient:
    def __init__(self, *, events=None, calendars=None, get_result="unset", raise_on=None):
        self._events = events or []
        self._calendars = calendars or []
        self._get_result = get_result
        self._raise_on = raise_on
        self.created: Event | None = None
        self.updated: Event | None = None
        self.update_href: str | None = None
        self.deleted: str | None = None
        self.list_calendar_arg: str | None = "unset"
        self.respond_args: tuple[str, str] | None = None
        self.move_args: tuple[str, str] | None = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def list_calendars(self):
        if self._raise_on == "list_calendars":
            raise CalDAVError("cal boom")
        return self._calendars

    def list_events(self, start, end, calendar=None):
        if self._raise_on == "list":
            raise CalDAVError("list boom")
        self.list_calendar_arg = calendar
        return self._events

    def create_event(self, event, calendar=None):
        if self._raise_on == "create":
            raise CalDAVError("create boom")
        event.href = "/calendars/user/events-1/x.ics"
        self.created = event
        return event

    def get_event(self, href):
        if self._raise_on == "get":
            raise CalDAVError("get boom")
        if self._get_result == "unset":
            return Event(uid="existing", summary="Old", href=href)
        return self._get_result

    def update_event(self, event, href):
        if self._raise_on == "update":
            raise CalDAVError("update boom")
        self.updated = event
        self.update_href = href
        event.href = href
        return event

    def respond_to_event(self, href, partstat):
        if self._raise_on == "respond":
            raise CalDAVError("respond boom")
        self.respond_args = (href, partstat)
        return Event(uid="e", summary="Sync", href=href)

    def move_event(self, href, target):
        if self._raise_on == "move":
            raise CalDAVError("move boom")
        self.move_args = (href, target)
        return Event(uid="e", summary="Sync", href=f"{target}e.ics")

    def delete_event(self, href):
        if self._raise_on == "delete":
            raise CalDAVError("delete boom")
        self.deleted = href


@pytest.fixture
def patch_client(monkeypatch):
    def _install(client):
        monkeypatch.setattr(tool, "build_client", lambda: client)

    return _install


def test_list_returns_sorted_events(patch_client):
    events = [
        Event(uid="b", summary="Later", start=datetime(2026, 7, 25, 15, tzinfo=UTC)),
        Event(uid="a", summary="Earlier", start=datetime(2026, 7, 25, 9, tzinfo=UTC)),
    ]
    patch_client(FakeClient(events=events))
    out = json.loads(tool.handle_list({"start": "2026-07-25", "end": "2026-07-26"}))
    assert out["count"] == 2
    assert [e["summary"] for e in out["events"]] == ["Earlier", "Later"]


@pytest.mark.parametrize(
    ("handle", "args", "message"),
    [
        (tool.handle_delete, {"event_href": "https://attacker.example/event.ics"}, "HTTPS origin"),
        (tool.handle_delete, {"event_href": "/cal/%FF.ics"}, "percent escape"),
        (
            tool.handle_create,
            {
                "summary": "Meeting",
                "start": "2026-07-25",
                "calendar": "/cal/",
                "attendees": [{"email": "safe@example.com", "name": "Name\r\nX-INJECTED:YES"}],
            },
            "line break",
        ),
    ],
)
def test_validation_failures_return_json_without_network(patch_client, handle, args, message):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(204)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        patch_client(YandexCalDAVClient("user@yandex.ru", "app-pw", client=http))
        result = handle(args)

    assert isinstance(result, str)
    assert message in json.loads(result)["error"]
    assert seen == []


def test_list_defaults_range(patch_client):
    patch_client(FakeClient(events=[]))
    out = json.loads(tool.handle_list({}))
    assert out == {"count": 0, "events": []}


def test_list_bad_date_returns_error(patch_client):
    patch_client(FakeClient())
    out = json.loads(tool.handle_list({"start": "not-a-date"}))
    assert "error" in out


def test_list_caldav_error_returns_error(patch_client):
    patch_client(FakeClient(raise_on="list"))
    out = json.loads(tool.handle_list({}))
    assert out["error"] == "list boom"


def test_list_missing_credentials(monkeypatch):
    def boom():
        raise MissingCredentials("set the vars")

    monkeypatch.setattr(tool, "build_client", boom)
    out = json.loads(tool.handle_list({}))
    assert out["error"] == "set the vars"


def test_create_requires_summary(patch_client):
    patch_client(FakeClient())
    out = json.loads(tool.handle_create({"start": "2026-07-25T10:00:00Z"}))
    assert "error" in out and "summary" in out["error"]


def test_create_requires_start(patch_client):
    patch_client(FakeClient())
    out = json.loads(tool.handle_create({"summary": "x"}))
    assert "error" in out and "start" in out["error"]


def test_create_defaults_end_to_one_hour(patch_client):
    fake = FakeClient()
    patch_client(fake)
    out = json.loads(tool.handle_create({"summary": "Call", "start": "2026-07-25T10:00:00Z"}))
    assert out["created"] is True
    assert fake.created.end == datetime(2026, 7, 25, 11, 0, tzinfo=UTC)


def test_create_all_day_from_bare_date(patch_client):
    fake = FakeClient()
    patch_client(fake)
    out = json.loads(tool.handle_create({"summary": "Trip", "start": "2026-07-25"}))
    assert out["created"] is True
    assert fake.created.all_day is True


def test_create_all_day_flag_defaults_end_next_day(patch_client):
    fake = FakeClient()
    patch_client(fake)
    out = json.loads(
        tool.handle_create({"summary": "Conf", "start": "2026-07-25", "all_day": True})
    )
    assert out["created"] is True
    assert fake.created.all_day is True
    assert str(fake.created.end) == "2026-07-26"


def test_create_honors_explicit_end(patch_client):
    fake = FakeClient()
    patch_client(fake)
    out = json.loads(
        tool.handle_create(
            {
                "summary": "Long call",
                "start": "2026-07-25T10:00:00Z",
                "end": "2026-07-25T12:30:00Z",
                "location": "HQ",
                "description": "sync",
            }
        )
    )
    assert out["created"] is True
    assert fake.created.end == datetime(2026, 7, 25, 12, 30, tzinfo=UTC)
    assert fake.created.location == "HQ"


def test_list_end_defaults_from_datetime_start(patch_client):
    fake = FakeClient(events=[])
    patch_client(fake)
    out = json.loads(tool.handle_list({"start": "2026-07-25T09:00:00Z"}))
    assert out == {"count": 0, "events": []}


def test_create_error_wrapped(patch_client):
    patch_client(FakeClient(raise_on="create"))
    out = json.loads(tool.handle_create({"summary": "x", "start": "2026-07-25T10:00:00Z"}))
    assert out["error"] == "create boom"


def test_delete_ok(patch_client):
    fake = FakeClient()
    patch_client(fake)
    out = json.loads(tool.handle_delete({"event_href": "/x/y.ics"}))
    assert out == {"deleted": True, "event_href": "/x/y.ics"}
    assert fake.deleted == "/x/y.ics"


def test_delete_requires_href(patch_client):
    patch_client(FakeClient())
    out = json.loads(tool.handle_delete({}))
    assert "error" in out


def test_delete_error_wrapped(patch_client):
    patch_client(FakeClient(raise_on="delete"))
    out = json.loads(tool.handle_delete({"event_href": "/x/y.ics"}))
    assert out["error"] == "delete boom"


def test_handlers_accept_kwargs(patch_client):
    patch_client(FakeClient(events=[]))
    # Registry calls handler(args, **kwargs); ensure the extra kwargs don't break anything.
    assert json.loads(tool.handle_list({}, agent="x", session="y"))["count"] == 0


# -- calendars --------------------------------------------------------------


def test_list_calendars(patch_client):
    from hermes_yandex_calendar.caldav import Calendar

    fake = FakeClient(calendars=[Calendar("/cal/work/", "Work"), Calendar("/cal/home/", "Home")])
    patch_client(fake)
    out = json.loads(tool.handle_list_calendars({}))
    assert out["count"] == 2
    assert out["calendars"][0] == {"name": "Work", "href": "/cal/work/"}


def test_list_calendars_error(patch_client):
    patch_client(FakeClient(raise_on="list_calendars"))
    out = json.loads(tool.handle_list_calendars({}))
    assert out["error"] == "cal boom"


def test_list_passes_calendar_arg(patch_client):
    fake = FakeClient(events=[])
    patch_client(fake)
    tool.handle_list({"calendar": "Work"})
    assert fake.list_calendar_arg == "Work"


def test_create_passes_calendar_and_attendees(patch_client):
    fake = FakeClient()
    patch_client(fake)
    out = json.loads(
        tool.handle_create(
            {
                "summary": "Meet",
                "start": "2026-07-25T10:00:00Z",
                "calendar": "Personal",
                "attendees": ["ann@x.ru", "Bob <bob@x.ru>", {"email": "cara@x.ru", "name": "Cara"}],
                "busy": False,
            }
        )
    )
    assert out["created"] is True
    assert [a.email for a in fake.created.attendees] == ["ann@x.ru", "bob@x.ru", "cara@x.ru"]
    assert fake.created.attendees[1].name == "Bob"
    assert fake.created.transp == "TRANSPARENT"
    assert out["event"]["busy"] is False


# -- update -----------------------------------------------------------------


def test_update_changes_fields(patch_client):
    existing = Event(uid="e1", summary="Old", location="A")
    fake = FakeClient(get_result=existing)
    patch_client(fake)
    out = json.loads(
        tool.handle_update(
            {
                "event_href": "/cal/e1.ics",
                "summary": "New",
                "location": "B",
                "start": "2026-08-01T09:00:00Z",
                "busy": True,
            }
        )
    )
    assert out["updated"] is True
    assert fake.updated.summary == "New"
    assert fake.updated.location == "B"
    assert fake.updated.transp == "OPAQUE"
    assert fake.update_href == "/cal/e1.ics"


def test_update_adds_and_removes_attendees(patch_client):
    from hermes_yandex_calendar.ical import Attendee

    existing = Event(uid="e1", summary="Sync", attendees=[Attendee(email="old@x.ru")])
    fake = FakeClient(get_result=existing)
    patch_client(fake)
    tool.handle_update(
        {
            "event_href": "/cal/e1.ics",
            "add_attendees": ["new@x.ru", "old@x.ru"],  # duplicate old is ignored
            "remove_attendees": ["OLD@x.ru"],  # case-insensitive removal
        }
    )
    emails = [a.email for a in fake.updated.attendees]
    assert emails == ["new@x.ru"]


def test_update_requires_href(patch_client):
    patch_client(FakeClient())
    out = json.loads(tool.handle_update({}))
    assert "error" in out


def test_update_event_not_found(patch_client):
    patch_client(FakeClient(get_result=None))
    out = json.loads(tool.handle_update({"event_href": "/cal/missing.ics"}))
    assert "not found" in out["error"]


def test_update_error_wrapped(patch_client):
    patch_client(FakeClient(raise_on="update"))
    out = json.loads(tool.handle_update({"event_href": "/cal/e1.ics", "summary": "x"}))
    assert out["error"] == "update boom"


def test_update_bad_date(patch_client):
    patch_client(FakeClient(get_result=Event(uid="e1")))
    out = json.loads(tool.handle_update({"event_href": "/cal/e1.ics", "start": "nonsense"}))
    assert "error" in out


# -- respond / move ---------------------------------------------------------


def test_respond_accept(patch_client):
    fake = FakeClient()
    patch_client(fake)
    out = json.loads(tool.handle_respond({"event_href": "/cal/e.ics", "response": "accept"}))
    assert out["responded"] is True
    assert out["status"] == "ACCEPTED"
    assert fake.respond_args == ("/cal/e.ics", "ACCEPTED")


def test_respond_decline_and_tentative(patch_client):
    fake = FakeClient()
    patch_client(fake)
    assert (
        json.loads(tool.handle_respond({"event_href": "/x", "response": "decline"}))["status"]
        == "DECLINED"
    )
    assert (
        json.loads(tool.handle_respond({"event_href": "/x", "response": "tentative"}))["status"]
        == "TENTATIVE"
    )


def test_respond_bad_response(patch_client):
    patch_client(FakeClient())
    out = json.loads(tool.handle_respond({"event_href": "/x", "response": "maybe-later"}))
    assert "error" in out


def test_respond_requires_href(patch_client):
    patch_client(FakeClient())
    assert "error" in json.loads(tool.handle_respond({"response": "accept"}))


def test_respond_error_wrapped(patch_client):
    patch_client(FakeClient(raise_on="respond"))
    out = json.loads(tool.handle_respond({"event_href": "/x", "response": "accept"}))
    assert out["error"] == "respond boom"


def test_move_ok(patch_client):
    fake = FakeClient()
    patch_client(fake)
    out = json.loads(tool.handle_move({"event_href": "/cal/a/e.ics", "calendar": "Personal"}))
    assert out["moved"] is True
    assert fake.move_args == ("/cal/a/e.ics", "Personal")


def test_move_requires_calendar(patch_client):
    patch_client(FakeClient())
    assert "error" in json.loads(tool.handle_move({"event_href": "/x"}))


def test_move_requires_href(patch_client):
    patch_client(FakeClient())
    assert "error" in json.loads(tool.handle_move({"calendar": "Work"}))


def test_move_error_wrapped(patch_client):
    patch_client(FakeClient(raise_on="move"))
    out = json.loads(tool.handle_move({"event_href": "/x", "calendar": "Work"}))
    assert out["error"] == "move boom"


def test_attendee_object_missing_email(patch_client):
    patch_client(FakeClient())
    out = json.loads(
        tool.handle_create(
            {"summary": "x", "start": "2026-07-25T10:00:00Z", "attendees": [{"name": "No Email"}]}
        )
    )
    assert "error" in out and "email" in out["error"]
