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


def test_live_port_rows_open_by_name_not_by_ipv4_literal():
    """`localhost` resolves to ::1 OR 127.0.0.1, so it reaches the server whichever family
    it bound — a Vite/Next default bind is IPv6-only, where http://127.0.0.1:PORT is
    refused. It's also the host dev servers' Host-header allowlists expect."""
    js = script()
    ports_render = js.split('$("portlist").innerHTML', 1)[1].split("function ", 1)[0]
    assert "window.open('http://localhost:${p.port}'" in ports_render
    assert "127.0.0.1:${p.port}" not in ports_render
    # System listeners (AirPlay etc.) aren't web pages — no button for them.
    assert "p.system ? \"\"" in ports_render


def test_app_rows_open_by_name_too():
    js = script()
    assert "window.open('http://localhost:${a.open_port}'" in js
    assert "window.open('http://127.0.0.1:" not in js


def test_claimed_rows_have_no_open_button():
    """Nothing is serving a claimed port — Open would just fail; it gets a start button."""
    js = script()
    claimed = js.split('if (p.kind === "claimed")', 1)[1].split("const where =", 1)[0]
    assert "window.open" not in claimed


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


def test_watch_section_is_polled_with_everything_else():
    js = script()
    assert 'id="watchlist"' in PAGE
    loadall = js.split("function loadAll", 1)[1].split("\n", 1)[0]
    assert "loadWatch()" in loadall


def test_watch_title_badge_sets_and_resets():
    js = script()
    assert "`(${n}!) launchd dashboard`" in js
    assert "document.title = n ?" in js  # ternary: resets when nothing is active


def test_watch_ack_rides_data_attributes_not_inline_js():
    """Alert keys and summaries embed process COMMAND NAMES — arbitrary text. An
    inline onclick with an interpolated key lets a quote in a process name break
    into JS; data-attributes + one delegated listener keep it inert, and every
    HTML interpolation in the renderer must pass through esc()."""
    js = script()
    watch = js.split("async function loadWatch", 1)[1].split('$("watchlist").onclick', 1)[0]
    assert 'data-ack="${esc(a.key)}"' in watch
    assert 'data-ackcmd="${esc(a.command)}"' in watch
    assert "esc(a.summary)" in watch
    assert "onclick" not in watch, "no inline handlers in the watch renderer"
    assert "button[data-ack],button[data-ackcmd]" in js  # the delegated listener


def test_watch_allow_app_only_for_listener_alerts():
    """'Always allow <command>' makes no sense on a connection alert (the command
    there is our own server); it's offered only for listen: keys."""
    js = script()
    watch = js.split("async function loadWatch", 1)[1]
    assert 'a.key.startsWith("listen:")' in watch


def test_history_is_a_static_sheet_not_a_rerendered_child():
    """The history moved from an in-section toggle to a header-opened sheet. The sheet
    must be STATIC page HTML (present outside <script>), so a list re-render can never
    destroy it — the destroyed-log-panel lesson. The old toggle is gone (one surface)."""
    body = PAGE.split("<script>", 1)[0]
    assert 'id="histBtn"' in body and 'id="histsheet"' in body and 'id="sheetback"' in body
    assert 'onclick="openHistory()"' in body  # header button opens it
    # the retired toggle + its inline panel must be fully removed (no second surface)
    assert "showHistory" not in PAGE and "historywrap" not in PAGE and "loadNotifications" not in PAGE


def test_history_sheet_fetches_on_demand_only():
    """The full ring is larger than the live summary, so /api/watch/history is fetched
    only by the sheet loader — and re-fetched by the 30s poll ONLY while the sheet is
    open, so a closed sheet costs zero extra requests."""
    js = script()
    assert js.count('fetch("/api/watch/history")') == 1, "history fetched from exactly one place (the sheet loader)"
    hist = js.split("async function loadHistory", 1)[1]
    assert 'fetch("/api/watch/history")' in hist
    loadall = js.split("function loadAll", 1)[1].split("\n", 1)[0]
    assert "if (historyOpen) loadHistory()" in loadall  # poll re-fetch gated on open
    # every interpolation in the sheet embeds process-named text → esc()
    assert "esc(e.summary)" in hist and "esc(e.detail)" in hist
    assert "esc(nt.body)" in hist and "esc(nt.title)" in hist


def test_history_sheet_closes_by_backdrop_and_escape():
    js = script()
    body = PAGE.split("<script>", 1)[0]
    assert 'id="sheetback" onclick="closeHistory()"' in body  # backdrop click closes
    assert 'e.key === "Escape" && historyOpen' in js           # Esc closes


def test_watch_renders_detail_and_failed_send_count():
    js = script()
    watch = js.split("async function loadWatch", 1)[1].split("// ---- Network History", 1)[0]
    assert "esc(a.detail)" in watch and "esc(e.detail)" in watch
    assert "failed sends" in watch  # notify_failures surfaced in the meta line


def test_rows_carry_the_log_key_the_panel_is_placed_by():
    """placeLog() finds its row by data-log-key; both row templates must emit one,
    matching the keys openLogPanel is called with (label / app:<slug>)."""
    js = script()
    assert 'data-log-key="${a.label}"' in js
    assert 'data-log-key="app:${a.slug}"' in js
    assert '[data-log-key="${openLog}"]' in js
