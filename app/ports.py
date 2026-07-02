"""Deterministic core: which TCP ports are listening, owned by what, for which project.

Same philosophy as `launchd.py`: shell out to `lsof` / `ps` (no sudo — user
processes only, which is exactly the dev-server population), keep every parser
pure so it's fixture-testable, and let `app/main.py` stay a thin shell.

Attribution pipeline per listener:
  lsof (pid, command, addr:port)  ->  cwd (lsof -d cwd)  ->  args + ppid (ps)
  -> project dir (cwd under $HOME, else a $HOME path mined from the args)
  -> owning launchd agent (walk the ppid chain into the agents' pids)
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
from pathlib import Path
from typing import Optional

# Executables under these prefixes are macOS plumbing (AirPlay, rapportd, ...),
# not something you chose to run — hidden by default like vendor agents.
SYSTEM_PREFIXES = (
    "/System/",
    "/usr/libexec/",
    "/usr/sbin/",
    "/sbin/",
    "/Library/Apple/",
)

# A path token that enters one of these is inside a project, not the project itself.
_VENDOR_DIR_RE = re.compile(r"/(node_modules|\.venv|venv|\.git)(/|$)")

_LOCALHOST = {"127.0.0.1", "::1", "localhost"}


# --------------------------------------------------------------------------- #
# Pure parsers (fixture-tested)
# --------------------------------------------------------------------------- #
def parse_lsof_listeners(output: str) -> list[dict]:
    """Parse `lsof -nP -iTCP -sTCP:LISTEN -Fpcn` field output.

    Records: `p<pid>` starts a process, `c<command>` names it, each `n<addr>`
    is one listening socket (`127.0.0.1:8787`, `[::1]:5173`, `*:7000`).
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
            host, sep, port = rest.rpartition(":")
            if not sep or not port.isdigit():
                continue
            rows.append({
                "pid": pid,
                "command": command,
                "host": host.strip("[]") or "*",
                "port": int(port),
            })
    return rows


def merge_listeners(rows: list[dict]) -> list[dict]:
    """Collapse one process listening on IPv4+IPv6 (or several fds) of the same
    port into a single entry with all addresses."""
    merged: dict[tuple[int, int], dict] = {}
    for r in rows:
        key = (r["pid"], r["port"])
        entry = merged.setdefault(
            key, {"pid": r["pid"], "command": r["command"], "port": r["port"], "addresses": []}
        )
        if r["host"] not in entry["addresses"]:
            entry["addresses"].append(r["host"])
    return sorted(merged.values(), key=lambda e: (e["port"], e["pid"]))


def parse_lsof_cwds(output: str) -> dict[int, str]:
    """Parse `lsof -a -p <pids> -d cwd -Fpn` into {pid: cwd}."""
    cwds: dict[int, str] = {}
    pid: Optional[int] = None
    for line in output.splitlines():
        if not line:
            continue
        tag, rest = line[0], line[1:]
        if tag == "p":
            pid = int(rest) if rest.isdigit() else None
        elif tag == "n" and pid is not None:
            cwds[pid] = rest
    return cwds


def parse_ps_table(output: str) -> dict[int, dict]:
    """Parse `ps -axo pid=,ppid=,comm=` into {pid: {ppid, comm}}.

    comm can contain spaces ("Code Helper (Plugin)"), so split at most twice.
    """
    table: dict[int, dict] = {}
    for line in output.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        table[int(parts[0])] = {"ppid": int(parts[1]), "comm": parts[2]}
    return table


def parse_ps_args(output: str) -> dict[int, str]:
    """Parse `ps -o pid=,args= -p <pids>` into {pid: full command line}."""
    args: dict[int, str] = {}
    for line in output.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            args[int(parts[0])] = parts[1]
    return args


def is_localhost(addresses: list[str]) -> bool:
    """True only when every bound address is loopback — else the port is
    reachable from the LAN (worth flagging)."""
    return bool(addresses) and all(a in _LOCALHOST for a in addresses)


def is_system(comm: str) -> bool:
    return comm.startswith(SYSTEM_PREFIXES)


def tilde(path: str, home: str) -> str:
    if path == home or path.startswith(home + "/"):
        return "~" + path[len(home):]
    return path


def project_of(cwd: Optional[str], args: Optional[str], home: str) -> Optional[str]:
    """Best-effort project directory for a listener, tilde-shortened.

    1. Its cwd, when that's a real directory *inside* $HOME (not $HOME itself).
    2. Else mine the command line for a $HOME path — trimmed back out of
       node_modules/.venv/etc., and to its parent when it names a file
       (`node .../3D-globe/node_modules/.bin/vite` -> ~/Documents/3D-globe).

    ~/Library is app-support plumbing, never a project — excluded from both.
    """
    library = home + "/Library/"
    if cwd and cwd.startswith(home + "/") and not cwd.startswith(library):
        return tilde(cwd, home)
    for tok in (args or "").split():
        if not tok.startswith(home + "/") or tok.startswith(library):
            continue
        m = _VENDOR_DIR_RE.search(tok)
        if m:
            tok = tok[: m.start()]
        elif "." in tok.rsplit("/", 1)[-1]:  # last component looks like a file
            tok = tok.rsplit("/", 1)[0]
        if tok and tok != home:
            return tilde(tok, home)
    return None


def agent_for(pid: int, ppid_map: dict[int, int], agent_pids: dict[int, str]) -> Optional[str]:
    """Label of the launchd agent this pid runs under, if any.

    The listener is often a *child* of the agent's pid (run.sh -> uvicorn), so
    walk the ppid chain instead of only checking the pid itself.
    """
    seen: set[int] = set()
    cur: Optional[int] = pid
    while cur is not None and cur > 1 and cur not in seen:
        if cur in agent_pids:
            return agent_pids[cur]
        seen.add(cur)
        cur = ppid_map.get(cur)
    return None


# --------------------------------------------------------------------------- #
# Live wrappers (subprocess; degrade to partial data, never raise)
# --------------------------------------------------------------------------- #
def _out(cmd: list[str]) -> str:
    # lsof exits non-zero when nothing matches — that's an empty result, not an error.
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return res.stdout


def list_ports(agent_pids: Optional[dict[int, str]] = None) -> list[dict]:
    home = str(Path.home())
    listeners = merge_listeners(
        parse_lsof_listeners(_out(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpcn"]))
    )
    if not listeners:
        return []
    pids = sorted({e["pid"] for e in listeners})
    pid_list = ",".join(str(p) for p in pids)
    cwds = parse_lsof_cwds(_out(["lsof", "-a", "-p", pid_list, "-d", "cwd", "-Fpn"]))
    ps_table = parse_ps_table(_out(["ps", "-axo", "pid=,ppid=,comm="]))
    args = parse_ps_args(_out(["ps", "-o", "pid=,args=", "-p", pid_list]))
    ppid_map = {p: v["ppid"] for p, v in ps_table.items()}

    entries: list[dict] = []
    for e in listeners:
        pid = e["pid"]
        comm = ps_table.get(pid, {}).get("comm", "")
        cwd = cwds.get(pid)
        entries.append({
            "port": e["port"],
            "pid": pid,
            "command": e["command"],
            "addresses": e["addresses"],
            "localhost": is_localhost(e["addresses"]),
            "cwd": tilde(cwd, home) if cwd else None,
            "project": project_of(cwd, args.get(pid), home),
            "args": args.get(pid, ""),
            "system": is_system(comm),
            "agent": agent_for(pid, ppid_map, agent_pids or {}),
        })
    return entries


def kill_listener(pid: int) -> dict:
    """SIGTERM a process — but only one that is currently holding a listening
    port (re-checked live), so the endpoint can't kill arbitrary pids."""
    listening = {e["pid"] for e in parse_lsof_listeners(
        _out(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpcn"])
    )}
    if pid not in listening:
        return {"ok": False, "detail": f"pid {pid} is not holding a listening port"}
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return {"ok": False, "detail": f"pid {pid} already exited"}
    except PermissionError:
        return {"ok": False, "detail": f"pid {pid} is not yours to stop (no sudo here)"}
    return {"ok": True, "detail": f"sent SIGTERM to pid {pid}"}
