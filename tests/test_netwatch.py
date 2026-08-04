"""Network watch: parsers, remote classification, and the observe() transition rules."""

import json

from app.netwatch import (
    EVENT_RING,
    ack,
    ack_command,
    classify_remote,
    conn_key,
    inbound,
    listener_key,
    load_state,
    notify_script,
    observe,
    parse_lsof_connections,
    save_state,
)

T0 = "2026-08-04T10:00:00+00:00"
T1 = "2026-08-04T10:00:30+00:00"


def L(command="node", port=3000, localhost=True, system=False, agent=None, project=None):
    """A ports.list_ports entry."""
    return {
        "port": port, "pid": 1, "command": command,
        "addresses": ["127.0.0.1"] if localhost else ["*"],
        "localhost": localhost, "cwd": None, "project": project,
        "args": "", "system": system, "agent": agent,
    }


def C(rhost="10.0.1.37", lport=8081, command="node", remote_class="lan"):
    """An inbound() row (already classified)."""
    return {"pid": 2, "command": command, "lhost": "10.0.1.5", "lport": lport,
            "rhost": rhost, "rport": 52911, "remote_class": remote_class}


def seeded(listeners=(), conns=()):
    state = {}
    assert observe(state, list(listeners), list(conns), T0) == []
    return state


# --------------------------------------------------------------------------- #
# Parsers / classifiers
# --------------------------------------------------------------------------- #
def test_parse_connections_ipv4_and_ipv6():
    out = "p70\ncnode\nn10.0.1.5:8081->10.0.1.37:52911\nn[fe80::abc]:8081->[fe80::def]:52911\n"
    rows = parse_lsof_connections(out)
    assert rows == [
        {"pid": 70, "command": "node", "lhost": "10.0.1.5", "lport": 8081,
         "rhost": "10.0.1.37", "rport": 52911},
        {"pid": 70, "command": "node", "lhost": "fe80::abc", "lport": 8081,
         "rhost": "fe80::def", "rport": 52911},
    ]


def test_parse_connections_skips_listen_rows_and_garbage():
    # A LISTEN row has no arrow; a mangled endpoint has no numeric port.
    out = "p70\ncnode\nn*:8081\nnjunk->alsojunk\nn10.0.1.5:8081->10.0.1.9:1000\n"
    rows = parse_lsof_connections(out)
    assert len(rows) == 1
    assert rows[0]["rhost"] == "10.0.1.9"


def test_classify_remote():
    assert classify_remote("127.0.0.1") == "loopback"
    assert classify_remote("::1") == "loopback"
    assert classify_remote("10.0.1.37") == "lan"
    assert classify_remote("192.168.1.5") == "lan"
    assert classify_remote("172.16.0.9") == "lan"
    assert classify_remote("169.254.1.1") == "lan"
    assert classify_remote("fe80::1") == "lan"
    assert classify_remote("fe80::1%en0") == "lan"  # scoped IPv6 (lsof prints the zone)
    assert classify_remote("172.32.0.1") == "public"  # just past 172.16/12
    assert classify_remote("8.8.8.8") == "public"
    assert classify_remote("2606:4700::1") == "public"
    # Fail CLOSED: an address we can't parse should alert, not slide by.
    assert classify_remote("not-an-ip") == "public"


def test_inbound_joins_on_listening_ports_and_drops_loopback():
    conns = [
        {"pid": 1, "command": "node", "lhost": "10.0.1.5", "lport": 8081,
         "rhost": "10.0.1.37", "rport": 50000},           # inbound from the LAN
        {"pid": 1, "command": "node", "lhost": "127.0.0.1", "lport": 8081,
         "rhost": "127.0.0.1", "rport": 50001},           # this Mac talking to itself
        {"pid": 2, "command": "firefox", "lhost": "10.0.1.5", "lport": 61234,
         "rhost": "8.8.8.8", "rport": 443},               # OUTBOUND (ephemeral local port)
    ]
    rows = inbound(conns, {8081})
    assert len(rows) == 1
    assert rows[0]["remote_class"] == "lan"
    assert rows[0]["rhost"] == "10.0.1.37"


# --------------------------------------------------------------------------- #
# observe(): transition rules
# --------------------------------------------------------------------------- #
def test_seed_run_records_everything_silently():
    state = {}
    events = observe(state, [L(localhost=False), L(command="cupsd", port=631, system=True)],
                     [C()], T0)
    assert events == []
    assert state["seeded_at"] == T0
    assert listener_key(L(localhost=False)) in state["known"]
    assert conn_key(C()) in state["known"]
    assert state.get("active", {}) == {}


def test_new_unattributed_loopback_listener_warns():
    state = seeded()
    events = observe(state, [L(command="mystery", port=4444)], [], T1)
    assert [e["kind"] for e in events] == ["new_listener"]
    assert events[0]["severity"] == "warn"
    assert events[0]["banner"] is True
    assert "listen:mystery:4444" in state["active"]


def test_system_listener_is_silent():
    state = seeded()
    events = observe(state, [L(command="rapportd", port=49200, system=True)], [], T1)
    assert events == []
    assert state.get("active", {}) == {}


def test_attributed_listeners_are_log_only():
    state = seeded()
    events = observe(state, [
        L(command="uvicorn", port=8787, agent="com.launchddash.server"),
        L(command="python3", port=9738, project="~/scratch"),
    ], [], T1)
    assert [e["kind"] for e in events] == ["app_listener", "dev_listener"]
    assert all(e["banner"] is False for e in events)
    assert state.get("active", {}) == {}


def test_exposure_outranks_attribution():
    """Your own project binding * is exactly the case to catch (next dev without -H)."""
    state = seeded()
    events = observe(state, [
        L(command="node", port=3000, localhost=False, project="~/some-app"),
        L(command="node", port=3010, localhost=False, agent="com.launchddash.app.x"),
    ], [], T1)
    assert [e["kind"] for e in events] == ["new_exposed", "new_exposed"]
    assert all(e["severity"] == "bad" and e["banner"] is True for e in events)


def test_known_listener_flipping_to_exposed_alerts():
    state = seeded([L(command="node", port=3000, localhost=True)])
    events = observe(state, [L(command="node", port=3000, localhost=False)], [], T1)
    assert [e["kind"] for e in events] == ["now_exposed"]
    assert events[0]["severity"] == "bad"
    # ...and only once: the flip is recorded, the next cycle is quiet.
    assert observe(state, [L(command="node", port=3000, localhost=False)], [], T1) == []


def test_lan_connect_alerts_once_per_device_and_port():
    state = seeded()
    events = observe(state, [], [C(rhost="10.0.1.37", lport=8081)], T1)
    assert [e["kind"] for e in events] == ["lan_connect"]
    assert events[0]["severity"] == "warn"
    # Same device+port again (fresh ephemeral rport upstream): silent.
    assert observe(state, [], [C(rhost="10.0.1.37", lport=8081)], T1) == []
    # A DIFFERENT device on the same port alerts.
    events = observe(state, [], [C(rhost="10.0.1.99", lport=8081)], T1)
    assert [e["kind"] for e in events] == ["lan_connect"]


def test_public_connect_is_bad():
    state = seeded()
    events = observe(state, [], [C(rhost="203.0.113.9", remote_class="public")], T1)
    assert [e["kind"] for e in events] == ["public_connect"]
    assert events[0]["severity"] == "bad"
    assert "PUBLIC" in events[0]["summary"]


def test_ack_clears_and_suppresses():
    state = seeded()
    observe(state, [], [C(rhost="10.0.1.37", lport=8081)], T1)
    key = "conn:10.0.1.37:8081"
    assert ack(state, key)["ok"] is True
    assert key not in state["active"]
    assert key in state["acked"]
    # An acked key never re-alerts, even from a fresh known-set (state pruned).
    state["known"].pop(key)
    assert observe(state, [], [C(rhost="10.0.1.37", lport=8081)], T1) == []


def test_ack_rejects_non_active_keys():
    """Trust boundary: the browser can only ack keys the server minted as active."""
    state = seeded()
    assert ack(state, "listen:evil:1")["ok"] is False
    assert "acked" not in state


def test_acked_key_still_alerts_on_exposure_flip():
    """A per-key ack blessed a LOOPBACK listener; LAN exposure is a new fact."""
    state = seeded()
    observe(state, [L(command="mystery", port=4444)], [], T1)
    assert ack(state, "listen:mystery:4444")["ok"] is True
    events = observe(state, [L(command="mystery", port=4444, localhost=False)], [], T1)
    assert [e["kind"] for e in events] == ["now_exposed"]


def test_ack_command_allows_any_port():
    """Chrome binds * on a fresh random port per session — per-key ack can't quiet it."""
    state = seeded()
    observe(state, [L(command="Chrome", port=50123, localhost=False)], [], T1)
    assert ack_command(state, "Chrome")["ok"] is True
    assert state["active"] == {}
    # New port, same command: silent — including the exposed and flip paths.
    assert observe(state, [L(command="Chrome", port=50999, localhost=False)], [], T1) == []
    assert observe(state, [L(command="Chrome", port=51000)], [], T1) == []
    assert observe(state, [L(command="Chrome", port=51000, localhost=False)], [], T1) == []


def test_ack_command_requires_an_active_match():
    state = seeded()
    assert ack_command(state, "Chrome")["ok"] is False
    assert "acked_commands" not in state


def test_event_ring_is_capped():
    state = seeded()
    listeners = [L(command=f"app{i}", port=10000 + i) for i in range(EVENT_RING + 50)]
    observe(state, listeners, [], T1)
    assert len(state["events"]) == EVENT_RING
    # Newest first: the highest port is at the head.
    assert f":{10000 + EVENT_RING + 49}" in state["events"][0]["summary"]


# --------------------------------------------------------------------------- #
# Notification script escaping
# --------------------------------------------------------------------------- #
def test_notify_script_escapes_injection():
    """A command name is attacker-influenced text; it must never terminate the
    AppleScript string. Backslashes are escaped FIRST (else the quote-escape's
    own backslashes get doubled)."""
    s = notify_script("t", 'a"b\\c')
    assert s == 'display notification "a\\"b\\\\c" with title "t"'
    # A crafted name trying to break out stays inert inside the string.
    evil = notify_script("t", '" & (do shell script "echo pwned") & "')
    assert '\\" & (do shell script \\"echo pwned\\") & \\"' in evil
    # Newlines flatten — banners are one line.
    assert "\n" not in notify_script("t", "a\nb")


# --------------------------------------------------------------------------- #
# State persistence
# --------------------------------------------------------------------------- #
def test_state_round_trip(tmp_path):
    p = tmp_path / "netwatch.json"
    save_state({"seeded_at": T0, "known": {"listen:a:1": {"exposed": False}}}, p)
    assert load_state(p)["seeded_at"] == T0
    assert not (p.parent / "netwatch.json.tmp").exists()  # atomic write cleaned up


def test_corrupt_state_is_parked_not_destroyed(tmp_path):
    p = tmp_path / "netwatch.json"
    p.write_text("{not json")
    assert load_state(p) == {}
    assert (tmp_path / "netwatch.json.bak").read_text() == "{not json"


def test_non_dict_state_is_parked_too(tmp_path):
    p = tmp_path / "netwatch.json"
    p.write_text(json.dumps([1, 2]))
    assert load_state(p) == {}
    assert (tmp_path / "netwatch.json.bak").exists()


def test_missing_state_is_fresh(tmp_path):
    assert load_state(tmp_path / "netwatch.json") == {}
