"""Deterministic core: discover, inspect, and control macOS launchd agents.

Everything here shells out to `launchctl` / reads plists — no guessing, no LLM.
The web layer (`app/main.py`) is a thin shell over these functions, and the pure
parsers (`humanize_schedule`, `next_run`, `parse_launchctl_list`) are unit-tested
against fixtures so they don't need a live machine.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

UID = os.getuid()

# User LaunchAgents only (no sudo). LaunchDaemons (root) are a deliberate later tier.
AGENT_DIRS = [
    Path.home() / "Library" / "LaunchAgents",
    Path("/Library/LaunchAgents"),
]

# Labels we didn't author — hidden by default so the dashboard shows *your* jobs.
VENDOR_PREFIXES = (
    "com.apple.",
    "com.google.",
    "com.microsoft.",
    "com.adobe.",
    "com.amazon.",
    "com.docker.",
    "org.mozilla.",
    "homebrew.",
)

WEEKDAYS = {0: "Sun", 1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}


@dataclass
class Agent:
    label: str
    path: str
    schedule: str
    status: str  # running | idle | unloaded | disabled
    healthy: bool
    last_exit: Optional[int]
    pid: Optional[int]
    program: str
    next_run: Optional[str]  # ISO8601, local
    last_run: Optional[str]  # ISO8601, local (proxy: stdout log mtime)
    log_path: Optional[str]
    vendor: bool = False
    raw_schedule: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return self.__dict__


# --------------------------------------------------------------------------- #
# Discovery + plist reading
# --------------------------------------------------------------------------- #
def discover_plists() -> list[Path]:
    found: list[Path] = []
    for d in AGENT_DIRS:
        if d.is_dir():
            found.extend(sorted(d.glob("*.plist")))
    return found


def load_plist(path: Path) -> Optional[dict]:
    try:
        with path.open("rb") as fh:
            return plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException):
        return None


def is_vendor(label: str) -> bool:
    return label.startswith(VENDOR_PREFIXES)


# --------------------------------------------------------------------------- #
# launchctl state (pure parser + the subprocess wrapper that feeds it)
# --------------------------------------------------------------------------- #
def parse_launchctl_list(output: str) -> dict:
    """Parse `launchctl list <label>` output into the few fields we need."""
    out: dict = {}
    pid = re.search(r'"PID"\s*=\s*(\d+);', output)
    exit_ = re.search(r'"LastExitStatus"\s*=\s*(-?\d+);', output)
    if pid:
        out["pid"] = int(pid.group(1))
    if exit_:
        out["last_exit"] = int(exit_.group(1))
    return out


def _launchctl_state_live(label: str) -> dict:
    try:
        res = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"loaded": False}
    if res.returncode != 0:
        return {"loaded": False}
    state = parse_launchctl_list(res.stdout)
    state["loaded"] = True
    return state


# The UI polls agents + apps + ports every 30s and each endpoint used to shell out
# one `launchctl list` per label — dozens of subprocesses per refresh. A short TTL
# memo makes one sweep serve all three; control actions invalidate so a click's
# follow-up refresh never shows pre-action state.
_STATE_TTL_S = 5.0
_state_cache: dict = {}


def invalidate_state(label: Optional[str] = None) -> None:
    """Drop cached state after a mutation (run/stop/enable/bootstrap/bootout)."""
    if label is None:
        _state_cache.clear()
    else:
        _state_cache.pop(label, None)


def launchctl_state(label: str, now: Optional[float] = None) -> dict:
    """Live state for one label, memoized for a few seconds. {} (with loaded=False)
    when not bootstrapped. `now` is injectable for tests."""
    t = time.monotonic() if now is None else now
    hit = _state_cache.get(label)
    if hit and t - hit[0] < _STATE_TTL_S:
        return hit[1]
    state = _launchctl_state_live(label)
    _state_cache[label] = (t, state)
    return state


# --------------------------------------------------------------------------- #
# Schedule humanizing + next-run (pure)
# --------------------------------------------------------------------------- #
def _intervals(plist: dict) -> list[dict]:
    cal = plist.get("StartCalendarInterval")
    if isinstance(cal, dict):
        return [cal]
    if isinstance(cal, list):
        return [c for c in cal if isinstance(c, dict)]
    return []


def humanize_schedule(plist: dict) -> str:
    if _intervals(plist):
        parts: list[str] = []
        for iv in _intervals(plist):
            h, m = iv.get("Hour"), iv.get("Minute")
            wd, day = iv.get("Weekday"), iv.get("Day")
            t = f"{(h or 0):02d}:{(m or 0):02d}" if (h is not None or m is not None) else ""
            if wd is not None:
                parts.append(f"{WEEKDAYS.get(wd % 8, f'wd{wd}')} {t}".strip())
            elif day is not None:
                parts.append(f"Day {day} {t}".strip())
            else:
                parts.append(f"Daily {t}".strip())
        # de-dupe while preserving order
        return ", ".join(dict.fromkeys(parts)) or "calendar"
    if "StartInterval" in plist:
        s = int(plist["StartInterval"])
        if s and s % 3600 == 0:
            return f"Every {s // 3600}h"
        if s and s % 60 == 0:
            return f"Every {s // 60}m"
        return f"Every {s}s"
    if "WatchPaths" in plist:
        return "On file change"
    if "KeepAlive" in plist:
        return "Always on"
    if plist.get("RunAtLoad"):
        return "At login"
    return "Manual / on-demand"


def next_run(plist: dict, now: Optional[datetime] = None) -> Optional[datetime]:
    """Soonest future fire time for a calendar-scheduled agent (minute resolution).

    launchd treats an omitted field as a wildcard ("every"). Only calendar jobs get
    a precise next-run; interval/keepalive jobs return None (continuous/relative).
    """
    intervals = _intervals(plist)
    if not intervals:
        return None
    now = now or datetime.now()
    start = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
    best: Optional[datetime] = None
    for iv in intervals:
        cand = _next_for_interval(iv, start)
        if cand and (best is None or cand < best):
            best = cand
    return best


def _next_for_interval(iv: dict, start: datetime) -> Optional[datetime]:
    minute, hour = iv.get("Minute"), iv.get("Hour")
    weekday, day, month = iv.get("Weekday"), iv.get("Day"), iv.get("Month")
    dt = start
    for _ in range(366 * 24 * 60):  # cap: a year of minutes, break on first match
        if minute is not None and dt.minute != minute:
            dt += timedelta(minutes=1)
            continue
        if hour is not None and dt.hour != hour:
            dt += timedelta(minutes=1)
            continue
        if weekday is not None and (dt.isoweekday() % 7) != (weekday % 7):
            dt += timedelta(minutes=1)
            continue
        if day is not None and dt.day != day:
            dt += timedelta(minutes=1)
            continue
        if month is not None and dt.month != month:
            dt += timedelta(minutes=1)
            continue
        return dt
    return None


# --------------------------------------------------------------------------- #
# Logs
# --------------------------------------------------------------------------- #
def log_path_of(plist: dict) -> Optional[str]:
    return plist.get("StandardOutPath") or plist.get("StandardErrorPath")


def read_log_tail(plist: dict, lines: int = 200) -> dict:
    paths: list[str] = []
    for key in ("StandardOutPath", "StandardErrorPath"):
        p = plist.get(key)
        if p and p not in paths:
            paths.append(p)
    if not paths:
        return {"path": None, "text": "", "note": "no StandardOutPath / StandardErrorPath set"}
    path = paths[0]
    f = Path(path).expanduser()
    if not f.exists():
        return {"path": path, "text": "", "note": "log file not created yet"}
    try:
        data = f.read_text(errors="replace").splitlines()
    except OSError as exc:
        return {"path": path, "text": "", "note": f"could not read log: {exc}"}
    return {"path": path, "text": "\n".join(data[-lines:]), "note": ""}


def _last_run(plist: dict) -> Optional[str]:
    """Proxy for 'last run': mtime of the stdout log (when it exists)."""
    p = log_path_of(plist)
    if not p:
        return None
    f = Path(p).expanduser()
    if not f.exists():
        return None
    try:
        return datetime.fromtimestamp(f.stat().st_mtime).isoformat()
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# The list the API serves
# --------------------------------------------------------------------------- #
def is_healthy(pid: Optional[int], last_exit: Optional[int]) -> bool:
    """A live process is healthy — a past crash is history once KeepAlive (or the user)
    got it running again; the exit code stays visible in the row's subtitle. Idle or
    unloaded agents are judged by their last exit (None = never ran = fine)."""
    return pid is not None or last_exit in (0, None)


def build_agent(path: Path, plist: dict) -> Agent:
    label = plist.get("Label", path.stem)
    state = launchctl_state(label)
    pid = state.get("pid")
    last_exit = state.get("last_exit")
    if not state.get("loaded"):
        status = "unloaded"
    elif pid:
        status = "running"
    else:
        status = "idle"
    nr = next_run(plist)
    prog = plist.get("Program")
    if not prog:
        args = plist.get("ProgramArguments") or []
        prog = " ".join(args) if args else "—"
    return Agent(
        label=label,
        path=str(path),
        schedule=humanize_schedule(plist),
        status=status,
        healthy=is_healthy(pid, last_exit),
        last_exit=last_exit,
        pid=pid,
        program=prog,
        next_run=nr.isoformat() if nr else None,
        last_run=_last_run(plist),
        log_path=log_path_of(plist),
        vendor=is_vendor(label),
        raw_schedule={k: plist[k] for k in ("StartCalendarInterval", "StartInterval", "RunAtLoad", "KeepAlive") if k in plist},
    )


def list_agents(include_vendor: bool = False) -> list[dict]:
    agents: list[Agent] = []
    seen: set[str] = set()
    for path in discover_plists():
        plist = load_plist(path)
        if plist is None:
            continue
        label = plist.get("Label", path.stem)
        if label in seen:
            continue
        seen.add(label)
        # Dashboard-launched apps (com.launchddash.app.*) live in the Apps section,
        # not here — double-listing them as agents would just be noise.
        if label.startswith("com.launchddash.app."):
            continue
        if not include_vendor and is_vendor(label):
            continue
        agents.append(build_agent(path, plist))
    agents.sort(key=lambda a: (a.status != "running", a.label))
    return [a.as_dict() for a in agents]


def find_plist(label: str) -> Optional[Path]:
    for path in discover_plists():
        plist = load_plist(path)
        if plist and plist.get("Label", path.stem) == label:
            return path
    return None


# --------------------------------------------------------------------------- #
# Control (mutating) — localhost-only by design
# --------------------------------------------------------------------------- #
def _run(cmd: list[str]) -> dict:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "detail": str(exc)}
    return {
        "ok": res.returncode == 0,
        "detail": (res.stderr or res.stdout).strip(),
        "code": res.returncode,
    }


def _target(label: str) -> str:
    return f"gui/{UID}/{label}"


def run_now(label: str) -> dict:
    # -k restarts if already running; for an idle on-demand job it just fires it.
    invalidate_state(label)
    return _run(["launchctl", "kickstart", "-k", _target(label)])


def stop(label: str) -> dict:
    invalidate_state(label)
    return _run(["launchctl", "kill", "TERM", _target(label)])


def set_enabled(label: str, enabled: bool) -> dict:
    invalidate_state(label)
    return _run(["launchctl", "enable" if enabled else "disable", _target(label)])
