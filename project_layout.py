"""Fixed two-box layout for streamer sources: the webcam above, the game below.

The project (projects.py) says where the two boxes sit; nothing is detected. One ffmpeg graph draws
two looks over one blurred backdrop and switches between them over time: BASE (cam about 40% of the
height on top, game big below) and ZOOM (cam about 75%, a small game strip below) at the clip's zoom moments.
Every crop is scaled to fit its band, never stretched. Only numbers go into the graph: the project
is validated (projects.clean_box) and the zoom moments are coerced to floats.
"""
import math
import os
import subprocess

import layout_ranges
from ffmpeg_utils import QUALITY, audio_encode_args, blurred_backdrop, video_encode_args

OUT_W, OUT_H = 1080, 1920
BASE_CAM = 0.40
ZOOM_CAM = 0.75
MIN_ZOOM = 0.3   # seconds: a shorter flash is not a zoom
MAX_ZOOMS = 20
MIN_KEEP = 0.7   # a cam crop may lose up to 30% of its width to fill a band, never more
CAPTION_FONT_SIZE = 26  # smaller than the automatic 30 and lower than SAFE_MARGIN_V: the captions sit on the game
CAPTION_MARGIN_V = 24


def zoom_moments(raw, clip_start, clip_end):
    """Sorted, merged (start, end) pairs in CLIP seconds from absolute source seconds; bad entries dropped."""
    out = []
    for item in (raw if isinstance(raw, list) else [])[:MAX_ZOOMS * 4]:
        try:
            a, b = (float(v) for v in item)
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(a) and math.isfinite(b)):
            continue
        a, b = max(a, clip_start) - clip_start, min(b, clip_end) - clip_start
        if b - a >= MIN_ZOOM:
            out.append([float(a), float(b)])
    merged = []
    for a, b in sorted(out):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [tuple(m) for m in merged[:MAX_ZOOMS]]


def _even(v):
    return max(2, int(v) // 2 * 2)


def _crop(box, src_w, src_h):
    return (_even(box["w"] * src_w), _even(box["h"] * src_h),
            int(box["x"] * src_w) // 2 * 2, int(box["y"] * src_h) // 2 * 2)


def _fit(crop_w, crop_h, band_h):
    """Size of a crop scaled to fit OUT_W x band_h, aspect kept."""
    k = min(OUT_W / crop_w, band_h / crop_h)
    return _even(crop_w * k), _even(crop_h * k)


def _cam_look(cam, target_h):
    """(band height, crop, scaled w, scaled h) for the cam in a band of about target_h.
    A cam wider than the band is cropped from the sides (up to 30%) so it can fill it; a taller
    one is fitted and the band's sides show the blur. Nothing is stretched."""
    w, h, x, y = cam
    natural = OUT_W * h / w
    if natural >= target_h:
        return (target_h, cam) + _fit(w, h, target_h)
    band = _even(min(target_h, natural / MIN_KEEP))
    keep_w = _even(w * natural / band)
    crop = (keep_w, h, x + (w - keep_w) // 2 // 2 * 2, y)
    return (band, crop) + _fit(keep_w, h, band)


def build(src_w, src_h, project, zooms):
    """(filter_complex, bands) for a clip; bands[look] is the (top, bottom) frame fraction the webcam
    band fills (the top of the frame), which the hook placement (layout_ranges ``content``) keeps the hook off."""
    game, cam = _crop(project["game"], src_w, src_h), _crop(project["cam"], src_w, src_h)
    looks = {"base": round(OUT_H * BASE_CAM) // 2 * 2, "zoom": round(OUT_H * ZOOM_CAM) // 2 * 2}
    names = ["base", "zoom"] if zooms else ["base"]
    parts = [f"[0:v]split={1 + 2 * len(names)}[bg]" + "".join(f"[g_{n}][c_{n}]" for n in names),
             f"[bg]{blurred_backdrop(OUT_W, OUT_H, 14)}[v0]"]
    inzoom = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in zooms)
    bands, last = {}, "v0"
    for n in names:
        cam_h, ccrop, cw, ch = _cam_look(cam, looks[n])
        game_h = OUT_H - cam_h
        gw, gh = _fit(game[0], game[1], game_h)
        cy, gy = (cam_h - ch) // 2, cam_h + (game_h - gh) // 2
        bands[n] = (0.0, cam_h / OUT_H)  # the hook goes around this band, so off the webcam
        when = f"not({inzoom})" if n == "base" else inzoom
        enable = f":enable='{when}'" if zooms else ""
        parts.append(f"[g_{n}]crop={game[0]}:{game[1]}:{game[2]}:{game[3]},scale={gw}:{gh}[G{n}]")
        parts.append(f"[c_{n}]crop={ccrop[0]}:{ccrop[1]}:{ccrop[2]}:{ccrop[3]},scale={cw}:{ch}[C{n}]")
        parts.append(f"[{last}][G{n}]overlay=x={(OUT_W - gw) // 2}:y={gy}{enable}[a{n}]")
        tail = ",setsar=1[v]" if n == names[-1] else f"[b{n}]"
        parts.append(f"[a{n}][C{n}]overlay=x={(OUT_W - cw) // 2}:y={cy}{enable}{tail}")
        last = f"b{n}"
    return ";".join(parts), bands


def ranges(duration, zooms, bands):
    """layout_ranges tuples covering the clip: base stretches and zoom stretches."""
    out, t = [], 0.0
    for a, b in zooms:
        if a > t:
            out.append((t, a, "project", bands["base"]))
        out.append((a, b, "project", bands["zoom"]))
        t = b
    if duration > t:
        out.append((t, duration, "project", bands["base"]))
    return out


def _probe(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height:format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True, timeout=30)
    (w, h), d = p.stdout.split()[0].split(",")[:2], p.stdout.split()[1]
    return int(w), int(h), float(d)


def render(clip_in, clip_out, project, raw_zooms=None, clip_start=0.0, clip_end=0.0):
    """Render a cut clip with the project's layout; True on success, False so the caller can
    fall back to the automatic reframe. ``raw_zooms``: absolute source seconds."""
    try:
        src_w, src_h, duration = _probe(clip_in)
        zooms = zoom_moments(raw_zooms, clip_start, clip_end)
        graph, bands = build(src_w, src_h, project, zooms)
        done = subprocess.run(
            ["ffmpeg", "-y", "-i", clip_in, "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
             *video_encode_args(QUALITY), *audio_encode_args(), "-movflags", "+faststart", clip_out],
            capture_output=True, text=True, errors="replace", timeout=1800)
    except (subprocess.TimeoutExpired, ValueError, IndexError, OSError) as e:
        print(f"   ⚠️ Project layout failed ({type(e).__name__}: {e})")
        return False
    if done.returncode != 0 or not os.path.isfile(clip_out):
        print(f"   ⚠️ Project layout failed: {done.stderr[-300:]}")
        return False
    # not "wide": that name makes hook_grounding rewrite the hook from frames
    layout_ranges.write(clip_out, ranges(duration, zooms, bands))
    return True
