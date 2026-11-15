"""Brand banner (image, GIF or video) laid over a finished clip.

Separate from the render pipeline on purpose: the library lives in ``brand/``
(not swept like output/ and uploads/), a job gets ``BANNER_ID`` in its env, and
``main.py`` calls ``apply_to_clip`` last, on the file that is about to be served.
"""
import json
import os
import re
import subprocess
import time
import uuid

BANNER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brand")
MAX_BYTES = 30 * 1024 * 1024
MAX_BANNERS = 20
MAX_SIDE = 4096
MAX_SECONDS = 60
FORMATS = {"png_pipe", "jpeg_pipe", "webp_pipe", "gif", "mov", "mp4", "matroska", "webm"}
KINDS = {".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image",
         ".gif": "gif", ".mp4": "video", ".webm": "video", ".mov": "video"}
POSITIONS = ("top", "bottom")
WIDTH = 0.9    # of the clip width
MARGIN = 0.04  # of the clip height, from the chosen edge
MAX_HEIGHT = 0.25  # of the clip height: a tall banner is shrunk, never cropped or pushed off-frame
_ID = re.compile(r"[0-9a-f]{32}")


class BannerError(ValueError):
    """Bad banner upload or option (the API turns it into a 400)."""


def _meta_path(banner_id):
    return os.path.join(BANNER_DIR, f"{banner_id}.json")


def get(banner_id):
    """Meta dict (with ``path``) of a stored banner, or None. ``banner_id`` is user input."""
    if not isinstance(banner_id, str) or not _ID.fullmatch(banner_id):
        return None
    try:
        with open(_meta_path(banner_id), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None
    path = os.path.join(BANNER_DIR, banner_id + meta.get("ext", ""))
    return {**meta, "id": banner_id, "path": path} if os.path.isfile(path) else None


def list_all():
    out = [get(n[:-5]) for n in sorted(os.listdir(BANNER_DIR)) if n.endswith(".json")] \
        if os.path.isdir(BANNER_DIR) else []
    return [{k: b[k] for k in ("id", "name", "kind", "position")} for b in out if b]


def _write_capped(stream, path):
    """Copy the upload, stopping at MAX_BYTES instead of after the whole body."""
    written = 0
    with open(path, "wb") as out:
        while chunk := stream.read(1024 * 1024):
            written += len(chunk)
            if written > MAX_BYTES:
                raise BannerError(f"Banner is larger than {MAX_BYTES // (1024 * 1024)} MB")
            out.write(chunk)


def _probe(path):
    """(container format names, width, height, seconds) by ffprobe; empty when unreadable."""
    done = subprocess.run(
        ["ffprobe", "-v", "error", "-protocol_whitelist", "file", "-select_streams", "v:0",
         "-show_entries", "format=format_name,duration:stream=width,height", "-of", "json", path],
        capture_output=True, text=True, timeout=30)
    try:
        info = json.loads(done.stdout)
        stream = info["streams"][0]
        fmt = info["format"]
        return fmt["format_name"], int(stream["width"]), int(stream["height"]), \
            float(fmt.get("duration") or 0)
    except (ValueError, KeyError, IndexError, TypeError):
        return None


def _check_media(path):
    """Reject anything that is not a real, small image/GIF/video (ffmpeg sniffs content, not names)."""
    info = _probe(path)
    if not info or not set(info[0].split(",")) & FORMATS:
        raise BannerError("Not a valid image, GIF or video file")
    _, w, h, seconds = info
    if w > MAX_SIDE or h > MAX_SIDE or seconds > MAX_SECONDS:
        raise BannerError(f"Banner must be at most {MAX_SIDE}px and {MAX_SECONDS}s")


def save(stream, filename, position="top"):
    """Store an uploaded banner; returns its public meta. Raises BannerError."""
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in KINDS:
        raise BannerError(f"Banner must be one of: {', '.join(sorted(KINDS))}")
    if position not in POSITIONS:
        raise BannerError("position must be top or bottom")
    os.makedirs(BANNER_DIR, exist_ok=True)
    if len(list_all()) >= MAX_BANNERS:
        raise BannerError(f"At most {MAX_BANNERS} banners; delete one first")
    banner_id = uuid.uuid4().hex
    path = os.path.join(BANNER_DIR, banner_id + ext)
    try:
        _write_capped(stream, path)
        _check_media(path)
    except BaseException:
        if os.path.exists(path):
            os.remove(path)
        raise
    meta = {"name": os.path.basename(filename)[:80], "ext": ext, "kind": KINDS[ext],
            "position": position}
    with open(_meta_path(banner_id), "w", encoding="utf-8") as f:
        json.dump(meta, f)
    return {"id": banner_id, **{k: meta[k] for k in ("name", "kind", "position")}}


def delete(banner_id):
    banner = get(banner_id)
    if not banner:
        return False
    os.remove(banner["path"])
    os.remove(_meta_path(banner_id))
    return True


def _clip_size(clip):
    """(width, height) of the clip's video stream by ffprobe."""
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "csv=p=0", clip],
                           capture_output=True, text=True, timeout=30)
    w, h = probe.stdout.strip().split(",")[:2]
    return int(w), int(h)


def _filter(position, size):
    """Banner at WIDTH of the clip, at most MAX_HEIGHT of its height, MARGIN from the chosen edge."""
    clip_w, clip_h = size
    y = f"main_h*{MARGIN}" if position == "top" else f"main_h-overlay_h-main_h*{MARGIN}"
    w, h = int(clip_w * WIDTH) // 2 * 2, int(clip_h * MAX_HEIGHT) // 2 * 2
    return (f"[1:v]format=rgba,scale={w}:{h}:force_original_aspect_ratio=decrease"
            f":force_divisible_by=2[b];"
            f"[0:v][b]overlay=x=(main_w-overlay_w)/2:y={y}:shortest=1:format=auto,format=yuv420p[v]")


def _command(clip, banner, out):
    # Loop the banner for the whole clip: stills with -loop, GIFs with -ignore_loop 0, videos with -stream_loop.
    loop = {"image": ["-loop", "1"], "gif": ["-ignore_loop", "0"],
            "video": ["-stream_loop", "-1"]}[banner["kind"]]
    from ffmpeg_utils import video_encode_args, QUALITY
    return ["ffmpeg", "-y", "-i", clip, *loop, "-protocol_whitelist", "file", "-i", banner["path"],
            "-filter_complex", _filter(banner["position"], _clip_size(clip)), "-map", "[v]",
            "-map", "0:a?", "-c:a", "copy", *video_encode_args(QUALITY),
            "-movflags", "+faststart", out]


_DERIVED = re.compile(r"(subtitled|hooked)_(\d+)_(.+)")


def _branded_name(name):
    """Output name in the scheme app._canonical_clip_file, the strip helpers and has_captions
    already walk back (``subtitled_<ts>_<clean>``): the newest file wins, so restores, downloads
    and the R2 archive keep the banner without teaching app.py a new prefix."""
    m = _DERIVED.fullmatch(name)
    if m:
        return f"{m[1]}_{int(m[2]) + 1}_{m[3]}"
    return f"hooked_{int(time.time())}_{name}"  # clean file served as is (no captions on this clip)


def apply_to_clip(clip, banner_id, position=None):
    """Path of the clip with the banner on it, or ``clip`` itself when there is
    no banner or the overlay failed (a banner problem never costs the clip).
    ``position`` (top/bottom) chosen for this job wins over the one stored with the banner."""
    banner = get(banner_id) if banner_id else None
    if not banner:
        return clip
    if position in POSITIONS:
        banner = {**banner, "position": position}
    d, name = os.path.split(clip)
    out = os.path.join(d, _branded_name(name))
    try:
        done = subprocess.run(_command(clip, banner, out), stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, text=True, errors="replace", timeout=900)
        failure = None if done.returncode == 0 and os.path.isfile(out) else done.stderr[-300:]
    except (subprocess.TimeoutExpired, ValueError, OSError) as e:  # ffprobe unreadable, ffmpeg missing...
        failure = f"{type(e).__name__}: {e}"
    if failure:
        print(f"   ⚠️ Banner not applied to {name}: {failure}")
        return clip
    return out
