"""Status badges: the SVG renderer, the verdicts, and the badge routes.

The renderer and verdict tables in ``cronstable/badge.py`` are pure, so
they are driven directly.  The handlers are called with the small mock
request the other web tests use, and the auth cases (private by default,
public by opt-in, off entirely) run against a real listener, because the
public exemption matches the route the router picked.
"""

import asyncio
import datetime
import json
import math
import os
import re
import xml.etree.ElementTree as ET

import pytest
from aiohttp import web

from cronstable import badge
from cronstable.config import ConfigError, parse_config_string
from cronstable.cron import (
    SLA_CHECK_LATE,
    WEB_BADGE_PATHS,
    Cron,
    JobRunInfo,
    PauseInfo,
    _route_canonical,
    _web_public_paths,
)
from cronstable.job import JobOutputStream
from tests.conftest import Req, _cron

_UTC = datetime.timezone.utc
_SVG = "{http://www.w3.org/2000/svg}"

_JOBS = """
jobs:
  - name: backup
    command: "true"
    schedule: "0 3 * * *"
  - name: report
    command: "true"
    schedule: "0 6 * * *"
    sla:
      lateAfterSeconds: 600
  - name: parked
    command: "true"
    schedule: "0 0 * * *"
    enabled: false
"""


def _run(outcome):
    now = datetime.datetime.now(_UTC)
    return JobRunInfo(
        outcome=outcome,
        exit_code=0 if outcome == "success" else 1,
        started_at=now - datetime.timedelta(seconds=2),
        finished_at=now,
        fail_reason=None if outcome == "success" else "exit 1",
        output=JobOutputStream(),
    )


def _texts(svg):
    """The visible text runs of a rendered badge, in document order."""
    root = ET.fromstring(svg)
    return [
        el.text
        for el in root.iter(_SVG + "text")
        if el.get("aria-hidden") != "true"
    ]


@pytest.fixture
async def start_web_app():
    """Start a cron's real web app with teardown guaranteed, like the
    fixture of the same name in tests/test_cron_web.py."""
    crons = []

    async def start(cron, config):
        crons.append(cron)
        await cron.start_stop_web_app(config)
        return "http://127.0.0.1:{}".format(cron.web_runner.addresses[0][1])

    yield start
    for cron in reversed(crons):
        await cron.start_stop_web_app(None)
    # the Windows Proactor's transports finish closing (see test_cron_web)
    await asyncio.sleep(0.25)


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(None, {"public": False}, id="absent-is-on"),
        pytest.param(True, {"public": False}, id="true"),
        pytest.param(False, None, id="false"),
        pytest.param({}, {"public": False}, id="empty-map"),
        pytest.param({"enabled": False}, None, id="map-disabled"),
        pytest.param({"public": True}, {"public": True}, id="public"),
        pytest.param(
            {"enabled": True, "public": False}, {"public": False}, id="map"
        ),
    ],
)
def test_resolve_badges_config(raw, expected):
    web_config = {} if raw is None else {"badges": raw}
    assert badge.resolve_badges_config(web_config) == expected


_WEB = "web:\n  listen:\n    - http://127.0.0.1:8080\n"


@pytest.mark.parametrize(
    "block",
    [
        "  badges: false\n",
        "  badges: true\n",
        "  badges:\n    public: true\n",
        "  badges:\n    enabled: false\n    public: false\n",
    ],
)
def test_web_badges_config_forms_parse(block):
    conf = parse_config_string(_WEB + block, "")
    assert "badges" in conf.web_config


@pytest.mark.parametrize(
    "block",
    [
        pytest.param("  badges:\n    public: maybe\n", id="not-a-bool"),
        pytest.param("  badges:\n    colour: red\n", id="unknown-key"),
        pytest.param("  badges: sometimes\n", id="not-bool-or-map"),
    ],
)
def test_web_badges_config_rejects_bad_values(block):
    with pytest.raises(ConfigError):
        parse_config_string(_WEB + block, "")


# ---------------------------------------------------------------------------
# verdicts
# ---------------------------------------------------------------------------


def _verdict(**over):
    state = {
        "enabled": True,
        "paused": False,
        "outcome": None,
        "overdue": False,
        "running": False,
    }
    state.update(over)
    return badge.job_verdict(**state)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({}, "pending"),
        ({"running": True}, "running"),
        ({"outcome": "success"}, "passing"),
        # a later run in progress keeps the verdict
        ({"outcome": "success", "running": True}, "passing"),
        ({"outcome": "failure", "running": True}, "failing"),
        ({"outcome": "failure"}, "failing"),
        ({"outcome": "cancelled"}, "cancelled"),
        ({"outcome": "skipped"}, "skipped"),
        ({"outcome": "unknown"}, "unknown"),
        ({"outcome": "something-new"}, "unknown"),
        # overdue outranks a good or neutral result, not a failure
        ({"outcome": "success", "overdue": True}, "overdue"),
        ({"overdue": True}, "overdue"),
        ({"outcome": "failure", "overdue": True}, "failing"),
        # paused and disabled outrank every result
        ({"outcome": "failure", "paused": True}, "paused"),
        ({"outcome": "failure", "enabled": False}, "disabled"),
        ({"paused": True, "enabled": False}, "disabled"),
    ],
)
def test_job_verdict_precedence(state, expected):
    assert _verdict(**state) == expected


def test_every_verdict_has_a_color():
    seen = set()
    for enabled in (True, False):
        for paused in (True, False):
            for outcome in (
                None,
                "success",
                "failure",
                "cancelled",
                "skipped",
                "unknown",
            ):
                for overdue in (True, False):
                    for running in (True, False):
                        seen.add(
                            badge.job_verdict(
                                enabled=enabled,
                                paused=paused,
                                outcome=outcome,
                                overdue=overdue,
                                running=running,
                            )
                        )
    assert seen == set(badge.VERDICT_COLORS)


@pytest.mark.parametrize(
    ("verdicts", "expected"),
    [
        ([], ("no jobs", "#9f9f9f")),
        (["passing"] * 12, ("12 passing", "#4c1")),
        (
            ["passing", "failing", "failing", "overdue"],
            ("2 failing", "#e05d44"),
        ),
        (["passing", "overdue", "paused"], ("1 overdue", "#fe7d37")),
        (["passing", "pending", "disabled"], ("1 passing", "#4c1")),
        (["pending", "paused", "disabled"], ("no status", "#9f9f9f")),
    ],
)
def test_fleet_message(verdicts, expected):
    assert badge.fleet_message(iter(verdicts)) == expected


# ---------------------------------------------------------------------------
# the renderer
# ---------------------------------------------------------------------------


def test_text_width_uses_the_table_and_the_fallbacks():
    # p a s s i n g in DejaVu Sans at 11px
    assert badge.text_width("passing") == pytest.approx(42.2)
    assert badge.text_width("") == 0.0
    # a combining mark adds nothing, a wide CJK character a full em
    assert badge.text_width("e\u0301") == pytest.approx(6.8)
    assert badge.text_width("\u65e5") == 11.0
    # any other character outside the table gets the fallback width
    assert badge.text_width("\u00e9") == 7.0


@pytest.mark.parametrize("style", badge.BADGE_STYLES)
def test_render_badge_is_well_formed_and_labelled(style):
    svg = badge.render_badge("nightly-backup", "passing", "#4c1", style)
    root = ET.fromstring(svg)
    assert root.tag == _SVG + "svg"
    assert root.get("role") == "img"
    assert root.get("aria-label") == "nightly-backup: passing"
    assert root.find(_SVG + "title").text == "nightly-backup: passing"
    assert _texts(svg) == ["nightly-backup", "passing"]
    # the two halves: 5px of padding around each measured text run
    label_width = math.ceil(badge.text_width("nightly-backup")) + 10
    message_width = math.ceil(badge.text_width("passing")) + 10
    assert root.get("width") == str(label_width + message_width)
    assert root.get("height") == "20"
    rects = [r for r in root.iter(_SVG + "rect") if r.get("fill")]
    fills = [r.get("fill") for r in rects]
    assert "#555" in fills and "#4c1" in fills


def test_flat_square_has_no_gradient_shadow_or_rounding():
    flat = badge.render_badge("a", "b", "#4c1", "flat")
    square = badge.render_badge("a", "b", "#4c1", "flat-square")
    assert "linearGradient" in flat and 'rx="3"' in flat
    assert 'aria-hidden="true"' in flat
    assert "linearGradient" not in square and "rx=" not in square
    assert 'aria-hidden="true"' not in square
    assert 'shape-rendering="crispEdges"' in square


def test_render_badge_refuses_an_unknown_style():
    with pytest.raises(ValueError, match="unknown badge style"):
        badge.render_badge("a", "b", "#4c1", "plastic")


def test_render_badge_escapes_markup():
    label = "<script>alert(1)</script> & \"q\" 's'"
    svg = badge.render_badge(label, "passing", "#4c1")
    assert "<script>" not in svg
    # the parsed document carries the text back verbatim
    assert _texts(svg)[0] == label
    assert ET.fromstring(svg).get("aria-label") == label + ": passing"


def test_render_badge_replaces_characters_xml_forbids():
    # a job name is any YAML string, control characters included; the
    # image must stay a well-formed document
    svg = badge.render_badge("bell\x07name", "pass\x00ing", "#4c1")
    assert _texts(svg) == ["bell\ufffdname", "pass\ufffding"]
    # tab, newline, and characters past the BMP are legal and kept
    svg = badge.render_badge("tab\there", "\U0001f600", "#4c1")
    assert _texts(svg) == ["tab\there", "\U0001f600"]


@pytest.mark.parametrize(
    ("label", "error"),
    [
        ("", "label must not be blank"),
        ("   ", "label must not be blank"),
        ("x" * 65, "label must be at most 64 characters"),
        ("tab\there", "label must contain only printable characters"),
        ("nul\x00", "label must contain only printable characters"),
        ("x" * 64, None),
        ("nightly backup (prod) ✓", None),
        ("日本語", None),
    ],
)
def test_label_error(label, error):
    assert badge.label_error(label) == error


@pytest.mark.parametrize(
    ("base", "name", "expected"),
    [
        (
            "https://cron.example.com",
            None,
            "[![cronstable](https://cron.example.com/badge.svg)]"
            "(https://cron.example.com/)",
        ),
        (
            "https://cron.example.com/",
            "nightly-backup",
            "[![nightly-backup]"
            "(https://cron.example.com/jobs/nightly-backup/badge.svg)]"
            "(https://cron.example.com/#job/nightly-backup)",
        ),
        (
            "http://h:8080",
            "odd [name] (x)\\y/z",
            "[![odd \\[name\\] (x)\\\\y/z]"
            "(http://h:8080/jobs/odd%20%5Bname%5D%20%28x%29%5Cy%2Fz/badge.svg)]"
            "(http://h:8080/#job/odd%20%5Bname%5D%20%28x%29%5Cy%2Fz)",
        ),
    ],
)
def test_badge_markdown(base, name, expected):
    assert badge.badge_markdown(base, name) == expected


# ---------------------------------------------------------------------------
# the handlers
# ---------------------------------------------------------------------------


async def _job_badge(cron, name, **req):
    return await cron._web_job_badge(Req(match={"name": name}, **req))


async def test_job_badge_reads_the_scheduler_state():
    cron = _cron(_JOBS)
    now = datetime.datetime.now(_UTC)

    async def message(name):
        return _texts((await _job_badge(cron, name)).text)[1]

    assert await message("backup") == "pending"
    cron.running_jobs["backup"] = [object()]
    assert await message("backup") == "running"
    cron.last_run["backup"] = _run("success")
    assert await message("backup") == "passing"
    cron.last_run["backup"] = _run("failure")
    assert await message("backup") == "failing"
    del cron.running_jobs["backup"]
    cron._paused["backup"] = PauseInfo(
        since=now,
        until=now + datetime.timedelta(hours=1),
        note="",
        by="t",
        channel="api",
    )
    assert await message("backup") == "paused"
    # an expired pause no longer counts
    cron._paused["backup"] = PauseInfo(
        since=now - datetime.timedelta(hours=2),
        until=now - datetime.timedelta(hours=1),
        note="",
        by="t",
        channel="api",
    )
    assert await message("backup") == "failing"
    assert await message("parked") == "disabled"
    # an SLA latch makes a passing job overdue
    cron.last_run["report"] = _run("success")
    assert await message("report") == "passing"
    cron._sla_state[("report", SLA_CHECK_LATE)] = now
    assert await message("report") == "overdue"


async def test_job_badge_response_headers():
    cron = _cron(_JOBS)
    cron.web_config = {
        "headers": {
            "X-Operator": "kept",
            "content-type": "text/plain",
            "Content-Security-Policy": "default-src 'self'",
        }
    }
    resp = await _job_badge(cron, "backup")
    assert resp.status == 200
    assert resp.content_type == "image/svg+xml"
    assert resp.charset == "utf-8"
    assert resp.headers["Cache-Control"] == "no-cache"
    assert re.fullmatch(r'"[0-9a-f]{32}"', resp.headers["ETag"])
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Operator"] == "kept"
    # an operator's header wins over the image's default, as on the page
    assert resp.headers["Content-Security-Policy"] == "default-src 'self'"
    assert _texts(resp.text) == ["backup", "pending"]


async def test_job_badge_default_security_policy():
    cron = _cron(_JOBS)
    resp = await _job_badge(cron, "backup")
    assert resp.headers["Content-Security-Policy"] == "default-src 'none'"


async def test_job_badge_revalidates_with_the_etag():
    cron = _cron(_JOBS)
    first = await _job_badge(cron, "backup")
    etag = first.headers["ETag"]
    for header in (etag, "W/" + etag, '"other", ' + etag):
        again = await _job_badge(
            cron, "backup", headers={"If-None-Match": header}
        )
        assert again.status == 304
        assert again.body is None
        assert again.headers["ETag"] == etag
    # a new verdict is a new image, so the old tag no longer matches
    cron.last_run["backup"] = _run("success")
    changed = await _job_badge(cron, "backup", headers={"If-None-Match": etag})
    assert changed.status == 200
    assert changed.headers["ETag"] != etag


async def test_job_badge_label_and_style_overrides():
    cron = _cron(_JOBS)
    resp = await _job_badge(
        cron, "backup", query={"label": "nightly backup", "style": ""}
    )
    assert _texts(resp.text) == ["nightly backup", "pending"]
    assert "linearGradient" in resp.text
    square = await _job_badge(cron, "backup", query={"style": "flat-square"})
    assert "linearGradient" not in square.text


@pytest.mark.parametrize(
    ("query", "error"),
    [
        ({"label": ""}, "label must not be blank"),
        ({"label": "x" * 65}, "label must be at most 64 characters"),
        ({"label": "a\nb"}, "label must contain only printable characters"),
        ({"style": "plastic"}, "style must be one of: flat, flat-square"),
    ],
)
async def test_badge_bad_query_is_a_400(query, error):
    cron = _cron(_JOBS)
    for resp in (
        await _job_badge(cron, "backup", query=query),
        await cron._web_fleet_badge(Req(query=query)),
    ):
        assert resp.status == 400
        assert json.loads(resp.text) == {"error": error}


async def test_job_badge_unknown_job_is_a_404():
    cron = _cron(_JOBS)
    with pytest.raises(web.HTTPNotFound) as raised:
        await _job_badge(cron, "nope")
    assert json.loads(raised.value.text or "") == {
        "error": "job 'nope' not found"
    }


async def test_fleet_badge_counts_the_job_verdicts():
    cron = _cron(_JOBS)

    async def texts(**req):
        return _texts((await cron._web_fleet_badge(Req(**req))).text)

    # nothing has run, and the disabled job shows no result either
    assert await texts() == ["cronstable", "no status"]
    cron.last_run["backup"] = _run("success")
    cron.last_run["report"] = _run("success")
    assert await texts() == ["cronstable", "2 passing"]
    cron._sla_state[("report", SLA_CHECK_LATE)] = datetime.datetime.now(_UTC)
    assert await texts() == ["cronstable", "1 overdue"]
    cron.last_run["backup"] = _run("failure")
    assert await texts() == ["cronstable", "1 failing"]
    # a failed run of a disabled job does not color the fleet
    cron.last_run["parked"] = _run("failure")
    assert await texts() == ["cronstable", "1 failing"]
    assert await texts(query={"label": "prod"}) == ["prod", "1 failing"]


async def test_fleet_badge_with_no_jobs():
    cron = _cron("")
    resp = await cron._web_fleet_badge(Req())
    assert _texts(resp.text) == ["cronstable", "no jobs"]


# ---------------------------------------------------------------------------
# auth: private by default, public by opt-in
# ---------------------------------------------------------------------------


def test_web_public_paths_composition():
    on = {"public": True}
    off = {"public": False}
    assert _web_public_paths(True, None, None) == {"/"}
    assert _web_public_paths(False, None, off) == frozenset()
    assert _web_public_paths(False, on, None) == {"/metrics"}
    assert _web_public_paths(True, off, on) == {"/"} | WEB_BADGE_PATHS


def test_route_canonical_tolerates_a_bare_request():
    class Bare:
        path = "/jobs/x/badge.svg"

    assert _route_canonical(Bare()) is None


_TOKEN = {"value": "s3cret", "fromFile": None, "fromEnvVar": None}


async def _status(session, url, token=None):
    headers = {"Authorization": "Bearer " + token} if token else None
    async with session.get(url, headers=headers) as resp:
        await resp.read()
        return resp.status


async def test_badges_require_the_token_by_default(start_web_app):
    import aiohttp

    cron = Cron(None, config_yaml=_JOBS)
    base = await start_web_app(
        cron, {"listen": ["http://127.0.0.1:0"], "authToken": _TOKEN}
    )
    async with aiohttp.ClientSession() as session:
        assert await _status(session, base + "/badge.svg") == 401
        assert await _status(session, base + "/jobs/backup/badge.svg") == 401
        assert await _status(session, base + "/badge.svg", "s3cret") == 200
        assert (
            await _status(session, base + "/jobs/backup/badge.svg", "s3cret")
            == 200
        )


async def test_public_badges_open_only_the_badge_routes(start_web_app):
    import aiohttp

    cron = Cron(None, config_yaml=_JOBS)
    base = await start_web_app(
        cron,
        {
            "listen": ["http://127.0.0.1:0"],
            "authToken": _TOKEN,
            "badges": {"public": True},
        },
    )
    async with aiohttp.ClientSession() as session:
        async with session.get(base + "/jobs/backup/badge.svg") as resp:
            assert resp.status == 200
            assert resp.content_type == "image/svg+xml"
            assert _texts(await resp.text()) == ["backup", "pending"]
        assert await _status(session, base + "/badge.svg") == 200
        # HEAD rides along with GET, as for every read route
        async with session.head(base + "/badge.svg") as resp:
            assert resp.status == 200
        # an unknown job is still an honest 404, not a 401
        assert await _status(session, base + "/jobs/nope/badge.svg") == 404
        # every other route keeps the token gate
        for path in ("/jobs", "/jobs/backup", "/summary", "/metrics"):
            assert await _status(session, base + path) == 401


async def test_disabled_badges_are_not_routed(start_web_app):
    import aiohttp

    cron = Cron(None, config_yaml=_JOBS)
    base = await start_web_app(
        cron, {"listen": ["http://127.0.0.1:0"], "badges": False}
    )
    async with aiohttp.ClientSession() as session:
        assert await _status(session, base + "/badge.svg") == 404
        assert await _status(session, base + "/jobs/backup/badge.svg") == 404
        assert await _status(session, base + "/jobs/backup") == 200


async def test_anonymous_view_serves_badges(start_web_app):
    import aiohttp

    cron = Cron(None, config_yaml=_JOBS)
    base = await start_web_app(
        cron,
        {
            "listen": ["http://127.0.0.1:0"],
            "authToken": _TOKEN,
            "anonymousScopes": ["view"],
        },
    )
    async with aiohttp.ClientSession() as session:
        assert await _status(session, base + "/jobs/backup/badge.svg") == 200


_ODD_NAME = """
jobs:
  - name: team/backup (prod)
    command: "true"
    schedule: "0 3 * * *"
"""


async def test_copied_markdown_image_url_reaches_the_badge(start_web_app):
    # the dashboards percent-encode the name, "/" and ")" included, and
    # the router decodes it back into one path segment
    import aiohttp

    cron = Cron(None, config_yaml=_ODD_NAME)
    base = await start_web_app(cron, {"listen": ["http://127.0.0.1:0"]})
    markdown = badge.badge_markdown(base, "team/backup (prod)")
    image = re.search(r"\]\((\S+?/badge\.svg)\)\]", markdown).group(1)
    async with aiohttp.ClientSession() as session:
        async with session.get(image) as resp:
            assert resp.status == 200
            assert _texts(await resp.text()) == [
                "team/backup (prod)",
                "pending",
            ]


# ---------------------------------------------------------------------------
# the dashboards' copy actions
# ---------------------------------------------------------------------------

_PAGE = os.path.join(os.path.dirname(badge.__file__), "web", "index.html")


def test_page_palette_offers_badge_markdown():
    with open(_PAGE, encoding="utf-8") as fh:
        page = fh.read()
    assert 'lbl: "Copy badge Markdown: " + j.name' in page
    # the fleet entry sits after "Copy job-set ID", as in the terminal
    labels = re.findall(r'lbl: "([^"]+)"', page)
    at = labels.index("Copy fleet badge Markdown")
    assert labels[at - 1] == "Copy job-set ID"


def test_tui_palette_copies_badge_markdown(tmp_path):
    from tests.test_tui import _bare_app, _job

    app = _bare_app(tmp_path)
    app.jobs = [_job("nightly backup")]
    app.by_name = {j["name"]: j for j in app.jobs}
    commands = app.palette_commands()
    labels = [label for _icon, label, _run in commands]
    at = labels.index("Copy fleet badge Markdown")
    assert labels[at - 1] == "Copy job-set ID"
    run = {label: action for _icon, label, action in commands}
    run["Copy badge Markdown: nightly backup"]()
    run["Copy fleet badge Markdown"]()
    assert app.term.copied == [
        badge.badge_markdown(app.api.url, "nightly backup"),
        badge.badge_markdown(app.api.url),
    ]
