"""Network watch: parsers, remote classification, and the observe() transition rules."""

import json

from app.netwatch import (
    _TRANSITION_KINDS,
    EVENT_RING,
    NOTIFY_RING,
    ack,
    ack_command,
    classify_remote,
    conn_key,
    inbound,
    listener_key,
    load_state,
    notify_script,
    observe,
    observe_agents,
    parse_dscacheutil_name,
    parse_lsof_connections,
    record_notification,
    save_state,
)

T0 = "2026-08-04T10:00:00+00:00"
T1 = "2026-08-04T10:00:30+00:00"


def L(command="node", port=3000, localhost=True, system=False, agent=None, project=None,
      args="node /Users/x/app/server.js"):
    """A ports.list_ports entry."""
    return {
        "port": port, "pid": 1, "command": command,
        "addresses": ["127.0.0.1"] if localhost else ["*"],
        "localhost": localhost, "cwd": None, "project": project,
        "args": args, "system": system, "agent": agent,
    }


def C(rhost="10.0.1.37", lport=8081, command="node", remote_class="lan", hostname=None):
    """An inbound() row (already classified; hostname enriched by the watcher)."""
    c = {"pid": 2, "command": command, "lhost": "10.0.1.5", "lport": lport,
         "rhost": rhost, "rport": 52911, "remote_class": remote_class}
    if hostname is not None:
        c["hostname"] = hostname
    return c


def seeded(listeners=(), conns=()):
    state = {}
    assert observe(state, list(listeners), list(conns), T0) == []
    return state


def A(label="com.groceryhelper.recipes", healthy=True, last_exit=0, status="idle",
      schedule="Sun 10:00"):
    """A launchd.list_agents() row (subset the watcher reads)."""
    return {"label": label, "healthy": healthy, "last_exit": last_exit,
            "status": status, "schedule": schedule, "vendor": False}


def agents_seeded(agents=()):
    state = {}
    assert observe_agents(state, list(agents), set(), T0) == []
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
# Listener detail enrichment
# --------------------------------------------------------------------------- #
def test_listener_alert_carries_a_detail_line():
    state = seeded()
    # Exposed + attributed → banners (exposure outranks attribution), so it lands in active.
    observe(state, [L(command="node", port=4444, localhost=False, project="~/x")], [], T1)
    ev = state["events"][0]
    assert "pid 1" in ev["detail"] and "*" in ev["detail"] and "~/x" in ev["detail"]
    assert state["active"]["listen:node:4444"]["detail"] == ev["detail"]


# --------------------------------------------------------------------------- #
# Structured `data` for the click-to-expand card
# --------------------------------------------------------------------------- #
def test_listener_event_data_carries_the_full_command_line():
    state = seeded()
    observe(state, [L(command="mystery", port=4444, args="/bin/mystery --serve :4444")], [], T1)
    d = state["events"][0]["data"]
    assert d["type"] == "listener"
    assert d["args"] == "/bin/mystery --serve :4444"  # the headline field
    assert d["pid"] == 1 and d["port"] == 4444 and d["addresses"] == ["127.0.0.1"]
    # active entry carries the same structured data (the card also opens from active).
    assert state["active"]["listen:mystery:4444"]["data"]["args"] == d["args"]


def test_conn_event_data_carries_the_remote_endpoint():
    state = seeded()
    observe(state, [], [C(rhost="10.0.1.9", lport=8081, remote_class="lan")], T1)
    d = state["events"][0]["data"]
    assert d["type"] == "conn"
    assert d["rhost"] == "10.0.1.9" and d["rport"] == 52911 and d["lport"] == 8081
    assert d["remote_class"] == "lan"


def test_agent_event_data_carries_exit_and_schedule():
    state = agents_seeded([A(healthy=True)])
    observe_agents(state, [A(healthy=False, last_exit=256, status="idle",
                             schedule="Sun 10:00")], set(), T1)
    d = state["events"][0]["data"]
    assert d["type"] == "agent"
    assert d["last_exit"] == 256 and d["status"] == "idle" and d["schedule"] == "Sun 10:00"


def test_hostname_is_folded_into_a_conn_summary_when_present():
    state = seeded()
    observe(state, [], [C(rhost="10.0.1.9", hostname="Alis-iPhone")], T1)
    ev = state["events"][0]
    assert "Alis-iPhone (10.0.1.9)" in ev["summary"]
    assert ev["data"]["hostname"] == "Alis-iPhone"
    # Absent hostname → summary unchanged (old rows / unresolved IPs).
    state2 = seeded()
    observe(state2, [], [C(rhost="10.0.1.9")], T1)
    assert "10.0.1.9 connected" in state2["events"][0]["summary"]
    assert state2["events"][0]["data"]["hostname"] == ""


def test_parse_dscacheutil_name():
    out = "name: speedport.ip\nalias: 1.2.168.192.in-addr.arpa\nip_address: 192.168.2.1\n"
    assert parse_dscacheutil_name(out) == "speedport.ip"
    assert parse_dscacheutil_name("") == ""  # unknown IP prints nothing (verified live)


# --------------------------------------------------------------------------- #
# observe_agents(): launchd agent failure watch
# --------------------------------------------------------------------------- #
def test_agent_seed_is_silent_even_if_already_failed():
    """A pre-existing failure must not banner on first sight — you'd be told about
    history you can't act on, every restart."""
    state = {}
    assert observe_agents(state, [A(healthy=False, last_exit=256)], set(), T0) == []
    assert state["agent_health"]["com.groceryhelper.recipes"] is False


def test_agent_failure_banners_once_with_exit_code():
    state = agents_seeded([A(healthy=True)])
    events = observe_agents(state, [A(healthy=False, last_exit=256, status="idle")], set(), T1)
    assert [e["kind"] for e in events] == ["agent_failed"]
    assert events[0]["severity"] == "bad"
    assert "exit 256" in events[0]["summary"]
    assert "agent:com.groceryhelper.recipes" in state["active"]
    # Stays failed → no re-banner on the next scan.
    assert observe_agents(state, [A(healthy=False, last_exit=256)], set(), T1) == []


def test_expected_stop_does_not_banner_but_still_updates_health():
    """A dashboard-initiated stop is expected; it must NOT read as a crash — but the
    health MUST still flip, or the later recovery event never fires."""
    state = agents_seeded([A(label="com.x", healthy=True)])
    events = observe_agents(state, [A(label="com.x", healthy=False, last_exit=143)],
                            {"com.x"}, T1)
    assert events == []
    assert state["agent_health"]["com.x"] is False  # sabotage target


def test_agent_recovery_is_log_only_and_clears_the_alert():
    state = agents_seeded([A(healthy=True)])
    observe_agents(state, [A(healthy=False, last_exit=1)], set(), T1)
    assert "agent:com.groceryhelper.recipes" in state["active"]
    events = observe_agents(state, [A(healthy=True, last_exit=0)], set(), T1)
    assert [e["kind"] for e in events] == ["agent_recovered"]
    assert events[0]["banner"] is False
    assert "agent:com.groceryhelper.recipes" not in state["active"]


def test_agent_failure_ignores_a_prior_ack():
    """agent_failed is a TRANSITION, not a first-sighting: an ack resolves one episode,
    the next failure is a new fact (a permanent per-agent mute would re-hide the recipes
    job). Contrast a `new_listener` ack, which is 'I know, hush forever'."""
    assert "agent_failed" in _TRANSITION_KINDS and "now_exposed" in _TRANSITION_KINDS
    state = agents_seeded([A(healthy=True)])
    observe_agents(state, [A(healthy=False, last_exit=1)], set(), T1)
    assert ack(state, "agent:com.groceryhelper.recipes")["ok"] is True
    observe_agents(state, [A(healthy=True)], set(), T1)          # recover
    events = observe_agents(state, [A(healthy=False, last_exit=1)], set(), T1)  # fail again
    assert [e["kind"] for e in events] == ["agent_failed"]


def test_removed_agent_is_pruned_and_reseeds():
    state = agents_seeded([A(label="com.gone", healthy=True)])
    observe_agents(state, [], set(), T1)                      # agent disappears
    assert "com.gone" not in state["agent_health"]
    # Re-adding it seeds silently (no false failure just because it came back).
    assert observe_agents(state, [A(label="com.gone", healthy=False, last_exit=9)],
                          set(), T1) == []


# --------------------------------------------------------------------------- #
# Notification history
# --------------------------------------------------------------------------- #
def test_record_notification_is_capped_and_newest_first():
    state = {}
    for i in range(NOTIFY_RING + 20):
        record_notification(state, title="t", body=f"b{i}", ok=True, now=T1)
    assert len(state["notifications"]) == NOTIFY_RING
    assert state["notifications"][0]["body"] == f"b{NOTIFY_RING + 19}"
    assert "notify_failures" not in state


def test_failed_send_is_recorded_and_counted():
    state = {}
    record_notification(state, title="t", body="b", ok=False, now=T1)
    record_notification(state, title="t", body="b2", ok=False, now=T1)
    assert state["notify_failures"] == 2
    assert state["notifications"][0]["ok"] is False


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
