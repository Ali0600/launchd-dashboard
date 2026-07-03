"""Pure-logic tests for job annotations — fixtures only."""

from app import annotations


def test_parse_valid_annotations():
    raw = '{"com.x.job": {"purpose": "backups", "repo": "~/x", "note": "runs nightly"}}'
    out = annotations.parse_annotations(raw)
    assert out["com.x.job"] == {"purpose": "backups", "repo": "~/x", "note": "runs nightly"}


def test_parse_skips_invalid_keeps_valid():
    raw = """{
      "com.good": {"purpose": "ok", "extraneous": "dropped"},
      "com.bad": "not-an-object",
      "com.empty": {"purpose": "  "}
    }"""
    out = annotations.parse_annotations(raw)
    assert list(out) == ["com.good"]
    assert out["com.good"] == {"purpose": "ok"}  # unknown fields dropped


def test_parse_garbage_is_empty_not_fatal():
    assert annotations.parse_annotations("{oops") == {}
    assert annotations.parse_annotations('["a","list"]') == {}


def test_load_missing_file(tmp_path):
    assert annotations.load_annotations(tmp_path / "nope.json") == {}
    f = tmp_path / "labels.json"
    f.write_text('{"com.a": {"purpose": "p"}}')
    assert annotations.load_annotations(f) == {"com.a": {"purpose": "p"}}
