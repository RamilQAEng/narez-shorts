"""Streamer projects: where the game and the webcam sit in the source frame.

A project is a small JSON file in ``projects/`` (not swept like output/ and uploads/). A job gets
``PROJECT_ID`` in its env and ``main.py`` renders every clip with the fixed two-box layout of
project_layout.py instead of the automatic reframe. Same shape as brand_banner.py.
"""
import json
import math
import os
import re
import uuid

import brand_banner

PROJECT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects")
MAX_PROJECTS = 20
MAX_NAME = 40
MIN_SIDE = 0.05  # of the source frame
_ID = re.compile(r"[0-9a-f]{32}")


class ProjectError(ValueError):
    """Bad project body (the API turns it into a 400)."""


def _path(project_id):
    return os.path.join(PROJECT_DIR, f"{project_id}.json")


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ProjectError("box values must be numbers")
    return float(v)


def clean_box(box, label="box"):
    """{x, y, w, h} as floats, inside the frame and at least MIN_SIDE wide and tall."""
    if not isinstance(box, dict):
        raise ProjectError(f"{label} must be an object with x, y, w, h")
    x, y, w, h = (_num(box.get(k)) for k in ("x", "y", "w", "h"))
    if x < 0 or y < 0 or w < MIN_SIDE or h < MIN_SIDE or x + w > 1 + 1e-6 or y + h > 1 + 1e-6:
        raise ProjectError(f"{label} must lie inside the frame and be at least {MIN_SIDE} wide and tall")
    return {"x": x, "y": y, "w": w, "h": h}


def clean(body):
    """The stored fields of a request body, validated. Raises ProjectError."""
    if not isinstance(body, dict):
        raise ProjectError("body must be a JSON object")
    name = body.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > MAX_NAME:
        raise ProjectError(f"name is required, at most {MAX_NAME} characters")
    banner_id = body.get("banner_id") or None
    if banner_id is not None and not brand_banner.get(banner_id):
        raise ProjectError("unknown banner_id")
    position = body.get("banner_position") or "top"
    if position not in brand_banner.POSITIONS:
        raise ProjectError("banner_position must be top or bottom")
    return {"name": name.strip(), "game": clean_box(body.get("game"), "game"),
            "cam": clean_box(body.get("cam"), "cam"),
            "banner_id": banner_id, "banner_position": position}


def get(project_id):
    """The project dict, or None. ``project_id`` is user input."""
    if not isinstance(project_id, str) or not _ID.fullmatch(project_id):
        return None
    try:
        with open(_path(project_id), encoding="utf-8") as f:
            data = json.load(f)
        return {**clean(data), "id": project_id}  # a hand-edited file is re-validated, not trusted
    except (OSError, ValueError):
        return None


def list_all():
    names = sorted(os.listdir(PROJECT_DIR)) if os.path.isdir(PROJECT_DIR) else []
    return [p for p in (get(n[:-5]) for n in names if n.endswith(".json")) if p]


def _write(project_id, data):
    os.makedirs(PROJECT_DIR, exist_ok=True)
    with open(_path(project_id), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return {**data, "id": project_id}


def create(body):
    data = clean(body)
    if len(list_all()) >= MAX_PROJECTS:
        raise ProjectError(f"At most {MAX_PROJECTS} projects; delete one first")
    return _write(uuid.uuid4().hex, data)


def update(project_id, body):
    """The updated project, or None for an unknown id."""
    if not get(project_id):
        return None
    return _write(project_id, clean(body))


def delete(project_id):
    if not get(project_id):
        return False
    os.remove(_path(project_id))
    return True
