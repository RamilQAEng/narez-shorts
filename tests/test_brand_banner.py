import io
import os
import shutil
import subprocess
import sys
import types

import pytest

import brand_banner as bb


@pytest.fixture(autouse=True)
def banner_dir(tmp_path, monkeypatch):
    d = str(tmp_path / "brand")
    monkeypatch.setattr(bb, "BANNER_DIR", d)
    monkeypatch.setattr(bb, "_probe", lambda path: ("png_pipe", 800, 200, 0.0))
    return d


def _save(name="b.png", position="top", data=b"x"):
    return bb.save(io.BytesIO(data), name, position)


@pytest.mark.parametrize("name,kind", [("a.png", "image"), ("a.GIF", "gif"), ("a.mp4", "video")])
def test_save_stores_meta_and_file(banner_dir, name, kind):
    meta = _save(name, "bottom")
    assert meta["kind"] == kind and meta["position"] == "bottom"
    got = bb.get(meta["id"])
    assert got["path"].startswith(banner_dir) and os.path.isfile(got["path"])
    assert os.path.isfile(os.path.join(banner_dir, meta["id"] + ".json"))


@pytest.mark.parametrize("name,position", [("a.exe", "top"), ("noext", "top"), ("a.png", "middle")])
def test_save_rejects_bad_input(banner_dir, name, position):
    with pytest.raises(bb.BannerError):
        _save(name, position)
    assert not os.path.isdir(banner_dir) or os.listdir(banner_dir) == []


def test_save_rejects_oversize_and_leaves_nothing(banner_dir, monkeypatch):
    monkeypatch.setattr(bb, "MAX_BYTES", 10)
    with pytest.raises(bb.BannerError):
        _save(data=b"x" * 11)
    assert os.listdir(banner_dir) == []


@pytest.mark.parametrize("bad", ["../../etc/passwd", "zz" * 16, "", None, 5, "A" * 32, "0" * 31])
def test_get_rejects_bad_ids(bad):
    assert bb.get(bad) is None


def test_get_unknown_id():
    assert bb.get("0" * 32) is None


def test_list_all_and_delete_round_trip(banner_dir):
    assert bb.list_all() == []
    meta = _save()
    assert bb.list_all() == [meta]
    assert bb.delete(meta["id"]) is True
    assert bb.list_all() == [] and os.listdir(banner_dir) == []
    assert bb.delete(meta["id"]) is False


def test_filter_top_and_bottom_margin():
    top = bb._filter("top", (1080, 1920))
    bottom = bb._filter("bottom", (1080, 1920))
    assert f"y=main_h*{bb.MARGIN}:" in top
    assert f"y=main_h-overlay_h-main_h*{bb.MARGIN}:" in bottom


@pytest.mark.parametrize("clip_width", [1080, 1081, 721])
def test_filter_width_is_even(clip_width):
    w = int(bb._filter("top", (clip_width, 1920)).split("scale=")[1].split(":")[0])
    assert w % 2 == 0 and w <= clip_width * bb.WIDTH


@pytest.mark.parametrize("kind,flags", [("image", ["-loop", "1"]), ("gif", ["-ignore_loop", "0"]),
                                        ("video", ["-stream_loop", "-1"])])
def test_command_loop_flags(monkeypatch, kind, flags):
    fake = types.ModuleType("ffmpeg_utils")
    fake.video_encode_args = lambda q: ["-c:v", "fake"]
    fake.QUALITY = "q"
    monkeypatch.setitem(sys.modules, "ffmpeg_utils", fake)
    monkeypatch.setattr(bb, "_clip_size", lambda clip: (1080, 1920))
    cmd = bb._command("in.mp4", {"kind": kind, "path": "b.x", "position": "top"}, "out.mp4")
    i = cmd.index(flags[0])
    assert cmd[i:i + 2] == flags
    assert cmd[i + 2:i + 6] == ["-protocol_whitelist", "file", "-i", "b.x"]  # all before the banner input, not the clip
    assert cmd[-1] == "out.mp4"


@pytest.mark.parametrize("banner_id", [None, "", "0" * 32])
def test_apply_returns_clip_without_banner(banner_id):
    assert bb.apply_to_clip("clip.mp4", banner_id) == "clip.mp4"


def _with_banner(monkeypatch, tmp_path, run):
    meta = _save()
    monkeypatch.setattr(bb, "_command", lambda clip, banner, out: ["ffmpeg"])
    monkeypatch.setattr(bb.subprocess, "run", run)
    clip = str(tmp_path / "clip.mp4")
    return clip, bb.apply_to_clip(clip, meta["id"])


def test_apply_falls_back_on_nonzero_exit(monkeypatch, tmp_path):
    run = lambda *a, **k: types.SimpleNamespace(returncode=1, stderr="boom")
    clip, result = _with_banner(monkeypatch, tmp_path, run)
    assert result == clip


def test_apply_falls_back_on_timeout(monkeypatch, tmp_path):
    def run(*a, **k):
        raise subprocess.TimeoutExpired("ffmpeg", 1)
    clip, result = _with_banner(monkeypatch, tmp_path, run)
    assert result == clip


def test_apply_returns_branded_path_on_success(monkeypatch, tmp_path):
    def run(cmd, **k):
        return types.SimpleNamespace(returncode=0, stderr="")
    meta = _save()
    monkeypatch.setattr(bb, "_command", lambda clip, banner, out: (open(out, "wb").close(), ["ffmpeg"])[1])
    monkeypatch.setattr(bb.subprocess, "run", run)
    clip = str(tmp_path / "subtitled_5_clip.mp4")
    assert bb.apply_to_clip(clip, meta["id"]) == str(tmp_path / "subtitled_6_clip.mp4")


@pytest.mark.parametrize("probed", [None, ("hls", 800, 200, 5.0), ("png_pipe", 20000, 200, 0.0),
                                    ("mp4", 800, 200, 600.0)])
def test_save_rejects_non_media_and_oversized_media(banner_dir, monkeypatch, probed):
    """A playlist named x.mp4, a 20000px PNG and a 10-minute video never reach ffmpeg's overlay."""
    monkeypatch.setattr(bb, "_probe", lambda path: probed)
    with pytest.raises(bb.BannerError):
        _save("x.mp4")
    assert os.listdir(banner_dir) == []


def test_save_caps_the_number_of_banners(banner_dir, monkeypatch):
    monkeypatch.setattr(bb, "MAX_BANNERS", 2)
    _save(), _save()
    with pytest.raises(bb.BannerError):
        _save()
    assert len(bb.list_all()) == 2


def test_banner_input_is_limited_to_local_files(monkeypatch):
    fake = types.ModuleType("ffmpeg_utils")
    fake.video_encode_args = lambda q: ["-c:v", "fake"]
    fake.QUALITY = "q"
    monkeypatch.setitem(sys.modules, "ffmpeg_utils", fake)
    monkeypatch.setattr(bb, "_clip_size", lambda clip: (1080, 1920))
    cmd = bb._command("clip.mp4", {"kind": "image", "path": "brand/x.png", "position": "top"},
                      "out.mp4")
    assert cmd[cmd.index("-protocol_whitelist") + 1] == "file"
    assert cmd.index("-protocol_whitelist") < cmd.index("brand/x.png")


def test_filter_caps_banner_height_and_keeps_aspect():
    f = bb._filter("top", (1080, 1920))
    assert f"scale=972:480:force_original_aspect_ratio=decrease" in f  # 90% wide, at most 25% tall


@pytest.mark.parametrize("name,expected", [
    ("subtitled_100_X_clip_1.mp4", "subtitled_101_X_clip_1.mp4"),
    ("hooked_7_X_clip_1.mp4", "hooked_8_X_clip_1.mp4"),
])
def test_branded_file_keeps_the_names_app_py_walks_back(name, expected):
    """_canonical_clip_file / has_captions / the strip helpers only know these prefixes."""
    assert bb._branded_name(name) == expected
    assert bb._branded_name("X_clip_1.mp4").startswith("hooked_")


def test_apply_returns_the_clip_when_ffprobe_cannot_read_it(monkeypatch):
    meta = _save()

    def boom(clip):
        raise ValueError("not enough values to unpack")

    monkeypatch.setattr(bb, "_clip_size", boom)
    fake = types.ModuleType("ffmpeg_utils")
    fake.video_encode_args = lambda q: []
    fake.QUALITY = "q"
    monkeypatch.setitem(sys.modules, "ffmpeg_utils", fake)
    assert bb.apply_to_clip("subtitled_1_X_clip_1.mp4", meta["id"]) == "subtitled_1_X_clip_1.mp4"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
@pytest.mark.parametrize("with_audio", [True, False])
def test_overlay_on_a_real_clip(tmp_path, monkeypatch, with_audio):
    """End to end with the real ffmpeg: banner stored, probed, overlaid, same length, audio kept."""
    run = lambda *a: subprocess.run(["ffmpeg", "-v", "error", "-y", *a], check=True)
    png = tmp_path / "b.png"
    run("-f", "lavfi", "-i", "color=c=red:s=800x200", "-frames:v", "1", str(png))
    clip = tmp_path / "subtitled_1_X_clip_1.mp4"
    audio = ["-f", "lavfi", "-i", "sine=d=2"] if with_audio else []
    run("-f", "lavfi", "-i", "color=c=blue:s=540x960:r=25:d=2", *audio, "-pix_fmt", "yuv420p", str(clip))
    monkeypatch.setattr(bb, "BANNER_DIR", str(tmp_path / "brand"))
    with open(png, "rb") as fh:
        meta = bb.save(fh, "b.png", "bottom")
    out = bb.apply_to_clip(str(clip), meta["id"])
    assert os.path.basename(out) == "subtitled_2_X_clip_1.mp4"
    probe = lambda *a: subprocess.run(["ffprobe", "-v", "error", *a, out], capture_output=True, text=True).stdout
    assert abs(float(probe("-show_entries", "format=duration", "-of", "csv=p=0")) - 2.0) < 0.3
    assert ("audio" in probe("-show_entries", "stream=codec_type", "-of", "csv=p=0")) == with_audio
