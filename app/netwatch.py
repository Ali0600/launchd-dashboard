"""Network watch: alert on new listeners, loopback→exposed flips, and inbound connections.

Honest scope (user-level lsof polling — no root, no packet capture): this can see a
process START listening, a listener bound beyond loopback, and ESTABLISHED inbound
connections at poll time. It cannot see port scans, connections shorter than the poll
interval, or outbound traffic. It is a watch, not an intrusion detection system.

Same philosophy as `ports.py`: pure, fixture-testable core; thin live wrappers that
degrade instead of raising. State lives in a machine-local gitignored `netwatch.json`.

Alert rules (see `observe`):
  seed run            -> record everything silently (no day-one banner storm)
  new listener        -> system: silent · agent/project-attributed loopback: log-only
                         · unattributed loopback: WARN banner · non-system exposed: BAD
                         banner (exposure outranks attribution — your own `next dev`
                         without -H is exactly the case to catch)
  loopback -> exposed -> BAD banner, even for a previously acked key (a new fact)
  inbound connection  -> LAN remote: WARN banner once per (device, port) · public
                         remote: BAD banner. Loopback remotes are never tracked.
  acked keys          -> never alert again; `acked_commands` suppresses by command
                         alone (Chrome binds * on a fresh random port per session,
                         so a per-key ack can never quiet it).
"""

from __future__ import annotations

import ipaddress
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

STATE_PATH = Path(__file__).resolve().parent.parent / "netwatch.json"
# Append-only permanent record. The in-state rings are capped, so a busy week
# eventually erases an earlier one; this file is never trimmed (at dozens of records
# a day it takes years to reach a megabyte) and is meant for grep/jq.
ARCHIVE_PATH = Path(__file__).resolve().parent.parent / "netwatch.log.jsonl"

# One lock around every load→mutate→save cycle: the watcher thread and the ack
# route both rewrite the state file, and an unlocked interleave loses the ack.
STATE_LOCK = threading.Lock()

EVENT_RING = 200
NOTIFY_RING = 200
RUN_LEDGER_CAP = 50  # per agent

# Kinds that alert (go to `active` + banner); everything else is log-only.
_ALERT_SEVERITY = {
    "new_listener": "warn",
    "new_exposed": "bad",
    "now_exposed": "bad",
    "lan_connect": "warn",
    "public_connect": "bad",
    "agent_failed": "bad",
}

# Kinds that are a state TRANSITION, not a first-sighting. These alert even for a key
# that was previously acked — an ack resolves the current episode, the next flip is a
# new fact. (A per-key mute here would have re-hidden the recipes job that died silently
# for 11 days.) Contrast the "new_*" kinds, where an ack is a permanent "I know, hush".
_TRANSITION_KINDS = {"now_exposed", "agent_failed"}


# --------------------------------------------------------------------------- #
# Pure parsers / classifiers (fixture-tested)
# --------------------------------------------------------------------------- #
def _split_endpoint(s: str) -> "tuple[Optional[str], Optional[int]]":
    host, sep, port = s.rpartition(":")
    if not sep or not port.isdigit():
        return None, None
    return host.strip("[]"), int(port)


def parse_lsof_connections(output: str) -> list[dict]:
    """Parse `lsof -nP -iTCP -sTCP:ESTABLISHED -Fpcn` field output.

    Each `n` record is `local->remote` (`10.0.1.5:8081->10.0.1.37:52911`,
    IPv6 in brackets). Records without an arrow (a stray LISTEN row) are skipped.
    """
    rows: list[dict] = []
    pid: Optional[int] = None
    command = ""
    for line in output.splitlines():
        if not line:
            continue
        tag, rest = line[0], line[1:]
        if tag == "p":
            pid = int(rest) if rest.isdigit() else None
            command = ""
        elif tag == "c":
            command = rest
        elif tag == "n" and pid is not None:
            local, arrow, remote = rest.partition("->")
            if not arrow:
                continue
            lhost, lport = _split_endpoint(local)
            rhost, rport = _split_endpoint(remote)
            if lhost is None or rhost is None:
                continue
            rows.append({
                "pid": pid,
                "command": command,
                "lhost": lhost,
                "lport": lport,
                "rhost": rhost,
                "rport": rport,
            })
    return rows


def classify_remote(host: str) -> str:
    """loopback | lan | public. Unparseable fails CLOSED to public — an address
    we can't even parse should alert, not slide by."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "public"
    if ip.is_loopback:
        return "loopback"
    if ip.is_private or ip.is_link_local:
        return "lan"
    return "public"


def inbound(conns: list[dict], listening_ports: "set[int]") -> list[dict]:
    """Connections INTO one of our listeners from beyond loopback.

    Inbound = the local port is one we're listening on (an outbound connection's
    local port is ephemeral). Loopback remotes are this Mac talking to itself.
    """
    out: list[dict] = []
    for c in conns:
        if c["lport"] not in listening_ports:
            continue
        cls = classify_remote(c["rhost"])
        if cls == "loopback":
            continue
        out.append({**c, "remote_class": cls})
    return out


def listener_key(entry: dict) -> str:
    return f"listen:{entry['command']}:{entry['port']}"


def conn_key(conn: dict) -> str:
    # rport is deliberately excluded: it's ephemeral, and (device, port) is what
    # makes "alert once per device+port, ack forever" possible.
    return f"conn:{conn['rhost']}:{conn['lport']}"


# --------------------------------------------------------------------------- #
# Observation (pure; mutates the passed state dict, returns this cycle's events)
# --------------------------------------------------------------------------- #
def _emit(state: dict, events: list, *, kind: str, key: str, summary: str,
          command: str, now: str, detail: str = "", data: Optional[dict] = None) -> None:
    severity = _ALERT_SEVERITY.get(kind, "info")
    banner = kind in _ALERT_SEVERITY
    # `data` is the structured detail the click-to-expand card reads (full command line,
    # addresses, remote endpoint, exit code …). `detail` stays as the compact row line.
    ev = {"ts": now, "kind": kind, "key": key, "summary": summary,
          "severity": severity, "banner": banner, "detail": detail, "data": data or {}}
    events.append(ev)
    state["events"] = ([ev] + state.get("events", []))[:EVENT_RING]
    if banner:
        state.setdefault("active", {})[key] = {
            "ts": now, "kind": kind, "summary": summary,
            "severity": severity, "command": command, "detail": detail, "data": data or {},
        }


def _listener_kind(entry: dict, exposed: bool) -> Optional[str]:
    if entry.get("system"):
        return None  # macOS plumbing churns (AirPlay etc.) — recorded, never alerted
    if exposed:
        return "new_exposed"
    if entry.get("agent"):
        return "app_listener"
    if entry.get("project"):
        return "dev_listener"
    return "new_listener"


def _touch(known: dict, key: str, now: str, **fields) -> dict:
    """Record a SIGHTING of a known key and return its entry.

    The watcher re-observes every listener and device each cycle and used to throw
    that away, keeping only "have I seen this key". Now each entry carries
    `first_seen` / `last_seen` / `live` / `sessions`, which is what lets a card say
    "last seen 2h ago" or "connected 14×".

    `sessions` counts EPISODES, not ticks: it increments only on a dead→live
    transition, so a dev server left running all day is one session, not 2 880.

    Legacy entries (pre-stats: `{}` or `{"exposed": bool}`) are backfilled in place
    on first touch — `first_seen` then honestly means "tracked since". Any state
    file written before this feature must keep working without a migration step.
    """
    entry = known.get(key)
    if entry is None:
        entry = known[key] = {"first_seen": now, "last_seen": now, "live": True, "sessions": 1}
    elif "first_seen" not in entry:
        entry.update({"first_seen": now, "last_seen": now, "live": True, "sessions": 1})
    else:
        if not entry.get("live"):
            entry["sessions"] = int(entry.get("sessions") or 0) + 1
        entry["live"] = True
        entry["last_seen"] = now
    for name, value in fields.items():
        # Never overwrite a resolved value with an empty one (a hostname lookup that
        # worked once shouldn't be erased by a later failure).
        if value:
            entry[name] = value
    return entry


def observe(state: dict, listeners: list[dict], inbound_conns: list[dict],
            now: str) -> list[dict]:
    """Diff one scan against the known set. Returns the cycle's new events."""
    events: list[dict] = []
    known = state.setdefault("known", {})
    acked = set(state.get("acked", []))
    acked_commands = set(state.get("acked_commands", []))
    seeding = "seeded_at" not in state
    seen: set = set()

    for e in listeners:
        key = listener_key(e)
        exposed = not e.get("localhost", False)
        seen.add(key)
        is_new = key not in known
        prev = _touch(known, key, now, command=e.get("command"), port=e.get("port"))
        if is_new:
            prev["exposed"] = exposed
            if seeding or key in acked or e["command"] in acked_commands:
                continue
            kind = _listener_kind(e, exposed)
            if kind is None:
                continue
            where = f" ({e['project']})" if e.get("project") else ""
            reach = "exposed to the LAN" if exposed else "on loopback"
            _emit(state, events, kind=kind, key=key, command=e["command"], now=now,
                  summary=f"{e['command']} started listening on :{e['port']} {reach}{where}",
                  detail=_listener_detail(e), data=_listener_data(e))
        elif exposed and not prev.get("exposed"):
            prev["exposed"] = True
            # A previously ACKED key still alerts here: the ack blessed a loopback
            # listener, and LAN exposure is a new fact. Only a command-level allow
            # (or system-ness) silences it.
            if seeding or e.get("system") or e["command"] in acked_commands:
                continue
            _emit(state, events, kind="now_exposed", key=key, command=e["command"], now=now,
                  summary=f"{e['command']} on :{e['port']} flipped from loopback to LAN-exposed",
                  detail=_listener_detail(e), data=_listener_data(e))

    for c in inbound_conns:
        key = conn_key(c)
        seen.add(key)
        is_new = key not in known
        # rhost/lport/command are stored rather than re-parsed from the key: an IPv6
        # remote is full of colons, so splitting `conn:<host>:<port>` is a trap.
        _touch(known, key, now, hostname=c.get("hostname"), rhost=c.get("rhost"),
               lport=c.get("lport"), command=c.get("command"))
        if not is_new:
            continue
        if seeding or key in acked or c["command"] in acked_commands:
            continue
        kind = "lan_connect" if c["remote_class"] == "lan" else "public_connect"
        who = "device" if c["remote_class"] == "lan" else "PUBLIC address"
        # A resolved device name (dscacheutil, best-effort) is folded in when present.
        named = f"{c['hostname']} ({c['rhost']})" if c.get("hostname") else c["rhost"]
        _emit(state, events, kind=kind, key=key, command=c["command"], now=now,
              summary=f"{who} {named} connected to :{c['lport']} ({c['command']})",
              detail=f"{c['rhost']}:{c['rport']} → :{c['lport']} · pid {c['pid']}",
              data=_conn_data(c))

    # Anything not in this scan is no longer live. The entry is NEVER dropped — it's
    # the roster ("this device has connected 14× since Aug 4") and the reason a card
    # can say "last seen listening 2h ago" instead of just "not listening anymore".
    # `live` is set unconditionally (not only when it was previously true) so that after
    # ONE cycle every entry carries the field — including a legacy entry that hasn't been
    # seen since the upgrade, which `_touch` never gets to backfill. A consumer reading
    # entry["live"] must not have to guess. `first_seen`/`sessions` stay absent for those
    # until they're actually seen: we have no data, and inventing it would be a lie.
    for key, entry in known.items():
        if key not in seen:
            entry["live"] = False

    if seeding:
        state["seeded_at"] = now
    return events


def _listener_detail(e: dict) -> str:
    bits = [f"pid {e['pid']}", ", ".join(e.get("addresses") or [])]
    where = e.get("project") or e.get("cwd")
    if where:
        bits.append(where)
    if e.get("agent"):
        bits.append(e["agent"])
    return " · ".join(b for b in bits if b)


def _listener_data(e: dict) -> dict:
    """Structured fields for a listener event's expand card. `args` (the full command
    line) is the headline — it's what actually identifies a 'mystery listener'."""
    return {
        "type": "listener",
        "command": e.get("command"), "port": e.get("port"), "pid": e.get("pid"),
        "addresses": e.get("addresses") or [], "project": e.get("project"),
        "cwd": e.get("cwd"), "agent": e.get("agent"), "args": e.get("args") or "",
    }


def _conn_data(c: dict) -> dict:
    return {
        "type": "conn",
        "command": c.get("command"), "pid": c.get("pid"), "lport": c.get("lport"),
        "rhost": c.get("rhost"), "rport": c.get("rport"),
        "remote_class": c.get("remote_class"), "hostname": c.get("hostname") or "",
    }


def _agent_data(a: dict) -> dict:
    return {
        "type": "agent",
        "label": a.get("label"), "last_exit": a.get("last_exit"),
        "status": a.get("status"), "schedule": a.get("schedule"),
        "program": a.get("program"),
    }


def _record_run(state: dict, a: dict, now: str) -> None:
    """Keep a per-agent ledger of the runs we detect.

    Every cycle already observes each agent's `last_run` (its log's mtime) and
    `last_exit` and used to discard both — so "has the recipes job actually run every
    Sunday?" was unanswerable, which is the exact question its 11-day silent death begs.

    A run is recorded when `last_run` moves AND no pid is live: while a job is still
    running, `last_exit` is the PREVIOUS run's, so sampling mid-flight would pair a new
    timestamp with a stale exit code. Deferring to the next cycle costs 30s and is
    correct. Always-on services (KeepAlive, a pid forever) therefore keep only their
    seed record — right, since the ledger exists for scheduled and one-shot jobs.
    """
    last_run = a.get("last_run")
    if not last_run:
        return
    runs = state.setdefault("agent_runs", {})
    label = a["label"]
    log = runs.get(label)
    record = {"ts": last_run, "exit": a.get("last_exit"), "detected": now}
    if log is None:
        runs[label] = [record]  # first sighting: seed from real past data, no event
        return
    if (log and log[0].get("ts") == last_run) or a.get("pid") is not None:
        return
    runs[label] = ([record] + log)[:RUN_LEDGER_CAP]


def observe_agents(state: dict, agents: list[dict], expected: "set[str]",
                   now: str) -> list[dict]:
    """Diff launchd agent health against the last scan. A user agent going
    unhealthy (a nonzero last exit with no live pid) banners once — this is the
    class of failure that hid `com.groceryhelper.recipes` dying for 11 days.

    `expected` names labels the dashboard just stopped/restarted itself, so a
    deliberate stop doesn't read as a crash. Recovery clears the episode.
    """
    events: list[dict] = []
    health = state.setdefault("agent_health", {})
    seeding_agents = "agents_seeded_at" not in state

    present = set()
    for a in agents:
        label = a["label"]
        present.add(label)
        _record_run(state, a, now)
        healthy = bool(a.get("healthy"))
        prev = health.get(label)
        health[label] = healthy
        if prev is None or healthy == prev:
            continue  # first sighting (seed) or no change
        if not healthy:  # True -> False
            if seeding_agents or label in expected:
                continue
            exit_note = f" (exit {a['last_exit']})" if a.get("last_exit") not in (None,) else ""
            _emit(state, events, kind="agent_failed", key=f"agent:{label}",
                  command=label, now=now,
                  summary=f"agent {label} failed{exit_note}",
                  detail=f"status {a.get('status', '?')} · {a.get('schedule', '')}".strip(" ·"),
                  data=_agent_data(a))
        else:  # False -> True: recovered — resolve the episode
            (state.get("active") or {}).pop(f"agent:{label}", None)
            _emit(state, events, kind="agent_recovered", key=f"agent:{label}",
                  command=label, now=now, summary=f"agent {label} recovered",
                  data=_agent_data(a))

    # Drop labels that no longer exist (removed app / deleted plist) so re-adding re-seeds.
    runs = state.setdefault("agent_runs", {})
    for gone in [k for k in health if k not in present]:
        health.pop(gone)
        runs.pop(gone, None)
        (state.get("active") or {}).pop(f"agent:{gone}", None)

    if seeding_agents:
        state["agents_seeded_at"] = now
    return events


def record_notification(state: dict, *, title: str, body: str, ok: bool,
                        now: str) -> None:
    """Log a banner the watcher tried to post — newest first, capped. A failed send
    also bumps a persisted counter: a dead notification channel must announce itself,
    the same way `watch_errors` does for the loop."""
    entry = {"ts": now, "title": title, "body": body, "ok": ok}
    state["notifications"] = ([entry] + state.get("notifications", []))[:NOTIFY_RING]
    if not ok:
        state["notify_failures"] = state.get("notify_failures", 0) + 1


def ack(state: dict, key: str) -> dict:
    """Acknowledge one active alert — the key never alerts again (except a later
    loopback→exposed flip, which is a new fact). Only server-minted active keys
    are accepted: the browser can't seed the acked list with arbitrary strings."""
    active = state.get("active") or {}
    if key not in active:
        return {"ok": False, "detail": f"{key!r} is not an active alert"}
    active.pop(key)
    if key not in state.setdefault("acked", []):
        state["acked"].append(key)
    return {"ok": True, "detail": f"acknowledged {key}"}


def ack_command(state: dict, command: str) -> dict:
    """Allow a command on ANY port/host, forever — for apps that bind a fresh
    random port per session. Must name a command with an active alert."""
    active = state.get("active") or {}
    matches = [k for k, v in active.items() if v.get("command") == command]
    if not matches:
        return {"ok": False, "detail": f"no active alert from {command!r}"}
    for k in matches:
        active.pop(k)
    if command not in state.setdefault("acked_commands", []):
        state["acked_commands"].append(command)
    return {"ok": True, "detail": f"always allowing {command} ({len(matches)} cleared)"}


# --------------------------------------------------------------------------- #
# Notifications (pure script builder + best-effort poster)
# --------------------------------------------------------------------------- #
def _osa_str(s: str) -> str:
    """AppleScript string literal escaping: backslashes FIRST, then quotes.
    A listener's command name is attacker-influenced text and must never break
    out of the string. Newlines become spaces (banners are one line anyway)."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ")


def notify_script(title: str, body: str) -> str:
    return f'display notification "{_osa_str(body)}" with title "{_osa_str(title)}"'


def post_notification(title: str, body: str) -> bool:
    """Best-effort macOS banner (proven to fire from a launchd agent, 2026-08-04).
    Never raises — a broken notifier must not kill the watch cycle."""
    try:
        res = subprocess.run(
            ["/usr/bin/osascript", "-e", notify_script(title, body)],
            capture_output=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return res.returncode == 0


# --------------------------------------------------------------------------- #
# Live wrappers (degrade, never raise)
# --------------------------------------------------------------------------- #
def established() -> list[dict]:
    try:
        res = subprocess.run(
            ["lsof", "-nP", "-iTCP", "-sTCP:ESTABLISHED", "-Fpcn"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return parse_lsof_connections(res.stdout)


def parse_dscacheutil_name(output: str) -> str:
    """Pull the hostname from `dscacheutil -q host -a ip_address <ip>` output.
    A resolvable IP prints a `name: speedport.ip` line; an unknown IP prints
    nothing (verified live 2026-08-07). Returns "" when there's no name."""
    for line in output.splitlines():
        if line.startswith("name:"):
            return line[len("name:"):].strip()
    return ""


_HOSTNAME_CACHE: dict = {}


def resolve_hostname(ip: str) -> str:
    """Best-effort reverse name for a LAN/public IP (device identification in a
    connection alert). Cached in-memory; never raises; "" when unresolved."""
    if ip in _HOSTNAME_CACHE:
        return _HOSTNAME_CACHE[ip]
    try:
        res = subprocess.run(
            ["dscacheutil", "-q", "host", "-a", "ip_address", ip],
            capture_output=True, text=True, timeout=2,
        )
        name = parse_dscacheutil_name(res.stdout)
    except (OSError, subprocess.TimeoutExpired):
        name = ""
    _HOSTNAME_CACHE[ip] = name
    return name


def load_state(path: Path = STATE_PATH) -> dict:
    try:
        raw = path.read_text()
    except OSError:
        return {}
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("netwatch state must be a JSON object")
    except ValueError:
        # Park the evidence BEFORE starting fresh — an unreadable state file that a
        # later save overwrites is undiagnosable data loss.
        try:
            (path.parent / (path.name + ".bak")).write_text(raw)
        except OSError:
            pass
        return {}
    return data


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    """Atomic write (tmp + rename): the watcher saves every cycle, and a crash
    mid-write must not corrupt the known-set."""
    tmp = path.parent / (path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(state, indent=1))
        os.replace(tmp, path)
    except OSError:
        pass


def archive_records(events: list[dict], posted: list, path: Path = ARCHIVE_PATH) -> None:
    """Append this cycle's events and banners to the permanent JSONL log.

    One JSON object per line, `t` naming the record type. Best-effort by design: a
    failed append must never break a watch cycle, and there is exactly one writer
    (the watcher thread), so a plain append needs no locking.
    """
    lines = [json.dumps({"t": "event", **ev}) for ev in events]
    lines += [json.dumps({"t": "banner", "ts": ts, "title": title, "body": body, "ok": ok})
              for ts, title, body, ok in posted]
    if not lines:
        return
    try:
        with open(path, "a") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError as exc:
        print(f"launchddash: could not append to the netwatch archive: {exc!r}", file=sys.stderr)


def archive_size(path: Path = ARCHIVE_PATH) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
