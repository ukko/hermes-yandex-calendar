# hermes-yandex-calendar

[![PyPI version](https://img.shields.io/pypi/v/hermes-yandex-calendar.svg)](https://pypi.org/project/hermes-yandex-calendar/)
[![CI](https://github.com/akinfold/hermes-yandex-calendar/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/akinfold/hermes-yandex-calendar/actions/workflows/ci.yml)
[![E2E (live)](https://github.com/akinfold/hermes-yandex-calendar/actions/workflows/e2e.yml/badge.svg)](https://github.com/akinfold/hermes-yandex-calendar/actions/workflows/e2e.yml)
[![Coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/akinfold/hermes-yandex-calendar/badges/coverage.json&v=1)](https://github.com/akinfold/hermes-yandex-calendar/actions/workflows/ci.yml)
[![CodeFactor](https://www.codefactor.io/repository/github/akinfold/hermes-yandex-calendar/badge)](https://www.codefactor.io/repository/github/akinfold/hermes-yandex-calendar)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Let your [Hermes Agent](https://hermes-agent.nousresearch.com) run your Yandex
Calendar.** *"What's on my calendar next week?"* — *"Move the standup to Thursday
and invite Ann."* — *"Decline the 4pm."* The agent reads and writes real events on
your real calendar, over CalDAV, with no third-party service in the middle.

- 📅 **Seven tools, one toolset** — list, create, update, RSVP, move between
  calendars, delete, and enumerate the calendars themselves.
- 🔒 **You choose what it may touch** — restrict it to specific calendars, and to
  specific actions (`read`, `read,write`, …). A disallowed action is not in the
  toolset at all.
- 🛟 **Careful with your data** — recurrence rules, alarms, and properties this
  plugin does not model survive every edit; moves copy the resource byte for byte;
  an event is deleted only after its copy is safely in place.
- 🔑 **App password, not your account password** — scoped to CalDAV, revocable in
  one click.

Tested against Hermes **0.19.x**, Python **3.11–3.13**.

## Quick start

```bash
# 1. Install into Hermes (alternatively: pip install hermes-yandex-calendar)
hermes plugins install akinfold/hermes-yandex-calendar --enable

# 2. Add your credentials — the app password comes from
#    https://id.yandex.ru/security/app-passwords (scope: "Calendar (CalDAV)")
umask 077
printf 'YANDEX_CALENDAR_LOGIN=%s\nYANDEX_CALENDAR_APP_PASSWORD=%s\n' \
  'you@yandex.ru' 'your-app-password' >> ~/.hermes/.env
chmod 600 ~/.hermes/.env
```

Then enable it in `~/.hermes/config.yaml` (third-party plugins are off by default):

```yaml
plugins:
  enabled: [yandex_calendar]
```

That's it. Ask the agent *"what do I have tomorrow?"* and it will tell you.

> The backward-compatible default exposes every action, including delete. For a
> new or untrusted agent setup, start with `YANDEX_CALENDAR_ACTIONS=read` — see
> [Security boundaries](#security-boundaries).

## The tools

Up to seven standalone tools, in the `yandex_calendar` toolset:

| Tool | Purpose |
|---|---|
| `yandex_calendar_list_calendars` | List the calendars the plugin can use (name + `href`). |
| `yandex_calendar_list_events` | List events in a time range (summary, start/end, location, description, attendees, busy status, and an `href`). |
| `yandex_calendar_create_event` | Create an event (summary, start, optional end/location/description/all-day, attendees, busy status, target calendar). |
| `yandex_calendar_update_event` | Edit an event by `href`: change fields, add/remove attendees, toggle busy/free. Recurrence rules and alarms are preserved. |
| `yandex_calendar_respond_event` | Respond to a meeting invitation — accept, decline, or tentatively accept. |
| `yandex_calendar_move_event` | Move an event to another calendar, contents intact. |
| `yandex_calendar_delete_event` | Delete an event by `href`. |

Yandex Calendar has no public REST API, so this plugin speaks **CalDAV**
(`https://caldav.yandex.ru`) directly — the same protocol Yandex's own docs point
third-party clients at. Nothing is proxied through anyone else's servers.

### Multiple calendars

Every tool that reads or writes events takes an optional `calendar` argument — a
calendar **name** (as returned by `yandex_calendar_list_calendars`) or its `href`.
Omit it to use the default (first) calendar. `update`, `move`, and `delete` identify
the event by its `href`, which already encodes the calendar it lives in.

Restrict which calendars the plugin may touch with `YANDEX_CALENDAR_CALENDARS`; the
first one in that list becomes the default.

## Configuration

| Env var | Required | Default | Meaning |
|---|---|---|---|
| `YANDEX_CALENDAR_LOGIN` | yes | — | Yandex login / email. |
| `YANDEX_CALENDAR_APP_PASSWORD` | yes | — | App password for CalDAV — an account password will not work. |
| `YANDEX_CALENDAR_BASE_URL` | no | `https://caldav.yandex.ru` | HTTPS override for self-hosted / testing. |
| `YANDEX_CALENDAR_CALENDARS` | no | *(all)* | Comma-separated allow-list of calendar names, e.g. `Work,Personal`. The first is the default calendar. |
| `YANDEX_CALENDAR_ACTIONS` | no | *(all)* | Comma-separated allow-list of actions the agent may perform — see below. |

Credentials are read from the environment first, then from `~/.hermes/.env`, so they
work in gateway and subprocess runs. Secret values are never logged.

Dates and times are ISO 8601 (`2026-07-25` or `2026-07-25T14:00:00+03:00`); a datetime
without an offset is treated as UTC.

### Restricting what the agent can do

`YANDEX_CALENDAR_ACTIONS` decides which of the seven tools are registered at all.
A disallowed action is not merely refused at call time: the tool never appears in
the agent's toolset, so it cannot be invoked, and the model is not tempted to try.

Accepted values, comma-separated and case-insensitive — individual actions
(`list_calendars`, `list_events`, `create_event`, `update_event`, `respond_event`,
`move_event`, `delete_event`), full tool names (`yandex_calendar_delete_event`), or
the shorthands:

| Shorthand | Expands to |
|---|---|
| `read` | `list_calendars`, `list_events` |
| `write` | `create_event`, `update_event`, `respond_event`, `move_event` |
| `delete` | `delete_event` |
| `all` | everything (the default) |

```dotenv
# Look, but don't touch:
YANDEX_CALENDAR_ACTIONS=read

# Full scheduling, but the agent can never delete anything:
YANDEX_CALENDAR_ACTIONS=read,write

# Just enough to answer invitations:
YANDEX_CALENDAR_ACTIONS=list_events,respond_event
```

Leave it unset for all seven tools. A name that matches nothing is ignored, so a
typo can only ever withhold a tool, never grant one — and a value that names
nothing recognisable therefore registers nothing at all. The list is applied when
the plugin loads: restart Hermes after changing it.

### Security boundaries

- CalDAV credentials are sent only to the HTTPS origin configured by
  `YANDEX_CALENDAR_BASE_URL`. Event hrefs pointing to HTTP or another origin are
  rejected before a request is made. Cross-origin redirects do not receive the
  authorization header.
- `YANDEX_CALENDAR_CALENDARS` applies to both target calendars and direct event
  operations (`update`, `respond`, `move`, and `delete`). Event paths are
  canonicalized before the allow-list check and the same canonical path is sent;
  moves validate both the source and destination calendar.
- Calendar titles, descriptions, locations, and attendee names are untrusted input.
  A capable agent can still act on misleading event content, so use `read` unless
  the agent and every calendar writer are trusted. Attendees supplied to a tool
  must be mailbox addresses, while server-provided calendar addresses are preserved
  for compatible invitation updates. Line breaks are rejected during serialization
  to prevent CRLF property injection.
- The action allow-list limits the tools exposed to Hermes; it does not reduce the
  privileges of the Yandex app password itself. Use a dedicated app password and,
  for stronger isolation, a dedicated Yandex account.

The default remains `all` for compatibility with existing installations. This is a
trusted-agent mode, not the recommended starting point for a new deployment.

## Getting the app password

CalDAV does not accept your normal account password.

1. Open <https://id.yandex.ru/security/app-passwords>.
2. Add a password with the **Calendar (CalDAV)** scope.
3. Copy it into `YANDEX_CALENDAR_APP_PASSWORD` — it is shown only once, and you can
   revoke it at any time without touching your account password.

If a tool answers *"Authentication failed"*, this is almost always the cause.

## Installing the plugin into Hermes

### Option A — from Git (recommended)

```bash
hermes plugins install akinfold/hermes-yandex-calendar --enable
```

### Option B — pip

```bash
pip install hermes-yandex-calendar
```

Hermes discovers it through the `hermes_agent.plugins` entry point; add
`yandex_calendar` to `plugins.enabled`.

### Option C — drop-in directory

Unzip the release archive into `~/.hermes/plugins/` so you end up with
`~/.hermes/plugins/yandex_calendar/plugin.yaml`, then enable it the same way.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
ruff check . && ruff format --check .
pytest                       # unit tests, no network
```

## Running the live E2E tests

The `e2e`-marked tests hit a real Yandex account and are deselected by default.
They create, edit, and then delete a throwaway event 400 days out, so a successful
run leaves nothing behind — but the attendees they invite do receive an invitation,
so use addresses you own.

### Locally

```bash
YANDEX_CALENDAR_LOGIN=you@yandex.ru \
YANDEX_CALENDAR_APP_PASSWORD=xxxx \
YC_E2E_ATTENDEES=you+guest@yandex.ru \
pytest -m e2e
```

`YC_E2E_ATTENDEES` is the comma-separated list of addresses the throwaway event
invites, and each one really is emailed an invitation — so list mailboxes you own.
Omit it and the suite falls back to a `+e2e` sub-address of the account itself,
which lands in your own inbox. Two constraints, both learned the hard way against
the live server:

- **The addresses must exist.** A made-up one (`guest@example.com`) bounces back
  into your mailbox.
- **They must not resolve to the account itself.** Yandex canonicalises addresses
  (`@ya.ru` → `@yandex.ru`) and drops an attendee that equals the `ORGANIZER`, so a
  plain alias of your own login silently disappears and the round-trip check fails.
  A `+tag` sub-address is delivered to the same mailbox but stays a distinct
  attendee.

Or keep all three out of the command line, in `~/.yandex-calendar-login`,
`~/.yandex-calendar-app-password`, and `~/.yandex-calendar-attendees`, and just run
`pytest -m e2e` — see `tests/e2e/conftest.py`.

### On GitHub Actions

The **E2E (live)** workflow is manual (`workflow_dispatch`). It reads
`YANDEX_CALENDAR_LOGIN`, `YANDEX_CALENDAR_APP_PASSWORD`, and `YC_E2E_ATTENDEES`
from a GitHub Environment named `yandex-calendar-e2e`.

## Related Hermes plugins

Part of a family of Yandex plugins for Hermes Agent:

- [hermes-yandex-disk](https://github.com/akinfold/hermes-yandex-disk) — browse, read, write, and share files on Yandex Disk (REST API).
- [hermes-yandex-mail](https://github.com/akinfold/hermes-yandex-mail) — search, read, flag, move, and delete Yandex Mail messages (IMAP).
- [hermes-yandex-search-api](https://github.com/akinfold/hermes-yandex-search-api) — Yandex web search backend and generative, cited answers for Hermes (Yandex Search API).

## Contributing

Issues and PRs are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for the layout,
the plugin contract rules worth knowing, and the release process.

## License

MIT — see [LICENSE](LICENSE).
