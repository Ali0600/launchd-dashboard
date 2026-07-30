"""Text-level guards on the dashboard page script.

The page is a hand-written string of HTML+JS with no JS test runner, so the few
constraints that are invisible at the point they matter get pinned here.
"""

import re

from app.main import PAGE

PANEL_CHILDREN = ("log", "logpath", "lognote", "follow")


def script() -> str:
    m = re.search(r"<script>(.*)</script>", PAGE, re.S)
    assert m, "the page must carry an inline <script>"
    return m.group(1)


def test_log_panel_is_reached_through_a_held_reference():
    """The panel is moved under the clicked row, so it lives inside a list whose
    innerHTML the 30s poll replaces — which DESTROYS it. A fresh getElementById
    then returns null and `row.after(null)` injects a literal "null" into the page
    (observed live). The node must be held in a variable, and its children reached
    through it."""
    js = script()
    capture = 'const logPanel = $("logwrap");'
    assert capture in js
    assert '$("logwrap")' not in js.split(capture, 1)[1], (
        "after capturing it, use `logPanel` — a fresh $(\"logwrap\") is null once a "
        "re-render has destroyed the parked node"
    )
    for child in PANEL_CHILDREN:
        assert f'$("{child}")' not in js, (
            f'reach #{child} via logPart("{child}") — $("{child}") is null while the '
            "panel is detached between innerHTML replacement and reattachLog()"
        )


def test_both_list_renderers_reattach_the_panel():
    """Each renderer that replaces a list's innerHTML must re-place the panel, or an
    open log silently disappears on the next poll."""
    js = script()
    for anchor in ('$("list").innerHTML = agents.map', '$("applist").innerHTML = apps.map'):
        assert anchor in js
        tail = js.split(anchor, 1)[1]
        # reattachLog() must appear before the next function declaration ends the renderer
        head = tail.split("\nasync function", 1)[0].split("\nfunction", 1)[0]
        assert "reattachLog()" in head, f"missing reattachLog() after `{anchor}`"


def test_rows_carry_the_log_key_the_panel_is_placed_by():
    """placeLog() finds its row by data-log-key; both row templates must emit one,
    matching the keys openLogPanel is called with (label / app:<slug>)."""
    js = script()
    assert 'data-log-key="${a.label}"' in js
    assert 'data-log-key="app:${a.slug}"' in js
    assert '[data-log-key="${openLog}"]' in js
