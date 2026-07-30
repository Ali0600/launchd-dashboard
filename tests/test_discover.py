"""Pure-logic + tmp-dir tests for project discovery. No live scanning of the real home."""

import json

from app import discover
from app.apps import AppSpec


def test_port_from_text():
    assert discover.port_from_text("uvicorn app.main:app --port 8001") == 8001
    assert discover.port_from_text("vite -p 5173 --strictPort") == 5173
    assert discover.port_from_text("next dev") is None


def test_port_from_deps_and_slugify():
    assert discover.port_from_deps({"next": "^16.0.0"}) == 3000
    assert discover.port_from_deps({"vite": "^6.0.0"}) == 5173
    assert discover.port_from_deps({"lodash": "^4"}) is None
    assert discover.slugify("My Project!") == "my-project"
    assert discover.slugify("3D-globe") == "3d-globe"


def test_infer_npm_preference_order():
    dev = {"scripts": {"dev": "next dev"}, "dependencies": {"next": "^16"}}
    assert discover.infer_npm(dev, []) == {"command": "npm run dev", "port": 3000}
    explicit = {"scripts": {"dev": "vite --port 4000"}, "dependencies": {"vite": "^6"}}
    assert discover.infer_npm(explicit, [])["port"] == 4000  # script beats framework default
    ws = {"scripts": {"build": "tsup"}, "workspaces": ["packages/*"]}
    ws_pkg = ("@preflight/web", {"scripts": {"dev": "next dev"}, "dependencies": {"next": "^16"}})
    assert discover.infer_npm(ws, [ws_pkg]) == {"command": "npm run dev -w @preflight/web", "port": 3000}
    start_only = {"scripts": {"start": "node server.js --port 4321"}}
    assert discover.infer_npm(start_only, []) == {"command": "npm start", "port": 4321}
    assert discover.infer_npm({"scripts": {"build": "tsc"}}, []) is None


def make_project(root, name, files):
    p = root / name
    (p / ".git").mkdir(parents=True)
    for fname, content in files.items():
        (p / fname).write_text(content)
    return p


def test_classify_project_shell_script_beats_npm(tmp_path):
    p = make_project(tmp_path, "api", {
        "dev.sh": "#!/bin/sh\nuvicorn app.main:app --port 8001 & npx expo start --web\n",
        "package.json": json.dumps({"scripts": {"dev": "x"}}),
    })
    c = discover.classify_project(p)
    assert c["command"] == "./dev.sh"
    assert c["port"] == 8001  # first explicit port in the script


def test_discover_apps_marks_already_and_blocked(tmp_path, monkeypatch):
    make_project(tmp_path, "fresh", {"package.json": json.dumps({"scripts": {"dev": "vite"}, "dependencies": {"vite": "1"}})})
    make_project(tmp_path, "configured", {"run.sh": "serve --port 9000\n"})
    make_project(tmp_path, "docs-jail", {"package.json": json.dumps({"scripts": {"dev": "next dev"}})})
    (tmp_path / "not-git").mkdir()
    (tmp_path / "not-git" / "package.json").write_text(json.dumps({"scripts": {"dev": "x"}}))

    monkeypatch.setattr(discover, "tcc_blocked", lambda d: d.endswith("docs-jail"))
    existing = [AppSpec(slug="cfg", name="cfg", dir=str(tmp_path / "configured"), command="./run.sh")]
    out = discover.discover_apps(roots=[tmp_path], existing=existing)

    by = {c["slug"]: c for c in out}
    assert set(by) == {"fresh", "configured", "docs-jail"}  # not-git excluded
    assert by["fresh"] == {**by["fresh"], "blocked": False, "already": False}
    assert by["docs-jail"]["blocked"] is True
    assert by["configured"]["already"] is True
    # ready first, blocked next, already-configured last
    assert [c["slug"] for c in out] == ["fresh", "docs-jail", "configured"]


def test_discover_skips_the_dashboard_itself(tmp_path, monkeypatch):
    me = make_project(tmp_path, "dashboard", {"run.sh": "uvicorn --port 8787\n"})
    monkeypatch.setattr(discover, "CONFIG_PATH", me / "apps.json")
    assert discover.discover_apps(roots=[tmp_path], existing=[]) == []


def test_scan_roots_keeps_existing_dirs_and_dedupes(tmp_path):
    (tmp_path / "projects").mkdir()
    (tmp_path / "dev").mkdir()
    roots = discover.scan_roots([
        tmp_path,
        tmp_path / "projects",
        tmp_path / "projects",  # duplicate — also the ~/projects vs ~/Projects case on
        tmp_path / "dev",       # case-insensitive macOS volumes
        tmp_path / "nope",      # missing -> skipped
    ])
    assert [p.name for p in roots] == [tmp_path.name, "projects", "dev"]


def test_candidate_roots_include_a_dedicated_projects_dir():
    """A ~/projects folder is the common convention — scanning only ~ and ~/Documents
    missed a whole tree of the user's work."""
    names = [p.name for p in discover.CANDIDATE_ROOTS]
    for expected in ("projects", "dev", "code", "Documents"):
        assert expected in names


def test_discover_scans_multiple_roots(tmp_path, monkeypatch):
    """A project in a dedicated projects dir is found just like one at the home root."""
    home, projects = tmp_path / "home", tmp_path / "home" / "projects"
    home.mkdir()
    projects.mkdir()
    make_project(home, "at-home", {"run.sh": "serve --port 9000\n"})
    make_project(projects, "in-projects", {"package.json": json.dumps({"scripts": {"dev": "vite"}})})
    monkeypatch.setattr(discover, "tcc_blocked", lambda d: False)
    out = discover.discover_apps(roots=[home, projects], existing=[])
    assert {c["slug"] for c in out} == {"at-home", "in-projects"}
    assert next(c for c in out if c["slug"] == "in-projects")["dir"].endswith("/projects/in-projects")


def test_adopt_appends_without_touching_existing(tmp_path):
    cfg = tmp_path / "apps.json"
    cfg.write_text(json.dumps([{"slug": "old", "dir": "~/old", "command": "run", "env": {"CI": "1"}}]))
    cands = [
        {"slug": "fresh", "name": "Fresh", "dir": str(tmp_path / "fresh"), "command": "npm run dev", "port": 5173},
        {"slug": "old", "name": "Old", "dir": "/x", "command": "other"},
    ]
    res = discover.adopt_apps(cands, ["fresh", "old", "ghost"], config=cfg)
    assert res["ok"] and res["added"] == ["fresh"]
    assert any("already configured" in s for s in res["skipped"])
    assert any("not in the last scan" in s for s in res["skipped"])
    saved = json.loads(cfg.read_text())
    assert saved[0] == {"slug": "old", "dir": "~/old", "command": "run", "env": {"CI": "1"}}  # untouched
    assert saved[1]["slug"] == "fresh"
    assert saved[1]["port"] == 5173


def test_adopt_warns_on_duplicate_declared_port(tmp_path):
    cfg = tmp_path / "apps.json"
    cfg.write_text(json.dumps([{"slug": "web", "dir": "~/web", "command": "npm run dev", "port": 3000}]))
    cands = [{"slug": "landing", "name": "landing", "dir": "/l", "command": "npm run dev", "port": 3000}]
    res = discover.adopt_apps(cands, ["landing"], config=cfg)
    assert res["ok"] and res["added"] == ["landing"]  # still added — sharing is legal
    assert len(res["warnings"]) == 1
    assert "port 3000 is also declared by web" in res["warnings"][0]


def test_adopt_refuses_malformed_config(tmp_path):
    cfg = tmp_path / "apps.json"
    cfg.write_text("{oops")
    res = discover.adopt_apps([{"slug": "a", "name": "a", "dir": "/a", "command": "x"}], ["a"], config=cfg)
    assert res["ok"] is False
    assert "fix it by hand" in res["detail"]
    assert cfg.read_text() == "{oops"  # never clobbered


def test_adopt_creates_config_when_missing(tmp_path):
    cfg = tmp_path / "apps.json"
    res = discover.adopt_apps([{"slug": "a", "name": "a", "dir": "/a", "command": "x", "port": None}], ["a"], config=cfg)
    assert res["ok"] and res["added"] == ["a"]
    saved = json.loads(cfg.read_text())
    assert saved == [{"slug": "a", "name": "a", "dir": "/a", "command": "x"}]  # null port omitted
