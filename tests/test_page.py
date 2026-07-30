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


def test_claimed_port_rows_render_without_a_kill_button():
    """A claimed port has no process behind it — offering ✕ would be a lie. It gets a
    start affordance instead, and only when a single app claims it (else ambiguous)."""
    js = script()
    branch = js.split('if (p.kind === "claimed")', 1)
    assert len(branch) == 2, "the ports renderer must special-case claimed rows"
    claimed_block = branch[1].split("const where =", 1)[0]
    assert "killPort" not in claimed_block
    assert "claimed_by.length === 1" in claimed_block
    assert "appAct(" in claimed_block


def test_port_checker_reports_free_but_declared():
    js = script()
    checker = js.split("function checkPort", 1)[1]
    assert 'p.kind !== "claimed"' in checker, "a claimed row must not read as taken"
    assert "free — declared by" in checker


def test_remove_arms_before_it_deletes():
    """Removal is destructive (stops the app, drops its config), so the first click must
    only arm the button — same two-tap contract as the port kill control."""
    js = script()
    fn = js.split("async function removeApp", 1)[1].split("\n}", 1)[0]
    assert "armedRemove !== slug" in fn, "first click must arm, not delete"
    arm_block = fn.split("armedRemove !== slug", 1)[1].split("return;", 1)[0]
    assert 'method: "DELETE"' not in arm_block, "the arming branch must not call the API"
    assert 'method: "DELETE"' in fn, "the confirmed branch must call DELETE"
    assert "loadPorts()" in fn, "a freed port must refresh the ports section"


def test_rows_carry_the_log_key_the_panel_is_placed_by():
    """placeLog() finds its row by data-log-key; both row templates must emit one,
    matching the keys openLogPanel is called with (label / app:<slug>)."""
    js = script()
    assert 'data-log-key="${a.label}"' in js
    assert 'data-log-key="app:${a.slug}"' in js
    assert '[data-log-key="${openLog}"]' in js
