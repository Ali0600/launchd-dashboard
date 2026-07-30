"""Pure-logic + tmp-dir tests for project discovery. No live scanning of the real home."""

import json
from pathlib import Path

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


def test_infer_python_prefers_the_project_venv(tmp_path):
    """The sound-isolator shape: FastAPI app.py that self-launches, a project .venv, and
    a pile of task scripts that must NOT be mistaken for the launcher."""
    p = tmp_path / "sound-isolator"
    (p / ".venv" / "bin").mkdir(parents=True)
    (p / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
    (p / "app.py").write_text(
        'if __name__ == "__main__":\n    import uvicorn\n'
        '    uvicorn.run(app, host="127.0.0.1", port=8000)\n'
    )
    (p / "pyproject.toml").write_text("[project]\nname='sound-isolator'\n")
    for noise in ("isolate.sh", "render_job.sh", "cleanup.sh"):
        (p / noise).write_text("#!/bin/sh\necho pipeline\n")
    assert discover.infer_python(p) == {"command": ".venv/bin/python app.py", "port": 8000}
    c = discover.classify_project(p)
    assert c["launchable"] is True
    assert c["command"] == ".venv/bin/python app.py"  # not ./isolate.sh
    assert c["port"] == 8000


def test_infer_python_falls_back_to_uv_without_a_venv(tmp_path):
    p = tmp_path / "svc"
    p.mkdir()
    (p / "main.py").write_text("import uvicorn\nuvicorn.run(app, port=9100)\n")
    (p / "pyproject.toml").write_text("[project]\nname='svc'\n")
    assert discover.infer_python(p) == {"command": "uv run python main.py", "port": 9100}


def test_infer_python_needs_an_entry_point_and_an_environment(tmp_path):
    bare = tmp_path / "lib"
    bare.mkdir()
    (bare / "pyproject.toml").write_text("[project]\nname='lib'\n")
    assert discover.infer_python(bare) is None  # no app.py/main.py/server.py
    loose = tmp_path / "loose"
    loose.mkdir()
    (loose / "app.py").write_text("print('hi')\n")
    assert discover.infer_python(loose) is None  # no .venv and no pyproject


def test_python_entry_without_a_port_is_still_launchable(tmp_path):
    p = tmp_path / "worker"
    (p / ".venv" / "bin").mkdir(parents=True)
    (p / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
    (p / "server.py").write_text("serve_forever()\n")
    assert discover.infer_python(p) == {"command": ".venv/bin/python server.py", "port": None}


def test_uninferable_project_is_listed_not_dropped(tmp_path, monkeypatch):
    """Silently skipping reads as 'the scanner is broken' — the reason must be visible."""
    make_project(tmp_path, "docs-only", {"README.md": "# just docs\n"})
    monkeypatch.setattr(discover, "tcc_blocked", lambda d: False)
    [c] = discover.discover_apps(roots=[tmp_path], existing=[])
    assert c["slug"] == "docs-only"
    assert c["launchable"] is False
    assert c["command"] is None
    assert "no dev.sh/run.sh" in c["reason"]


def test_adopt_refuses_an_uninferable_project(tmp_path):
    cfg = tmp_path / "apps.json"
    cands = [{"slug": "docs-only", "name": "docs", "dir": "/d", "command": None, "launchable": False}]
    res = discover.adopt_apps(cands, ["docs-only"], config=cfg)
    assert res["added"] == []
    assert "no launch command" in res["skipped"][0]
    assert not cfg.exists()


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
        tmp_path / "projects",  # same path twice
        tmp_path / "dev",
        tmp_path / "nope",      # missing -> skipped
    ])
    assert [p.name for p in roots] == [tmp_path.name, "projects", "dev"]


def test_dir_identity_is_the_same_for_two_paths_to_one_directory(tmp_path):
    """Filesystem identity, not path text: `resolve()` can't tell that two spellings are
    one directory on a case-insensitive volume. This runs everywhere via a symlink."""
    real = tmp_path / "projects"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    assert discover.dir_identity(real) == discover.dir_identity(alias)
    assert discover.dir_identity(tmp_path) != discover.dir_identity(real)
    assert discover.dir_identity(tmp_path / "missing") is None


def test_scan_roots_dedupes_case_spellings_of_one_directory(tmp_path):
    """The live bug: on a case-INsensitive volume (macOS default) ~/projects and
    ~/Projects are ONE directory that `resolve()` reports as two, so every project in
    it was listed twice. NOTE this only *bites* on a case-insensitive filesystem — on
    case-sensitive CI the second spelling simply doesn't exist and is skipped, so the
    macOS run is the one that proves it."""
    (tmp_path / "projects").mkdir()
    roots = discover.scan_roots([tmp_path / "projects", tmp_path / "Projects"])
    assert len(roots) == 1


def test_discover_lists_an_aliased_project_once(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    projects.mkdir()
    make_project(projects, "waymark", {"package.json": json.dumps({"scripts": {"dev": "vite"}})})
    monkeypatch.setattr(discover, "tcc_blocked", lambda d: False)
    out = discover.discover_apps(roots=[projects, tmp_path / "Projects"], existing=[])
    assert [c["slug"] for c in out] == ["waymark"]


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


def _moved_setup(tmp_path, keep_old=False):
    """A project configured at <old> that now lives at <new>."""
    old, new = tmp_path / "Documents" / "proj", tmp_path / "projects" / "proj"
    (tmp_path / "Documents").mkdir()
    (tmp_path / "projects").mkdir()
    make_project(tmp_path / "projects", "proj", {"package.json": json.dumps({"scripts": {"dev": "vite"}})})
    if keep_old:
        make_project(tmp_path / "Documents", "proj", {"package.json": json.dumps({"scripts": {"dev": "vite"}})})
    spec = AppSpec(slug="proj", name="proj", dir=str(old), command="npm run dev", port=3000)
    return old, new, spec


def test_moved_project_is_flagged_with_its_previous_dir(tmp_path, monkeypatch):
    """The reported bug: a moved project read 'Ready' (dir-keyed) but adopt skipped it
    as 'already configured' (slug-keyed)."""
    old, new, spec = _moved_setup(tmp_path)
    monkeypatch.setattr(discover, "tcc_blocked", lambda d: False)
    [c] = discover.discover_apps(roots=[tmp_path / "projects"], existing=[spec])
    assert c["moved"] is True
    assert c["conflict"] is False
    assert c["already"] is False
    assert c["previous_dir"].endswith("Documents/proj")


def test_two_copies_on_disk_is_a_conflict_not_a_move(tmp_path, monkeypatch):
    """Repointing while the old dir still exists would silently retarget a working app."""
    old, new, spec = _moved_setup(tmp_path, keep_old=True)
    monkeypatch.setattr(discover, "tcc_blocked", lambda d: False)
    [c] = discover.discover_apps(roots=[tmp_path / "projects"], existing=[spec])
    assert c["moved"] is False
    assert c["conflict"] is True
    assert "still exists" in c["reason"]


def test_same_dir_is_already_not_moved(tmp_path, monkeypatch):
    make_project(tmp_path, "proj", {"package.json": json.dumps({"scripts": {"dev": "vite"}})})
    spec = AppSpec(slug="proj", name="proj", dir=str(tmp_path / "proj"), command="npm run dev")
    monkeypatch.setattr(discover, "tcc_blocked", lambda d: False)
    [c] = discover.discover_apps(roots=[tmp_path], existing=[spec])
    assert c["already"] is True and c["moved"] is False and c["conflict"] is False


def test_adopt_repairs_a_moved_path_preserving_hand_edits(tmp_path):
    cfg = tmp_path / "apps.json"
    cfg.write_text(json.dumps([
        {"slug": "proj", "name": "My Project", "dir": "~/Documents/proj",
         "command": "npm run dev -- -p 3020", "port": 3020, "env": {"CI": "1"}, "login": True},
        {"slug": "other", "dir": "~/other", "command": "run"},
    ]))
    cand = {"slug": "proj", "name": "proj", "dir": str(Path.home() / "projects" / "proj"),
            "command": "npm run dev", "port": 3000, "launchable": True, "moved": True,
            "conflict": False, "previous_dir": "~/Documents/proj"}
    res = discover.adopt_apps([cand], ["proj"], config=cfg)
    assert res["updated"] == ["proj"] and res["added"] == []
    saved = json.loads(cfg.read_text())
    assert len(saved) == 2  # repaired in place, not appended
    entry = saved[0]
    assert entry["dir"] == "~/projects/proj"          # path repaired
    assert entry["name"] == "My Project"              # hand edits survive
    assert entry["command"] == "npm run dev -- -p 3020"
    assert entry["port"] == 3020
    assert entry["env"] == {"CI": "1"} and entry["login"] is True
    assert saved[1] == {"slug": "other", "dir": "~/other", "command": "run"}


def test_adopt_refuses_a_conflicting_slug(tmp_path):
    cfg = tmp_path / "apps.json"
    original = json.dumps([{"slug": "proj", "dir": "~/Documents/proj", "command": "run"}])
    cfg.write_text(original)
    cand = {"slug": "proj", "name": "proj", "dir": "/x/proj", "command": "npm run dev",
            "launchable": True, "moved": False, "conflict": True, "reason": "slug already used by ~/Documents/proj, which still exists"}
    res = discover.adopt_apps([cand], ["proj"], config=cfg)
    assert res["added"] == [] and res["updated"] == []
    assert "still exists" in res["skipped"][0]
    assert cfg.read_text() == original


def test_everything_the_ui_offers_is_adoptable(tmp_path, monkeypatch):
    """THE invariant this bug violated: discover and adopt must agree. Any candidate the
    UI renders with an enabled checkbox must not come back 'already configured'."""
    old, new, spec = _moved_setup(tmp_path)
    make_project(tmp_path / "projects", "fresh", {"run.sh": "serve --port 9000\n"})
    make_project(tmp_path / "projects", "docs-only", {"README.md": "# docs\n"})
    monkeypatch.setattr(discover, "tcc_blocked", lambda d: False)
    cands = discover.discover_apps(roots=[tmp_path / "projects"], existing=[spec])
    offered = [c["slug"] for c in cands
               if c["launchable"] and not c["already"] and not c.get("conflict")]
    assert set(offered) == {"proj", "fresh"}

    cfg = tmp_path / "apps.json"
    cfg.write_text(json.dumps([{"slug": "proj", "dir": str(old), "command": "npm run dev"}]))
    res = discover.adopt_apps(cands, offered, config=cfg)
    assert res["skipped"] == [], f"the UI offered something adopt refused: {res['skipped']}"
    assert set(res["added"] + res["updated"]) == set(offered)


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
