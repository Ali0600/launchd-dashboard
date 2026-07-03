"""Discover launchable projects for the Apps section — the onboarding path.

Scans the configured roots (home root + ~/Documents, one level deep) for git
repos and infers how to start each one: a dev.sh/run.sh, or an npm `dev`/`start`
script (including one level into npm workspaces, e.g. `npm run dev -w web`).
Ports come from the script text (`--port 5173`) or the framework's default.

Candidates are generated entirely server-side; the browser only posts back WHICH
slugs to adopt — commands never cross HTTP, same trust boundary as the launcher.
TCC-blocked projects are included (marked) so the "move it out of ~/Documents"
constraint teaches itself during onboarding.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from .apps import CONFIG_PATH, AppSpec, load_apps, tcc_blocked, warn

DEFAULT_ROOTS = [Path.home(), Path.home() / "Documents"]

# Framework dev-server defaults, keyed by the dependency that implies them.
FRAMEWORK_PORTS = [
    ("next", 3000),
    ("nuxt", 3000),
    ("vite", 5173),
    ("astro", 4321),
    ("expo", 8081),
    ("react-scripts", 3000),
]

_PORT_RE = re.compile(r"(?:--port[= ]|-p )(\d{2,5})")


def port_from_text(text: str) -> Optional[int]:
    """First explicit --port/-p in a script line or shell script body."""
    m = _PORT_RE.search(text)
    return int(m.group(1)) if m else None


def port_from_deps(deps: dict) -> Optional[int]:
    for dep, port in FRAMEWORK_PORTS:
        if dep in deps:
            return port
    return None


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return slug or "app"


def _all_deps(pj: dict) -> dict:
    return {**pj.get("dependencies", {}), **pj.get("devDependencies", {})}


def infer_npm(pj: dict, workspaces: list[tuple[str, dict]]) -> Optional[dict]:
    """Infer an npm launch from a package.json (+ its workspace packages).

    Preference: own `dev` script > a workspace's `dev` script > own `start`.
    Returns {command, port} or None when nothing is launchable.
    """
    scripts = pj.get("scripts", {})
    if "dev" in scripts:
        port = port_from_text(str(scripts["dev"])) or port_from_deps(_all_deps(pj))
        return {"command": "npm run dev", "port": port}
    for ws_name, ws_pj in workspaces:
        ws_scripts = ws_pj.get("scripts", {})
        if "dev" in ws_scripts:
            port = port_from_text(str(ws_scripts["dev"])) or port_from_deps(_all_deps(ws_pj))
            return {"command": f"npm run dev -w {ws_name}", "port": port}
    if "start" in scripts:
        port = port_from_text(str(scripts["start"])) or port_from_deps(_all_deps(pj))
        return {"command": "npm start", "port": port}
    return None


def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _workspace_packages(project: Path, pj: dict) -> list[tuple[str, dict]]:
    """Resolve npm `workspaces` globs one level (the common `packages/*` shape)."""
    out: list[tuple[str, dict]] = []
    globs = pj.get("workspaces")
    if not isinstance(globs, list):
        return out
    for pattern in globs:
        if not isinstance(pattern, str):
            continue
        for ws_dir in sorted(project.glob(pattern)):
            ws_pj = _read_json(ws_dir / "package.json")
            if ws_pj and ws_pj.get("name"):
                out.append((str(ws_pj["name"]), ws_pj))
    return out


def classify_project(project: Path) -> Optional[dict]:
    """One directory -> a candidate {slug,name,dir,command,port} or None."""
    dev_sh = project / "dev.sh"
    run_sh = project / "run.sh"
    for script in (dev_sh, run_sh):
        if script.is_file():
            try:
                port = port_from_text(script.read_text())
            except OSError:
                port = None
            return {
                "slug": slugify(project.name),
                "name": project.name,
                "dir": str(project),
                "command": f"./{script.name}",
                "port": port,
            }
    pj = _read_json(project / "package.json")
    if pj:
        launch = infer_npm(pj, _workspace_packages(project, pj))
        if launch:
            return {
                "slug": slugify(project.name),
                "name": str(pj.get("name") or project.name),
                "dir": str(project),
                "command": launch["command"],
                "port": launch["port"],
            }
    return None


def discover_apps(
    roots: Optional[list[Path]] = None,
    existing: Optional[list[AppSpec]] = None,
) -> list[dict]:
    """Scan roots (one level) for launchable git projects. Includes already-configured
    and TCC-blocked projects, marked, so the UI can show the full picture."""
    roots = roots if roots is not None else DEFAULT_ROOTS
    existing = existing if existing is not None else load_apps()
    known_dirs = {spec.dir for spec in existing}
    dashboard_root = str(CONFIG_PATH.parent)
    out: list[dict] = []
    seen_dirs: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for project in sorted(root.iterdir()):
            p = str(project)
            if p in seen_dirs or p == dashboard_root:
                continue
            if not project.is_dir() or not (project / ".git").exists():
                continue
            candidate = classify_project(project)
            if not candidate:
                continue
            seen_dirs.add(p)
            candidate["blocked"] = tcc_blocked(p)
            candidate["already"] = p in known_dirs
            out.append(candidate)
    # Ready first, then blocked, already-configured last; stable by name within groups.
    out.sort(key=lambda c: (c["already"], c["blocked"], c["name"].lower()))
    return out


def adopt_apps(candidates: list[dict], slugs: list[str], config: Path = CONFIG_PATH) -> dict:
    """Merge chosen candidates into apps.json. Appends only — existing entries
    (including hand-edited fields) are never touched; duplicate slugs/dirs skip."""
    by_slug = {c["slug"]: c for c in candidates}
    try:
        raw = json.loads(config.read_text()) if config.exists() else []
    except (OSError, json.JSONDecodeError) as exc:
        warn(f"apps.json unreadable, refusing to overwrite: {exc}")
        return {"ok": False, "detail": f"apps.json unreadable ({exc}) — fix it by hand first"}
    if not isinstance(raw, list):
        return {"ok": False, "detail": "apps.json is not a JSON array — fix it by hand first"}
    taken_slugs = {str(e.get("slug")) for e in raw if isinstance(e, dict)}
    taken_dirs = {str(e.get("dir")) for e in raw if isinstance(e, dict)}
    added, skipped = [], []
    for slug in slugs:
        c = by_slug.get(slug)
        if c is None:
            skipped.append(f"{slug} (not in the last scan)")
            continue
        if c["slug"] in taken_slugs or c["dir"] in taken_dirs or tilde(c["dir"]) in taken_dirs:
            skipped.append(f"{slug} (already configured)")
            continue
        entry = {"slug": c["slug"], "name": c["name"], "dir": tilde(c["dir"]), "command": c["command"]}
        if c.get("port"):
            entry["port"] = c["port"]
        raw.append(entry)
        taken_slugs.add(c["slug"])
        added.append(slug)
    if added:
        try:
            config.write_text(json.dumps(raw, indent=2) + "\n")
        except OSError as exc:
            return {"ok": False, "detail": f"could not write apps.json: {exc}"}
    return {"ok": True, "added": added, "skipped": skipped}


def tilde(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path
