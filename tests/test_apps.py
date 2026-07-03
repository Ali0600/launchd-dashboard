"""Pure-logic tests for the app launcher — config parsing, plist rendering, state
mapping, TCC guard. No live launchctl (fixtures only), same policy as the rest."""

import plistlib

from app import apps

HOME = "/Users/dev"


def spec(**over):
    base = dict(slug="web", name="Web", dir="/Users/dev/web", command="npm run dev", port=3000)
    base.update(over)
    return apps.AppSpec(**base)


def test_parse_apps_valid_and_tilde_expansion():
    raw = '[{"slug": "web", "name": "Web", "dir": "~/web", "command": "npm run dev", "port": 3000}]'
    [a] = apps.parse_apps(raw, home=HOME)
    assert a.dir == "/Users/dev/web"
    assert a.label == "com.launchddash.app.web"
    assert a.port == 3000


def test_parse_apps_skips_bad_entries_keeps_good():
    raw = """[
      {"slug": "ok", "dir": "~/x", "command": "make run"},
      {"slug": "Bad Slug!", "dir": "~/y", "command": "z"},
      {"slug": "ok", "dir": "~/dup", "command": "z"},
      {"slug": "no-cmd", "dir": "~/y"},
      "not-an-object"
    ]"""
    out = apps.parse_apps(raw, home=HOME)
    assert [a.slug for a in out] == ["ok"]
    assert out[0].name == "ok"  # name defaults to slug
    assert out[0].port is None


def test_parse_apps_garbage_json_is_empty_not_fatal():
    assert apps.parse_apps("{oops", home=HOME) == []
    assert apps.parse_apps('{"not": "a list"}', home=HOME) == []


def test_tcc_blocked():
    assert apps.tcc_blocked("/Users/dev/Documents/proj", home=HOME)
    assert apps.tcc_blocked("/Users/dev/Desktop", home=HOME)
    assert apps.tcc_blocked("/Users/dev/Downloads/x/y", home=HOME)
    assert not apps.tcc_blocked("/Users/dev/proj", home=HOME)
    assert not apps.tcc_blocked("/Users/dev/Documents-archive", home=HOME)  # prefix, not folder


def test_robust_path_includes_newest_fnm_node(tmp_path):
    for v in ("v18.19.0", "v22.14.0"):
        (tmp_path / ".local/share/fnm/node-versions" / v / "installation/bin").mkdir(parents=True)
    p = apps.robust_path(home=str(tmp_path))
    first = p.split(":")[0]
    assert first.endswith("v22.14.0/installation/bin")  # newest wins
    assert "/opt/homebrew/bin" in p and "/usr/bin" in p


def test_robust_path_without_fnm_still_sane():
    p = apps.robust_path(home="/nonexistent")
    assert p.startswith("/nonexistent/.local/bin:/opt/homebrew/bin")


def test_render_plist_shape():
    d = apps.render_plist(spec(), path_env="/fake/bin:/usr/bin")
    assert d["Label"] == "com.launchddash.app.web"
    assert d["ProgramArguments"][0:2] == ["/bin/zsh", "-c"]
    assert d["ProgramArguments"][2] == "cd '/Users/dev/web' && exec npm run dev"
    assert d["EnvironmentVariables"]["PATH"] == "/fake/bin:/usr/bin"
    assert d["RunAtLoad"] is True
    assert "KeepAlive" not in d  # a crashed dev server must not restart-loop
    assert d["StandardOutPath"].endswith("web.log")
    plistlib.dumps(d)  # round-trips to a valid plist


def test_shquote_handles_single_quotes():
    assert apps.shquote("/a/it's here") == "'/a/it'\\''s here'"


def test_app_state_mapping():
    assert apps.app_state(False, None, None) == "stopped"
    assert apps.app_state(True, 123, None) == "running"
    assert apps.app_state(True, 123, 1) == "running"  # running now beats old crash
    assert apps.app_state(True, None, 0) == "exited"
    assert apps.app_state(True, None, None) == "exited"
    assert apps.app_state(True, None, 256) == "failed"


def test_start_app_refuses_tcc_dir(monkeypatch):
    monkeypatch.setattr(apps, "tcc_blocked", lambda d: True)
    res = apps.start_app(spec())
    assert res["ok"] is False
    assert "TCC-protected" in res["detail"]


def test_start_app_refuses_missing_dir(monkeypatch):
    monkeypatch.setattr(apps, "tcc_blocked", lambda d: False)
    res = apps.start_app(spec(dir="/no/such/dir"))
    assert res["ok"] is False
    assert "not found" in res["detail"]


def test_load_apps_missing_file_is_empty(tmp_path):
    assert apps.load_apps(tmp_path / "nope.json") == []
    cfg = tmp_path / "apps.json"
    cfg.write_text('[{"slug": "a", "dir": "~/a", "command": "run"}]')
    assert [s.slug for s in apps.load_apps(cfg)] == ["a"]


def test_is_app_label_and_agents_filter():
    assert apps.is_app_label("com.launchddash.app.web")
    assert not apps.is_app_label("com.launchddash.server")
    assert not apps.is_app_label("com.groceryhelper.recipes")


def test_parse_apps_login_and_env():
    raw = """[
      {"slug": "a", "dir": "~/a", "command": "run", "login": true,
       "env": {"CI": "1", "NUM": 2}},
      {"slug": "b", "dir": "~/b", "command": "run", "env": "not-a-dict"}
    ]"""
    a, b = apps.parse_apps(raw, home=HOME)
    assert a.login is True
    assert a.env == {"CI": "1", "NUM": "2"}  # values coerced to strings
    assert b.login is False
    assert b.env is None


def test_render_plist_merges_app_env_over_base():
    d = apps.render_plist(spec(env={"CI": "1", "PATH": "/custom"}), path_env="/base")
    assert d["EnvironmentVariables"]["CI"] == "1"
    assert d["EnvironmentVariables"]["PATH"] == "/custom"  # per-app override wins
    assert "HOME" in d["EnvironmentVariables"]


def test_stop_keeps_plist_for_login_apps(monkeypatch, tmp_path):
    plist = tmp_path / "com.launchddash.app.web.plist"
    plist.write_text("x")
    monkeypatch.setattr(apps, "_run", lambda cmd: type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})())
    monkeypatch.setattr(apps, "_plist_file", lambda s: plist)
    res = apps.stop_app(spec(login=True))
    assert res["ok"] and "plist kept" in res["detail"]
    assert plist.exists()
    res = apps.stop_app(spec(login=False))
    assert res["ok"] and not plist.exists()


def test_restart_waits_for_unload_before_starting(monkeypatch):
    calls = []
    order = []
    # The dying job stays "loaded" for two polls before unloading — restart must
    # not hand off to start_app until it's gone (else "already running" wins).
    states = iter([{"loaded": True, "pid": 1}, {"loaded": True}, {"loaded": False}])
    monkeypatch.setattr(apps, "_run", lambda cmd: (calls.append(cmd), type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})())[1])
    monkeypatch.setattr(apps, "launchctl_state", lambda label: (order.append("poll"), next(states))[1])
    monkeypatch.setattr(apps, "start_app", lambda s: (order.append("start"), {"ok": True, "detail": "started"})[1])
    monkeypatch.setattr(apps.time, "sleep", lambda s: None)
    res = apps.restart_app(spec())
    assert res["ok"]
    assert calls[0][:2] == ["launchctl", "bootout"]
    assert order == ["poll", "poll", "poll", "start"]  # start only after unload
