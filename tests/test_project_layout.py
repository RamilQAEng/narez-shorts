import pytest

import project_layout as pl
import projects as pr

GAME = {"x": 0, "y": 0, "w": 0.77, "h": 1}
CAM = {"x": 0, "y": 0.5, "w": 0.225, "h": 0.255}


@pytest.fixture(autouse=True)
def project_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "PROJECT_DIR", str(tmp_path / "projects"))


def _body(**kw):
    return {"name": "Stream", "game": GAME, "cam": CAM, **kw}


def test_crud_roundtrip():
    made = pr.create(_body())
    assert pr.get(made["id"])["cam"]["y"] == 0.5 and pr.list_all() == [pr.get(made["id"])]
    assert pr.update(made["id"], _body(name="New"))["name"] == "New"
    assert pr.update("0" * 32, _body()) is None
    assert pr.delete(made["id"]) and not pr.delete(made["id"]) and pr.get("../x") is None


@pytest.mark.parametrize("bad", [
    {"name": ""}, {"name": "x" * 41}, {"game": {**GAME, "w": 0.04}}, {"cam": {**CAM, "x": 0.9}},
    {"cam": {**CAM, "y": -0.1}}, {"game": {**GAME, "w": True}}, {"cam": "box"},
    {"banner_position": "left"}, {"banner_id": "0" * 32}])
def test_bad_body_rejected(bad):
    with pytest.raises(pr.ProjectError):
        pr.create(_body(**bad))


def test_project_cap(monkeypatch):
    monkeypatch.setattr(pr, "MAX_PROJECTS", 1)
    pr.create(_body())
    with pytest.raises(pr.ProjectError):
        pr.create(_body())


def test_zoom_moments_clamped_merged_and_sanitised():
    raw = [[110, 112], [100, 105], [104, 107], ["a", 3], [1, 2, 3][:2], [float("nan"), 5], 7, [119, 130]]
    assert pl.zoom_moments(raw, 100, 120) == [(0.0, 7.0), (10.0, 12.0), (19.0, 20.0)]
    assert pl.zoom_moments("junk", 0, 10) == []


def test_graph_switches_looks_with_numbers_only():
    graph, bands = pl.build(1920, 1080, pr.clean(_body()), [(2.0, 4.5)])
    assert "split=5" in graph and "enable='not(between(t,2.000,4.500))'" in graph
    assert "enable='between(t,2.000,4.500)'" in graph and graph.endswith("setsar=1[v]")
    assert bands["base"][0] == bands["zoom"][0] == 0.0  # the webcam band is the top of the frame
    assert bands["base"][1] < bands["zoom"][1] < 1      # and reaches lower in the zoom look
    plain, _ = pl.build(1920, 1080, pr.clean(_body()), [])
    assert "enable" not in plain and "split=3" in plain


def test_ranges_cover_clip():
    r = pl.ranges(10.0, [(2.0, 4.0)], {"base": (0.1, 0.5), "zoom": (0.0, 0.2)})
    assert [(a, b) for a, b, *_ in r] == [(0.0, 2.0), (2.0, 4.0), (4.0, 10.0)]
