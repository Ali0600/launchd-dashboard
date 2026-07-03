"""Per-job annotations: why each agent exists, owned by which repo.

labels.json (gitignored — machine-specific, like apps.json) maps a launchd label to
{purpose, repo, note}. The dashboard renders purpose in the row and the note as a
tooltip, turning the agent list into documentation for the growing pile of jobs.
"""

from __future__ import annotations

import json
from pathlib import Path

from .apps import warn

LABELS_PATH = Path(__file__).resolve().parent.parent / "labels.json"

_FIELDS = ("purpose", "repo", "note")


def parse_annotations(raw: str) -> dict[str, dict]:
    """Parse labels.json content: {label: {purpose?, repo?, note?}}. Invalid entries
    are skipped with a warning — one typo shouldn't drop everyone's annotations."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        warn(f"labels.json is not valid JSON: {exc}")
        return {}
    if not isinstance(data, dict):
        warn("labels.json must be a JSON object of label -> annotation")
        return {}
    out: dict[str, dict] = {}
    for label, entry in data.items():
        if not isinstance(entry, dict):
            warn(f"labels.json: skipping {label!r} — annotation must be an object")
            continue
        cleaned = {k: str(v) for k, v in entry.items() if k in _FIELDS and str(v).strip()}
        if cleaned:
            out[str(label)] = cleaned
    return out


def load_annotations(path: Path = LABELS_PATH) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        return parse_annotations(path.read_text())
    except OSError as exc:
        warn(f"could not read {path}: {exc}")
        return {}
