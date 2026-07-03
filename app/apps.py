"""Launch dev apps as transient launchd agents — the dashboard as a project launcher.

An "app" is declared once in apps.json (slug, name, dir, command, port). Start writes
a com.launchddash.app.<slug> plist and bootstraps it; Stop boots it out and deletes
the plist, so nothing lingers in login items. Because a launched app IS a launchd
job, everything the dashboard already does — status, last-exit, log tail, port
attribution via the ppid chain — applies with no extra plumbing.

Security model: the HTTP layer only ever passes a slug; commands come exclusively
from the local config file, same trust boundary as the existing launchctl endpoints.
"""

from __future__ import annotations

import glob
import json
import os
import plistlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .launchd import launchctl_state


def warn(msg: str) -> None:
    """Every skipped config entry / degraded path announces itself in the server log."""
    print(f"launchddash: {msg}", file=sys.stderr)

APP_LABEL_PREFIX = "com.launchddash.app."
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
LOG_DIR = Path.home() / "Library" / "Logs" / "launchddash"
CONFIG_PATH = Path(__file__).resolve().parent.parent / "apps.json"

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# macOS TCC-protected folders: a launchd agent gets EPERM in these (no prompt),
# even though the same command works from Terminal. Refuse with a clear reason
# instead of letting the app die with a cryptic PermissionError.
_TCC_DIRS = ("Documents", "Desktop", "Downloads")


@dataclass
class AppSpec:
    slug: str
    name: str
    dir: str  # absolute, ~ expanded
    command: str
    port: Optional[int] = None
    # Keep the plist installed after Stop, so the app comes back at next login.
    login: bool = False
    # Extra environment for the generated agent (merged over the baked PATH/HOME) —
    # the escape hatch for tools that behave differently without a TTY (CI=1 etc.).
    env: Optional[dict] = None

    @property
    def label(self) -> str:
        return APP_LABEL_PREFIX + self.slug

    @property
    def log_path(self) -> Path:
        return LOG_DIR / f"{self.slug}.log"


def is_app_label(label: str) -> bool:
    return label.startswith(APP_LABEL_PREFIX)


# --------------------------------------------------------------------------- #
# Config (pure parsing; fixture-tested)
# --------------------------------------------------------------------------- #
def parse_apps(raw: str, home: Optional[str] = None) -> list[AppSpec]:
    """Parse apps.json content. Invalid entries are skipped with a warning —
    one typo shouldn't take the whole section down."""
    home = home or str(Path.home())
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        warn(f"apps.json is not valid JSON: {exc}")
        return []
    if not isinstance(data, list):
        warn("apps.json must be a JSON array of app objects")
        return []
    out: list[AppSpec] = []
    seen: set[str] = set()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        slug = str(entry.get("slug", ""))
        command = str(entry.get("command", "")).strip()
        directory = str(entry.get("dir", "")).strip()
        if not _SLUG_RE.match(slug):
            warn(f"apps.json: skipping entry with invalid slug {slug!r} (use [a-z0-9-])")
            continue
        if slug in seen:
            warn(f"apps.json: skipping duplicate slug {slug!r}")
            continue
        if not command or not directory:
            warn(f"apps.json: skipping {slug!r} — 'dir' and 'command' are required")
            continue
        if directory == "~" or directory.startswith("~/"):
            directory = home + directory[1:]
        port = entry.get("port")
        env = entry.get("env")
        out.append(
            AppSpec(
                slug=slug,
                name=str(entry.get("name") or slug),
                dir=directory,
                command=command,
                port=int(port) if isinstance(port, (int, str)) and str(port).isdigit() else None,
                login=bool(entry.get("login")),
                env={str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else None,
            )
        )
        seen.add(slug)
    return out


def load_apps(path: Path = CONFIG_PATH) -> list[AppSpec]:
    if not path.exists():
        return []
    try:
        return parse_apps(path.read_text())
    except OSError as exc:
        warn(f"could not read {path}: {exc}")
        return []


def tcc_blocked(directory: str, home: Optional[str] = None) -> bool:
    """True when the dir sits in a TCC-protected folder launchd can't read."""
    home = home or str(Path.home())
    for name in _TCC_DIRS:
        prefix = f"{home}/{name}"
        if directory == prefix or directory.startswith(prefix + "/"):
            return True
    return False


# --------------------------------------------------------------------------- #
# Plist generation (pure; fixture-tested)
# --------------------------------------------------------------------------- #
def robust_path(home: Optional[str] = None) -> str:
    """A PATH that works under launchd's minimal env: homebrew + ~/.local/bin +
    the newest fnm node + the system dirs (same recipe as the proven weekly agent —
    version-manager shims aren't there for a daemon)."""
    home = home or str(Path.home())
    parts = [f"{home}/.local/bin", "/opt/homebrew/bin", "/usr/local/bin"]
    node_bins = sorted(glob.glob(f"{home}/.local/share/fnm/node-versions/*/installation/bin"))
    if node_bins:
        parts.insert(0, node_bins[-1])  # newest by version-ish sort
    parts += ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    return ":".join(parts)


def render_plist(spec: AppSpec, path_env: str) -> dict:
    """The transient agent definition. No KeepAlive on purpose: a crashed dev
    server should read as failed, not thrash in a restart loop."""
    env = {"PATH": path_env, "HOME": str(Path.home())}
    env.update(spec.env or {})  # per-app overrides win (CI=1, EXPO_* etc.)
    return {
        "Label": spec.label,
        "ProgramArguments": [
            "/bin/zsh",
            "-c",
            f"cd {shquote(spec.dir)} && exec {spec.command}",
        ],
        "WorkingDirectory": spec.dir,
        "EnvironmentVariables": env,
        "RunAtLoad": True,
        "StandardOutPath": str(spec.log_path),
        "StandardErrorPath": str(spec.log_path),
    }


def shquote(s: str) -> str:
    """Single-quote for zsh -c (the dir comes from local config, but quote anyway)."""
    return "'" + s.replace("'", "'\\''") + "'"


# --------------------------------------------------------------------------- #
# Status + control (subprocess wrappers)
# --------------------------------------------------------------------------- #
def app_state(loaded: bool, pid: Optional[int], last_exit: Optional[int]) -> str:
    """running | stopped (not loaded) | exited (loaded, clean end) | failed."""
    if not loaded:
        return "stopped"
    if pid:
        return "running"
    return "exited" if last_exit in (0, None) else "failed"


def describe(spec: AppSpec) -> dict:
    state = launchctl_state(spec.label)
    return {
        "slug": spec.slug,
        "name": spec.name,
        "dir": spec.dir,
        "command": spec.command,
        "port": spec.port,
        "status": app_state(bool(state.get("loaded")), state.get("pid"), state.get("last_exit")),
        "pid": state.get("pid"),
        "last_exit": state.get("last_exit"),
        "blocked": tcc_blocked(spec.dir),
        "login": spec.login,
        "log_path": str(spec.log_path),
    }


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=15)


def _plist_file(spec: AppSpec) -> Path:
    return LAUNCH_AGENTS_DIR / f"{spec.label}.plist"


def start_app(spec: AppSpec) -> dict:
    if tcc_blocked(spec.dir):
        return {
            "ok": False,
            "detail": f"{spec.dir} is in a TCC-protected folder — launchd agents get "
            "'Operation not permitted' there. Move the project out of "
            "Documents/Desktop/Downloads to launch it from the dashboard.",
        }
    if not Path(spec.dir).is_dir():
        return {"ok": False, "detail": f"directory not found: {spec.dir}"}
    state = launchctl_state(spec.label)
    if state.get("pid"):
        return {"ok": True, "detail": f"already running (pid {state['pid']})"}
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        # Truncate the old log so "Logs" shows this run, not history.
        spec.log_path.write_text("")
        file = _plist_file(spec)
        with file.open("wb") as fh:
            plistlib.dump(render_plist(spec, robust_path()), fh)
    except OSError as exc:
        return {"ok": False, "detail": f"could not write agent plist: {exc}"}
    if state.get("loaded"):  # a previous run's job is still registered — clear it first
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/{spec.label}"])
    res = _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(file)])
    if res.returncode != 0:
        return {"ok": False, "detail": (res.stderr or res.stdout).strip() or "bootstrap failed"}
    return {"ok": True, "detail": f"started {spec.label}"}


def stop_app(spec: AppSpec) -> dict:
    res = _run(["launchctl", "bootout", f"gui/{os.getuid()}/{spec.label}"])
    if spec.login:
        # Keep the plist: the app stays stopped now but comes back at next login.
        suffix = " (plist kept — starts at login)"
    else:
        suffix = ""
        try:
            _plist_file(spec).unlink(missing_ok=True)  # ephemeral: nothing left in login items
        except OSError as exc:
            warn(f"could not remove {spec.label} plist: {exc}")
    if res.returncode != 0:
        detail = (res.stderr or res.stdout).strip()
        # Booting out a job that isn't loaded is a no-op, not an error worth surfacing.
        return {"ok": True, "detail": (detail or "was not running") + suffix}
    return {"ok": True, "detail": f"stopped {spec.label}{suffix}"}


def restart_app(spec: AppSpec) -> dict:
    """Bootout (ignore result — it may simply not be running) then a fresh start,
    which rewrites the plist, so config edits are picked up on restart."""
    _run(["launchctl", "bootout", f"gui/{os.getuid()}/{spec.label}"])
    return start_app(spec)
