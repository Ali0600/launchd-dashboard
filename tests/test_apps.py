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
