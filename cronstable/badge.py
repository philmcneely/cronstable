"""Status badges: small SVG images of a job's or the fleet's health.

:func:`render_badge` draws the two-part badge that shields.io made familiar
from CI pipelines: a gray label on the left, such as a job's name, and a
colored message on the right, such as ``passing`` or ``failing``.  The
daemon serves it as ``GET /jobs/{name}/badge.svg`` (one job) and
``GET /badge.svg`` (the fleet); the "Status Badges" wiki page is the user
documentation.  :func:`job_verdict` and :func:`fleet_message` decide what a
badge says, and :func:`resolve_badges_config` reads the ``web.badges``
option.

Rendering choices, all deliberate:

- No font travels with the image, so text widths come from a table of
  DejaVu Sans advance widths at 11px, which sit within a fraction of a
  pixel of Verdana's for these characters.  Each text run also carries
  ``textLength``, so a viewer that draws another font still fits the text
  to its box.
- Every string is XML-escaped, and characters that XML forbids become
  U+FFFD.  :func:`label_error` also limits a caller's label override to
  printable characters.  The daemon serves these images from the
  dashboard's own origin, so markup in a label must never reach the SVG.
- The image carries ``role="img"``, an ``aria-label``, and a ``<title>``
  that read "label: message", so screen readers and hover tooltips name
  the verdict.
"""

import html
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import quote

__all__ = [
    "BADGE_STYLES",
    "LABEL_MAX",
    "VERDICT_COLORS",
    "badge_markdown",
    "fleet_message",
    "job_verdict",
    "label_error",
    "render_badge",
    "resolve_badges_config",
    "text_width",
]

#: The accepted ``style`` values; the first is the default.
BADGE_STYLES = ("flat", "flat-square")

#: Longest label override a caller may pass, in characters.
LABEL_MAX = 64

# The shields.io palette, which readers already know from CI badges.
_GREEN = "#4c1"
_RED = "#e05d44"
_ORANGE = "#fe7d37"
_BLUE = "#007ec6"
_GRAY = "#9f9f9f"
_LABEL_COLOR = "#555"

#: The message color of each verdict :func:`job_verdict` returns.
VERDICT_COLORS: Mapping[str, str] = {
    "passing": _GREEN,
    "failing": _RED,
    "overdue": _ORANGE,
    "running": _BLUE,
    "paused": _GRAY,
    "disabled": _GRAY,
    "cancelled": _GRAY,
    "skipped": _GRAY,
    "unknown": _GRAY,
    "pending": _GRAY,
}

#: Advance widths, in pixels at font size 11, of the printable ASCII
#: characters in DejaVu Sans, grouped by width.
_WIDTH_GROUPS = (
    (3.0, "'"),
    (3.1, "ijl"),
    (3.2, "IJ"),
    (3.5, " ,."),
    (3.7, "/:;\\|"),
    (3.9, "f"),
    (4.0, "-"),
    (4.3, "()[]t"),
    (4.4, "!"),
    (4.5, "r"),
    (5.1, '"'),
    (5.5, "*_`"),
    (5.7, "s"),
    (5.8, "?z"),
    (6.0, "c"),
    (6.1, "L"),
    (6.3, "F"),
    (6.4, "k"),
    (6.5, "vxy"),
    (6.6, "P"),
    (6.7, "TYao"),
    (6.8, "e"),
    (7.0, "$0123456789ESbdghnpqu{}"),
    (7.2, "K"),
    (7.5, "ABVXZ"),
    (7.6, "R"),
    (7.7, "C"),
    (8.1, "U"),
    (8.2, "N"),
    (8.3, "H"),
    (8.5, "DG"),
    (8.6, "&"),
    (8.7, "OQ"),
    (9.0, "w"),
    (9.2, "#+<=>^~"),
    (9.5, "M"),
    (10.5, "%"),
    (10.7, "m"),
    (10.9, "W"),
    (11.0, "@"),
)
_WIDTHS = {char: width for width, chars in _WIDTH_GROUPS for char in chars}

#: Width of a character outside the table: about one lowercase letter, or
#: a full em for an East Asian wide or fullwidth character.
_FALLBACK_WIDTH = 7.0
_WIDE_WIDTH = 11.0

#: Space between a text run and the edges of its half of the badge.
_PADDING = 5

_FONT = "Verdana,Geneva,DejaVu Sans,sans-serif"


def resolve_badges_config(
    web_config: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Resolve the raw ``web.badges`` option into effective settings.

    Returns ``None`` when the badge routes are off, else a dict with
    ``public`` (serve the badges without a bearer token).  Badges are on
    whenever the web API is on unless the option turns them off;
    ``badges: false`` and ``badges: true`` are shorthands for the map form.
    """
    raw = web_config.get("badges")
    if raw is False:
        return None
    if raw is None or raw is True:
        raw = {}
    if not raw.get("enabled", True):
        return None
    return {"public": bool(raw.get("public", False))}


def job_verdict(
    *,
    enabled: bool,
    paused: bool,
    outcome: str | None,
    overdue: bool,
    running: bool,
) -> str:
    """The word a job's badge shows, most urgent state first.

    ``outcome`` is the outcome of the job's last recorded run, or ``None``
    when the job has none yet, and ``overdue`` is whether an SLA check is
    in breach.  A run in progress leaves a verdict in place, so the badge
    of a job that runs every minute keeps showing its last result.
    """
    if not enabled:
        return "disabled"
    if paused:
        return "paused"
    if outcome == "failure":
        return "failing"
    if overdue:
        return "overdue"
    if outcome == "success":
        return "passing"
    if outcome is None:
        return "running" if running else "pending"
    if outcome in ("cancelled", "skipped"):
        return outcome
    return "unknown"


def fleet_message(verdicts: Iterable[str]) -> tuple[str, str]:
    """The fleet badge's message and color, from every job's verdict.

    The message counts the most urgent of ``failing``, ``overdue``, and
    ``passing`` that any job shows, so ``2 failing`` outranks
    ``1 overdue``, which outranks ``12 passing``.  A fleet where no job
    shows one of the three reads ``no status``, and a fleet with no jobs
    reads ``no jobs``.
    """
    counts = Counter(verdicts)
    if not counts:
        return "no jobs", _GRAY
    for verdict in ("failing", "overdue", "passing"):
        count = counts[verdict]
        if count:
            return "{} {}".format(count, verdict), VERDICT_COLORS[verdict]
    return "no status", _GRAY


def label_error(label: str) -> str | None:
    """Why ``label`` cannot head a badge, or ``None`` when it can."""
    if not label.strip():
        return "label must not be blank"
    if len(label) > LABEL_MAX:
        return "label must be at most {} characters".format(LABEL_MAX)
    if not label.isprintable():
        return "label must contain only printable characters"
    return None


def badge_markdown(base_url: str, name: str | None = None) -> str:
    """Markdown that embeds a status badge and links it to the dashboard.

    ``base_url`` is the daemon's web address, such as
    ``https://cron.example.com``.  ``name`` picks one job's badge, which
    links to that job's drawer, and ``None`` picks the fleet badge, which
    links to the dashboard.  The web dashboard's ``badgeMarkdown`` builds
    the same text.
    """
    base = base_url.rstrip("/")
    if name is None:
        alt, image, link = "cronstable", "/badge.svg", "/"
    else:
        # safe="" also encodes "/", and the "(" and ")" that would end a
        # Markdown link early
        quoted = quote(name, safe="")
        alt = name
        image = "/jobs/{}/badge.svg".format(quoted)
        link = "/#job/{}".format(quoted)
    # a bracket in a job name would end the alt text early, and a
    # backslash would escape the character after it
    alt = re.sub(r"([\\\[\]])", r"\\\1", alt)
    return "[![{}]({}{})]({}{})".format(alt, base, image, base, link)


def text_width(text: str) -> float:
    """The estimated rendered width of ``text``, in pixels at size 11."""
    total = 0.0
    for char in text:
        width = _WIDTHS.get(char)
        if width is None:
            if unicodedata.combining(char):
                width = 0.0
            elif unicodedata.east_asian_width(char) in ("W", "F"):
                width = _WIDE_WIDTH
            else:
                width = _FALLBACK_WIDTH
        total += width
    return total


def _xml_char(char: str) -> str:
    """``char``, or U+FFFD when XML 1.0 forbids it in a document."""
    code = ord(char)
    if (
        code in (0x9, 0xA, 0xD)
        or 0x20 <= code <= 0xD7FF
        or 0xE000 <= code <= 0xFFFD
        or code >= 0x10000
    ):
        return char
    return "\ufffd"


def render_badge(
    label: str, message: str, color: str, style: str = "flat"
) -> str:
    """The SVG text of a badge that reads ``label`` beside ``message``.

    ``color`` fills the message half.  ``style`` is ``flat`` (rounded
    corners and a soft gradient) or ``flat-square`` (square corners, no
    gradient); any other value raises :class:`ValueError`.
    """
    if style not in BADGE_STYLES:
        raise ValueError("unknown badge style {!r}".format(style))
    label = "".join(_xml_char(char) for char in label)
    message = "".join(_xml_char(char) for char in message)
    # Text sits at font size 110 under scale(.1), so every coordinate below
    # is in tenths of a pixel: the shields.io idiom for sub-pixel centering
    # in integer attributes.
    label_text = math.ceil(text_width(label))
    message_text = math.ceil(text_width(message))
    label_width = label_text + 2 * _PADDING
    message_width = message_text + 2 * _PADDING
    width = label_width + message_width
    flat = style == "flat"
    title = html.escape(label + ": " + message)
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="{}" height="20" '
        'role="img" aria-label="{}">'.format(width, title),
        "<title>{}</title>".format(title),
    ]
    if flat:
        parts.append(
            '<linearGradient id="s" x2="0" y2="100%">'
            '<stop offset="0" stop-color="#bbb" stop-opacity=".1"/>'
            '<stop offset="1" stop-opacity=".1"/>'
            "</linearGradient>"
            '<clipPath id="r"><rect width="{}" height="20" rx="3" '
            'fill="#fff"/></clipPath>'
            '<g clip-path="url(#r)">'.format(width)
        )
    else:
        parts.append('<g shape-rendering="crispEdges">')
    parts.append(
        '<rect width="{}" height="20" fill="{}"/>'
        '<rect x="{}" width="{}" height="20" fill="{}"/>'.format(
            label_width,
            _LABEL_COLOR,
            label_width,
            message_width,
            html.escape(color),
        )
    )
    if flat:
        parts.append(
            '<rect width="{}" height="20" fill="url(#s)"/>'.format(width)
        )
    parts.append(
        '</g><g fill="#fff" text-anchor="middle" font-family="{}" '
        'text-rendering="geometricPrecision" font-size="110">'.format(_FONT)
    )
    runs = (
        (label, label_width * 5, label_text * 10),
        (message, label_width * 10 + message_width * 5, message_text * 10),
    )
    for text, center, length in runs:
        safe = html.escape(text)
        if flat:
            # the one-pixel drop shadow that lifts the text off the fill
            parts.append(
                '<text aria-hidden="true" x="{}" y="150" fill="#010101" '
                'fill-opacity=".3" transform="scale(.1)" '
                'textLength="{}">{}</text>'.format(center, length, safe)
            )
        parts.append(
            '<text x="{}" y="140" transform="scale(.1)" '
            'textLength="{}">{}</text>'.format(center, length, safe)
        )
    parts.append("</g></svg>")
    return "".join(parts)
