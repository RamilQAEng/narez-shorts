import os
import llm_backend
import re
import sys
import uuid
import subprocess
import threading
import json
import shutil
import glob
import httpx
import time
import zipfile
import itertools
import asyncio
import signal
import socket
from dotenv import load_dotenv
from typing import Any, Dict, Optional, List
from contextlib import asynccontextmanager, suppress
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel
import recut
import layout_ranges
import media_auth
import yt_account
from jsonfile import write_json_atomic

load_dotenv()

# Constants
UPLOAD_DIR = "uploads"
OUTPUT_DIR = "output"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Configuration
# Default to 1 if not set, but user can set higher for powerful servers
MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "5"))
MAX_FILE_SIZE_MB = 2048  # 2GB limit

# Ceiling for the working directory once it lives on a persistent volume: the
# age-based sweep alone can't stop a burst of long videos from filling the disk.
# 0 disables the cap.
OUTPUT_MAX_GB = int(os.environ.get("OUTPUT_MAX_GB", "25"))
# Same idea for source uploads, which are the biggest single files on disk.
UPLOADS_MAX_GB = int(os.environ.get("UPLOADS_MAX_GB", "15"))
# Pre-flight quality gate: warn before processing a YouTube source below this
# height (0 disables). Only applies to URLs; uploads are whatever the user gave.
QUALITY_GATE_MIN_HEIGHT = int(os.environ.get("QUALITY_GATE_MIN_HEIGHT", "720"))
# Reject sources shorter than this before starting (0 disables). A 24s YouTube
# Short cannot yield 15-60s clips: Gemini returns nothing, the job burns
# managed minutes and dies with "no usable clips" (prod 20-ago: 3 of 5 recent
# failures were exactly this, one user retrying the same 24s video).
MIN_SOURCE_SECONDS = int(os.environ.get("MIN_SOURCE_SECONDS", "45"))
QUALITY_PROBE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quality_probe.py")

# Server credentials the pipeline subprocesses (main.py, quality_probe.py) never
# read: main.py and everything it imports use GEMINI_API_KEY / LLM_* / the
# proxies / YOUTUBE_COOKIES and nothing from cloud/. A child that runs yt-dlp
# and ffmpeg on user-supplied URLs and files has no business holding the
# Stripe key, the JWT signing secret or the database password; one exploit
# in that stack would otherwise hand over all of them.
CHILD_ENV_DENYLIST = frozenset({
    "DATABASE_URL", "POSTGRES_PASSWORD", "JWT_SECRET",
    "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET",
    "SMTP_USER", "SMTP_PASSWORD",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "GOOGLE_CLIENT_SECRET",
    "AGENTLEDGER_API_KEY",
    "MANAGED_UPLOAD_POST_API_KEY", "MANAGED_GEMINI_API_KEY", "UPLOAD_POST_API_KEY",
    "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    "OPENPANEL_CLIENT_SECRET", "ELEVENLABS_API_KEY",
    "REMOTE_WORKER_TOKEN",
})


def child_env(base=None):
    """A copy of the environment for a pipeline subprocess, minus the server
    secrets in CHILD_ENV_DENYLIST. Callers add GEMINI_API_KEY themselves."""
    env = dict(os.environ if base is None else base)
    for name in CHILD_ENV_DENYLIST:
        env.pop(name, None)
    return env


DISABLE_YOUTUBE_URL = os.environ.get("DISABLE_YOUTUBE_URL", "false").lower() in ("1", "true", "yes")

# Every log line in this module is emoji-prefixed, and a Windows console is
# cp1252 by default. _recover_jobs_from_disk() prints one during startup, so
# without this the server dies before it ever listens:
#
#   UnicodeEncodeError: 'charmap' codec can't encode characters in position 0-1
#   ERROR:    Application startup failed. Exiting.
#
# subtitles._configure_stdio solved this for the transcription path; the server
# needs it too, and needs it before the first print.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# Self-host agent mode: main.py waits for clips.json written by the user's own agent, so cutting needs no LLM key.
AGENT_CLIPS = os.environ.get("AGENT_CLIPS") == "1"

# Job/file retention (issue #46). Self-host defaults to 24h: the 1h sweep kept
# deleting finished projects under users who never touched their env, and the
# OUTPUT_MAX_GB / UPLOADS_MAX_GB caps below already bound the disk. Cloud keeps
# the tight default because clips are archived to R2 as soon as a job finishes.
JOB_RETENTION_SECONDS = int(
    os.environ.get("JOB_RETENTION_SECONDS", "86400")
)
# The retained download of a URL job (--keep-original) is the one artifact that
# is a full copy of someone else's video rather than something we made, so it
# can be aged out ahead of the clips it produced. Defaults to the job clock,
# i.e. no change: dropping it earlier costs the clip editor, whose rerender,
# reframe, scenes and EDL endpoints all read that file and answer 409 once it
# is gone. Lower it only if you would rather lose in-session re-edits than
# keep the original around. Uploads are deliberately untouched: that file is
# the user's own content, which they attested to owning.
SOURCE_RETENTION_SECONDS = int(
    os.environ.get("SOURCE_RETENTION_SECONDS", str(JOB_RETENTION_SECONDS))
)
# Force full pipeline logs to the client even under billing (local debugging).
DEBUG_LOGS = os.environ.get("DEBUG_LOGS", "").lower() in ("1", "true", "yes")

# Every job queues at one priority: the heap orders them by submission time.
JOB_PRIORITY = 2


async def resolve_gemini(request: Request) -> Optional[str]:
    """The Gemini key for a request: the ``X-Gemini-Key`` header, else GEMINI_API_KEY."""
    header = request.headers.get("X-Gemini-Key")
    if header:
        return header
    return os.environ.get("GEMINI_API_KEY")


def gemini_missing_error():
    """The right 4xx when no Gemini key could be resolved.

    402 for a signed-in-but-not-entitled cloud user (needs a plan); 400 otherwise
    (BYOK header simply missing).
    """
    return HTTPException(status_code=400, detail="Missing X-Gemini-Key header")


# Application State
# PriorityQueue holds (priority, key, job_id). Lower priority dispatches first:
# pro=0, starter/creator=1, free/BYOK/anonymous/self-host=2, and the free slow
# lane 3 (cloud/queue_mode.py, off unless configured). The key orders a
# priority: the job's submission time (``submitted_at``, which travels in the
# resume manifest, so a job enqueued again after a deploy keeps its place),
# or minus that time in a free lane that serves its newest job first. With
# BILLING disabled every job enqueues at priority 2 → plain FIFO as before.
job_queue = asyncio.PriorityQueue()
_job_seq = itertools.count()  # kept for callers that reset it; keys are times now
jobs: Dict[str, Dict] = {}
# Semester to limit concurrency to MAX_CONCURRENT_JOBS
concurrency_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)


# Queue position + ETA for /api/status. ``_admitting`` holds jobs already taken
# off the queue that wait for a free slot / GPU room: they go before anything
# still queued. Durations are a rolling sample of this instance's finished jobs.
_admitting: set = set()
_recent_job_seconds: list = []
DEFAULT_JOB_SECONDS = 240


def _record_job_duration(seconds):
    if seconds and seconds > 0:
        _recent_job_seconds.append(float(seconds))
        del _recent_job_seconds[:-30]


def queue_estimate(ahead: int, running: int, slots: int, typical_seconds: float) -> int:
    """Seconds until a job with ``ahead`` jobs in front of it starts.

    With a slot free it starts right away (a few seconds of admission);
    otherwise each full round of ``slots`` jobs ahead costs about one typical
    job, since the running ones are on average half done.
    """
    slots = max(1, int(slots))
    free = max(0, slots - int(running))
    if ahead < free:
        return 15
    waiting = ahead - free + 1
    rounds = waiting / slots
    return int(max(30, round(typical_seconds * (0.5 + rounds - 1 / slots))))


# Every change this module makes to the heap bumps this, so /api/status (polled
# every 2 s by each open tab) ranks a queue of thousands once per change, not
# once per poll.
_queue_version = 0
_rank_cache = {"key": None, "rank": {}}


def _queue_changed():
    global _queue_version
    _queue_version += 1


def _queue_ranks() -> dict:
    """``{job_id: (index, priority)}`` of the queued entries in dispatch order.
    Cached on the heap's identity, version and size, and for one second at
    most (a status flip nobody announced)."""
    heap = job_queue._queue
    key = (id(job_queue), _queue_version, len(heap), int(time.monotonic()))
    if _rank_cache["key"] != key:
        rank, i = {}, 0
        for prio, _k, jid in sorted(heap):
            if jid in rank or (jobs.get(jid) or {}).get('status') != 'queued':
                continue
            rank[jid] = (i, prio)
            i += 1
        _rank_cache.update(key=key, rank=rank)
    return _rank_cache["rank"]


def _overtakable(priority) -> bool:
    """Whether jobs that arrive later can still go in front of this one beyond
    paid plans: a free lane serving its newest first, or the slow lane (the
    normal lane goes before it). Its ETA is then a minimum, not an estimate."""
    return False


def queue_snapshot(job_id):
    """``{position, ahead, eta_seconds, eta_is_minimum}`` for a queued job,
    else None. Position is in the real dispatch order (lanes and newest-first
    included); ``eta_is_minimum`` says later arrivals can still pass it."""
    job = jobs.get(job_id) or {}
    if job.get('status') != 'queued':
        return None
    try:
        ranks = _queue_ranks()
    except Exception:
        ranks = {}
    prio = None
    if job_id in _admitting:
        ahead = len([j for j in _admitting if j != job_id])
    elif job_id in ranks:
        idx, prio = ranks[job_id]
        ahead = len(_admitting) + idx
    else:
        return None
    sample = sorted(_recent_job_seconds)
    typical = sample[len(sample) // 2] if sample else DEFAULT_JOB_SECONDS
    return {
        "position": ahead + 1,
        "ahead": ahead,
        "eta_seconds": queue_estimate(ahead, len(_running_jobs), MAX_CONCURRENT_JOBS, typical),
        "eta_is_minimum": _overtakable(prio),
    }


_last_arrival = 0.0


def _arrival_now() -> float:
    """Now, strictly after the last arrival handed out (equal keys would fall
    back to comparing job ids)."""
    global _last_arrival
    _last_arrival = max(time.time(), _last_arrival + 1e-6)
    return _last_arrival


def _job_arrival(job_id) -> float:
    """When the job was submitted (stamped on its record the first time)."""
    job = jobs.get(job_id)
    if job is None:
        return _arrival_now()
    t = job.get('submitted_at')
    if t is None:
        t = job['submitted_at'] = _arrival_now()
    return float(t)


def _job_last_run(job) -> float:
    """When the job last started running (here, or before a resume), or 0."""
    return float((job or {}).get('last_run') or (job or {}).get('run_started') or 0)


def _entry_key(priority, job_id, old_key=None) -> float:
    """Heap key of ``job_id`` at ``priority``. FIFO: its submission time.
    Newest first: minus the later of its submission and its last run, so a
    video cut by a drain or auto-retried counts as recent there and is not
    left at the back behind every newer arrival. Without a record the old
    key's time stands (``abs``: keys are signed times)."""
    job = jobs.get(job_id)
    if job is not None:
        sub, last = _job_arrival(job_id), _job_last_run(job)
    elif old_key is not None:
        sub = last = abs(float(old_key))
    else:
        sub = last = _arrival_now()
    return sub


def _enqueue_job(job_id: str):
    job_queue.put_nowait((JOB_PRIORITY, _entry_key(JOB_PRIORITY, job_id), job_id))
    _queue_changed()


def _manifest_names() -> list:
    """Job dirs holding a resume manifest. OUTPUT_DIR also has loose files (the
    instance marker, the queue watch state and lock, lock files): skip them
    instead of reading ``<file>/.resume.json``."""
    try:
        entries = os.listdir(OUTPUT_DIR)
    except FileNotFoundError:
        return []
    return [n for n in entries
            if not n.startswith(".") and os.path.isfile(_manifest_path(n))]


def _relocate_root_job_artifacts(job_id: str, job_output_dir: str) -> bool:
    """
    Backward-compat rescue:
    If main.py accidentally wrote metadata/clips into OUTPUT_DIR root (e.g. output/<jobid>_...),
    move them into output/<job_id>/ so the API can find and serve them.
    """
    try:
        os.makedirs(job_output_dir, exist_ok=True)
        root = OUTPUT_DIR
        pattern = os.path.join(root, f"{job_id}_*_metadata.json")
        meta_candidates = sorted(glob.glob(pattern), key=lambda p: os.path.getmtime(p), reverse=True)
        if not meta_candidates:
            return False

        # Move the newest metadata and its associated clips.
        metadata_path = meta_candidates[0]
        base_name = os.path.basename(metadata_path).replace("_metadata.json", "")

        # Move metadata
        dest_metadata = os.path.join(job_output_dir, os.path.basename(metadata_path))
        if os.path.abspath(metadata_path) != os.path.abspath(dest_metadata):
            shutil.move(metadata_path, dest_metadata)

        # Move any clips that match the same base_name into the job folder
        clip_pattern = os.path.join(root, f"{base_name}_clip_*.mp4")
        for clip_path in glob.glob(clip_pattern):
            dest_clip = os.path.join(job_output_dir, os.path.basename(clip_path))
            if os.path.abspath(clip_path) != os.path.abspath(dest_clip):
                shutil.move(clip_path, dest_clip)

        # Also move any temp_ clips that might remain
        temp_clip_pattern = os.path.join(root, f"temp_{base_name}_clip_*.mp4")
        for clip_path in glob.glob(temp_clip_pattern):
            dest_clip = os.path.join(job_output_dir, os.path.basename(clip_path))
            if os.path.abspath(clip_path) != os.path.abspath(dest_clip):
                shutil.move(clip_path, dest_clip)

        return True
    except Exception:
        return False

def _canonical_clip_file(output_dir, base_name, index):
    """The file to serve for clip ``index``, preferring a derived version.

    The pipeline writes the clean reframe as ``<base>_clip_<n>.mp4`` and any
    post-processing (auto-captions, /api/subtitle re-styles, and clip-editor
    recuts) as ``subtitled_<ts>_<clean>.mp4`` / ``recut_<ts>_<clean>.mp4``,
    keeping the original for re-styling. Every place that rebuilds the
    canonical name from disk — restore after a restart, the R2 upload, the
    download bundle — must therefore resolve to the newest derived file, or
    clips silently lose their captions (or their recut) on a redeploy.
    """
    clean = f"{base_name}_clip_{index + 1}.mp4"
    try:
        # subtitled_*_{clean} also matches subtitled_<ts>_recut_<ts>_{clean}
        # and subtitled_<ts>_hooked_<ts>_{clean}, i.e. captioned recuts and
        # captioned hooks; the bare recut_/hooked_/hook_ patterns cover
        # derivations that shipped uncaptioned (hook_ is the legacy manual-
        # hook prefix, kept so old jobs still resolve).
        derived = (glob.glob(os.path.join(output_dir, f"subtitled_*_{clean}"))
                   + glob.glob(os.path.join(output_dir, f"recut_*_{clean}"))
                   + glob.glob(os.path.join(output_dir, f"hooked_*_{clean}"))
                   + glob.glob(os.path.join(output_dir, f"hook_{clean}")))
    except Exception:
        derived = []
    if not derived:
        return clean
    # Highest timestamp wins — that's the most recent styling.
    return os.path.basename(max(derived, key=os.path.getmtime))


def _clips_actually_rendered(job_id, output_dir, base_name, clips):
    """Keep only the clips whose file is really on disk. Returns (kept, missing).

    The metadata JSON is written BEFORE any clip renders, and main.py's worker
    pool swallows a per-clip exception (it only prints "❌ Clip N failed") and
    still exits 0. So ``shorts`` is what Gemini promised, not what ffmpeg
    produced, and ``_canonical_clip_file`` happily returns the name of a file
    that was never written.

    Handing those back as delivered clips is what let a job that rendered
    NOTHING be marked completed: the minutes were committed, ClipsDelivered
    fired, ``archive_job`` skipped every missing file and uploaded the metadata
    alone — which, because it returns before writing the Project row, left the
    job unrestorable and invisible in history — and the dashboard offered clips
    whose only existence was a title, so "download all" answered 404 "No clip
    files found for this job" (ticket #4711, job 89d76bbc, 6-sep-2026).
    Measured over the R2 archive on 7-sep-2026: 195 of 1687 archived jobs held
    a metadata file and not one clip, across 184 users.

    Trust the disk, not the promise.
    """
    # main.py announces the file it actually finished for each clip
    # (CLIP_READY, printed after the whole reframe/watermark/hook/caption
    # chain). Prefer it over rebuilding the name: the marker is what the file
    # IS, _canonical_clip_file is a guess from the naming convention.
    ready_files = (jobs.get(job_id) or {}).get('ready_files') or {}
    kept = []
    for i, clip in enumerate(clips):
        clip_filename = (ready_files.get(i)
                         or _canonical_clip_file(output_dir, base_name, i))
        clip_path = os.path.join(output_dir, clip_filename)
        try:
            present = os.path.getsize(clip_path) > 0
        except OSError:
            present = False
        if not present:
            print(f"⚠️  Clip {i + 1} of {job_id} never rendered "
                  f"({clip_filename}) — dropping it from the result.")
            continue
        clip['video_url'] = f"/videos/{job_id}/{clip_filename}"
        kept.append(clip)
    return kept, len(clips) - len(kept)


def _strip_burned_captions(output_dir, filename, materialize=True):
    """Walk ``subtitled_<ts>_`` prefixes back to the file without burned captions.

    Returns the name unchanged when there is nothing to strip (or when the
    underlying file is gone, e.g. a library restore that only kept the current
    version). A hooked-only picture the pipeline kept as a recipe
    (hooks.materialize) counts as there and is encoded here, unless
    ``materialize`` is False (the caller strips the hook next and never opens
    it). Encodes: call it off the event loop.
    """
    import hooks
    while True:
        m = re.match(r'^subtitled_\d+_(.+)$', filename)
        if not m:
            return filename
        under = m.group(1)
        if not os.path.exists(os.path.join(output_dir, under)):
            if not hooks.has_recipe(output_dir, under):
                return filename
            if materialize and not hooks.materialize(output_dir, under):
                return filename
        filename = under


def _strip_burned_hook(output_dir, filename):
    """Walk ``hooked_<ts>_`` (and legacy ``hook_``) prefixes back to the file
    without a burned hook. Same fail-safe contract as _strip_burned_captions:
    the name is returned unchanged when there is nothing to strip or the
    underlying file is gone."""
    while True:
        m = re.match(r'^(?:hooked_\d+_|hook_)(.+)$', filename)
        if not m or not os.path.exists(os.path.join(output_dir, m.group(1))):
            return filename
        filename = m.group(1)


def _media_guard(path: str) -> bool:
    """``/videos`` allowlist: deliverable types only (media_auth)."""
    return media_auth.is_servable(path)


def _reapply_captions(job_id, clip_index, video_path):
    """Re-burn the default captions onto a freshly derived file.

    Captions must always be the LAST layer. Editing or hooking a clip that
    already had them burned in produced `edited_subtitled_<...>`, and the next
    subtitle pass then stacked a second caption layer on top of the first —
    visibly doubled and unreadable in real user clips (26-jul-2026). So the
    derivation runs on the clean file and captions go back on afterwards.

    Returns the captioned path, or None if there was nothing to caption.
    """
    try:
        meta_files = glob.glob(os.path.join(OUTPUT_DIR, job_id, "*_metadata.json"))
        if not meta_files:
            return None
        with open(meta_files[0], 'r') as f:
            data = json.load(f)
        transcript = data.get('transcript')
        clips = data.get('shorts', [])
        if not transcript or clip_index >= len(clips):
            return None
        clip = clips[clip_index]
        # A recut clip is a concatenation of source segments, so the flat
        # start..end window is wrong for it — caption against the clip-relative
        # remapped transcript instead (same trick /api/subtitle uses).
        recipe_segments = (clip.get('recipe') or {}).get('segments')
        if recipe_segments:
            transcript = recut.virtual_transcript(transcript, recipe_segments)
            start, end = 0.0, recut.total_duration(recipe_segments)
        else:
            start, end = clip['start'], clip['end']
        if clip.get('caption_words'):
            transcript = _words_transcript(clip['caption_words'],
                                           transcript.get('language', 'en'))
            start, end = 0.0, transcript['segments'][0]['end']
        if clip.get('caption_style'):
            return _restyle_captions(video_path, transcript, start, end,
                                     clip['caption_style'],
                                     clip.get('layout_ranges'))
        import main as _main
        return _main.auto_caption_clip(video_path, transcript, start, end)
    except Exception as e:
        print(f"⚠️  Could not re-apply captions to {video_path}: {e}")
        return None


# The /api/subtitle request fields that make up a caption look. The endpoint
# records them on the clip ('caption_style' in the metadata) so the paths that
# put captions back on a freshly derived file (hook, AI edit, recut) burn the
# look the user picked. They used to burn the default one, so adding a hook
# silently swapped someone's small serif captions for big yellow Anton
# (github issue #82).
CAPTION_STYLE_FIELDS = (
    "style", "position", "font_size", "font_name", "font_color",
    "border_color", "border_width", "bg_color", "bg_opacity",
    "highlight_color", "effect", "base_opacity", "uppercase", "reveal",
    "shadow", "max_chars", "max_duration")


def _words_transcript(words, language="en"):
    """A one-segment transcript from clip-relative caption words
    ({word, start, end}, word with its leading space), so the generators burn
    user-edited text verbatim."""
    words = sorted(words, key=lambda w: w["start"])
    return {
        "language": language,
        "segments": [{
            "start": words[0]["start"], "end": max(w["end"] for w in words),
            "text": " ".join(w["word"] for w in words),
            "words": words,
        }],
    }


def _burn_caption_style(video_path, out_path, sub_path, transcript, start, end,
                        style, split_ranges=None):
    """Generate the caption file for ``style`` (CAPTION_STYLE_FIELDS, a
    missing field takes the SubtitleRequest default) and burn it onto
    ``video_path``. Returns False when no words fall in the range."""
    s = {k: SubtitleRequest.model_fields[k].default for k in CAPTION_STYLE_FIELDS}
    s.update({k: v for k, v in (style or {}).items() if k in s})
    max_chars = (max(1, min(40, int(s["max_chars"])))
                 if s["max_chars"] is not None else None)
    max_duration = (max(0.5, min(5.0, float(s["max_duration"])))
                    if s["max_duration"] is not None else None)
    if s["style"] == "karaoke":
        opts = dict(
            split_ranges=split_ranges,
            alignment=s["position"], fontsize=s["font_size"],
            font_name=s["font_name"], font_color=s["font_color"],
            border_color=s["border_color"], border_width=s["border_width"],
            highlight_color=s["highlight_color"], bg_color=s["bg_color"],
            bg_opacity=s["bg_opacity"], effect=s["effect"],
            base_opacity=s["base_opacity"], uppercase=s["uppercase"],
            reveal=s["reveal"], shadow=s["shadow"])
        if max_chars is not None:
            opts["max_chars"] = max_chars
        if max_duration is not None:
            opts["max_duration"] = max_duration
        ok = generate_ass(transcript, start, end, sub_path, **opts)
    else:
        ok = generate_srt(transcript, start, end, sub_path,
                          max_chars or 20, max_duration or 2.0)
    if not ok:
        return False
    burn_subtitles(video_path, sub_path, out_path,
                   alignment=s["position"], fontsize=s["font_size"],
                   font_name=s["font_name"], font_color=s["font_color"],
                   border_color=s["border_color"], border_width=s["border_width"],
                   bg_color=s["bg_color"], bg_opacity=s["bg_opacity"])
    return True


def _restyle_captions(video_path, transcript, start, end, style,
                      layout=None):
    """Burn a recorded caption style next to ``video_path`` with the naming
    auto_caption_clip uses (``subtitled_<ts>_<file>``, so the walk-backs find
    the clean file). Returns the captioned path, or None (no words, failure)."""
    output_dir = os.path.dirname(video_path)
    generation_id = int(time.time())
    karaoke = (style or {}).get("style") == "karaoke"
    # Neutral name: the path goes inside an ffmpeg filter string, where an
    # apostrophe from a clip title would close the quote.
    sub_path = os.path.join(
        output_dir,
        f"subs_restyle_{generation_id}_{uuid.uuid4().hex[:8]}.{'ass' if karaoke else 'srt'}")
    out_path = os.path.join(
        output_dir, f"subtitled_{generation_id}_{os.path.basename(video_path)}")
    seams = layout_ranges.split_ranges(layout or layout_ranges.read(video_path))
    try:
        if not _burn_caption_style(video_path, out_path, sub_path, transcript,
                                   start, end, style, seams):
            return None
        return out_path
    except Exception as e:
        print(f"⚠️  Could not burn the recorded caption style on {video_path}: {e}")
        return None


def _recut_captioner(clip):
    """perform_recut's ``captioner`` for a clip whose captions were styled
    in the modal, or None for the default look. Edited words are not carried:
    their timings belong to the previous cut."""
    style = clip.get('caption_style')
    if not style:
        return None
    return lambda path, transcript, start, end: _restyle_captions(
        path, transcript, start, end, style)


def _recover_jobs_from_disk():
    """Rebuild completed jobs from OUTPUT_DIR after a restart (issue #46 / #18).

    Jobs live in memory, so a restart used to orphan finished clips that are
    still on disk: the frontend restores the job_id from localStorage but every
    endpoint answers 404 "Job not found". Rebuild a minimal completed record
    for each job directory that has a metadata JSON.
    """
    recovered = 0
    try:
        entries = os.listdir(OUTPUT_DIR)
    except FileNotFoundError:
        return
    for job_id in entries:
        job_path = os.path.join(OUTPUT_DIR, job_id)
        if not os.path.isdir(job_path) or job_id in jobs:
            continue
        if os.path.isfile(os.path.join(job_path, _RESUME_FILE)):
            continue  # in flight (interrupted or on the other instance): resume, not recover
        json_files = glob.glob(os.path.join(job_path, "*_metadata.json"))
        if not json_files:
            continue
        try:
            with open(json_files[0], 'r') as f:
                data = json.load(f)
            base_name = os.path.basename(json_files[0]).replace('_metadata.json', '')
            clips = data.get('shorts', [])
            for i, clip in enumerate(clips):
                if not clip.get('video_url'):
                    clip['video_url'] = (
                        f"/videos/{job_id}/"
                        f"{_canonical_clip_file(job_path, base_name, i)}")
            jobs[job_id] = {
                'status': 'completed',
                'logs': _TimedLog(["♻️ Job recovered from disk after server restart."]),
                'output_dir': job_path,
                'result': {'clips': clips, 'cost_analysis': data.get('cost_analysis')},
            }
            recovered += 1
        except Exception as e:
            print(f"⚠️ Could not recover job {job_id}: {e}")
    if recovered:
        print(f"♻️  Recovered {recovered} completed job(s) from disk.")


# --- Mid-flight job resume (survive a redeploy without losing work) ----------
# A job lives only in memory, so killing the container mid-processing used to
# lose it: the user's clip just stops. We persist a tiny manifest per job and,
# on startup, re-enqueue any that were interrupted — the user sees it resume
# instead of vanish. Bounded by MAX_RESUME_ATTEMPTS so a video that reliably
# crashes the worker can't crashloop the service; only resumes of a run that
# had started count (a queued video survives any number of deploys).
_RESUME_FILE = ".resume.json"
MAX_RESUME_ATTEMPTS = 2

# --- Deploy handover (two instances, one disk) -------------------------------
# Coolify starts the NEW container before it stops the old one (rolling
# update), and both see the same OUTPUT_DIR. Without coordination the new one
# re-enqueued, at startup, the very jobs the old one was still rendering —
# and the old one had 30 s to live anyway. Now:
#   * every instance stamps OUTPUT_DIR/.instance with its id at startup; an
#     instance that sees another id there knows it is the OLD one and DRAINS:
#     it finishes what it is running, starts nothing new, and leaves queued
#     manifests for the new instance to pick up;
#   * a running job writes a heartbeat into its manifest every few seconds,
#     so the new instance skips manifests that are alive elsewhere and resumes
#     only the stale ones (an instance killed mid-job stops heartbeating);
#   * SIGTERM (docker stop) also drains, up to DRAIN_TIMEOUT_SECONDS — keep it
#     under the Coolify stop grace period — before letting uvicorn exit.
INSTANCE_ID = os.environ.get("INSTANCE_ID") or socket.gethostname()
_INSTANCE_MARKER = ".instance"
HEARTBEAT_EVERY = 10                 # seconds between manifest heartbeats
HEARTBEAT_STALE_AFTER = 60           # no heartbeat for this long = nobody has it
RESUME_SCAN_INTERVAL = 30            # seconds between looks for stale manifests
HANDOVER_CHECK_INTERVAL = 5          # seconds between looks at the marker
DRAIN_TIMEOUT_SECONDS = int(os.environ.get("DRAIN_TIMEOUT_SECONDS", "840"))
# After the jobs are drained, keep SERVING this long with /health/ready at 503
# before closing the socket: the proxy only drops a container once its Docker
# healthcheck has failed interval*retries times (15 s with the Coolify
# settings), and closing the socket earlier sends that many seconds of
# requests to a dead port. Measured 2026-08-25: ~60 s of alternating 502/200
# per deploy with retries=12 and no grace at all.
PROXY_DRAIN_SECONDS = float(os.environ.get("PROXY_DRAIN_SECONDS", "20"))
# Once uvicorn has the signal it closes within --timeout-graceful-shutdown
# (15 s), but the interpreter then waits for non-daemon threads, and a
# request cancelled mid-flight can leave an executor thread stuck in a
# network probe (yt-dlp) for as long as that takes. Seen 2026-08-25: "Finished
# server process" printed, container alive until the 900 s SIGKILL, deploy
# stuck in "Removing old containers". So the process is ended outright a
# little after uvicorn was told to stop. Jobs are already drained by then.
HARD_EXIT_SECONDS = float(os.environ.get("HARD_EXIT_SECONDS", "30"))
_draining = False
_stopping = False                    # SIGTERM received: report not-ready so the
                                     # proxy stops routing here before the
                                     # listening socket closes
_running_jobs: set = set()           # job ids with a live subprocess here


def _local_running() -> int:
    """Jobs whose main.py runs on this host: what the GPU slots count."""
    return len(_running_jobs)


def _manifest_path(job_id):
    return os.path.join(OUTPUT_DIR, job_id, _RESUME_FILE)


def _read_manifest(job_id):
    try:
        with open(_manifest_path(job_id)) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"⚠️ Bad resume manifest for {job_id}: {e}")
        return None


def _touch_manifest(job_id, now=None, started=False):
    """Stamp 'this instance is running it, as of now' into the manifest.
    ``started`` (at dispatch) also stamps ``run_started``: a heartbeat says the
    worker is alive, not that the job is moving, so the metering sweeper's
    hung-job cap counts from the start of the run."""
    m = _read_manifest(job_id)
    if m is None:
        return
    m["heartbeat"] = now if now is not None else time.time()
    m["instance"] = INSTANCE_ID
    if started:
        m["run_started"] = m["heartbeat"]
    try:
        # Atomic: the other instance reads it at any time (resume scan, GPU
        # admission, the metering sweeper's live set), and a half-written
        # manifest reads as no manifest at all.
        write_json_atomic(_manifest_path(job_id), m)
    except Exception as e:
        print(f"⚠️ Could not heartbeat manifest for {job_id}: {e}")


def _manifest_busy_elsewhere(m, now=None):
    """True when ANOTHER instance heartbeated this job recently. Our own id
    with a fresh heartbeat is a job WE were running before a restart of this
    same container (same hostname) — that one must be resumed, not skipped."""
    now = time.time() if now is None else now
    return (m.get("instance") not in (None, INSTANCE_ID)
            and now - float(m.get("heartbeat") or 0) < HEARTBEAT_STALE_AFTER)


def _jobs_busy_elsewhere(now=None) -> int:
    """How many jobs another instance is running right now (fresh heartbeat).

    During a deploy the old container keeps running its jobs while it drains,
    on the same GPU. Counting them is what stops the new container from
    stacking its own full MAX_CONCURRENT_JOBS on top: 22-sep-2026, 7 old + 3
    new jobs filled the 20 GB card and 5 of 12 jobs died of CUDA OOM.
    """
    now = time.time() if now is None else now
    busy = 0
    try:
        entries = os.listdir(OUTPUT_DIR)
    except FileNotFoundError:
        return 0
    for name in entries:
        path = os.path.join(OUTPUT_DIR, name, _RESUME_FILE)
        if not os.path.exists(path):
            continue
        m = _read_manifest(name)
        # A job delivering elsewhere has left the card: it holds no GPU slot.

        if m and _manifest_busy_elsewhere(m, now) and m.get("stage") != "delivering":
            busy += 1
    return busy


SHARED_GPU_WAIT_SECONDS = 5
SHARED_GPU_MAX_WAIT = DRAIN_TIMEOUT_SECONDS + 120
# Free VRAM a new job needs before it starts. A job peaks at ~4-6 GB while it
# transcribes; starting one on a nearly full card is what made NVENC and
# TransNetV2 of the jobs already running fail with CUDA OOM. 0 disables.
GPU_MIN_FREE_MB = int(os.environ.get("GPU_MIN_FREE_MB", "4500"))
_gpu_free_cache = {"at": 0.0, "mb": None}


def _gpu_free_mb():
    """Free memory on GPU 0 in MiB, or None when there is no nvidia-smi (CPU
    host, self-host without a GPU): then only the job count limits."""
    now = time.monotonic()
    if now - _gpu_free_cache["at"] < 2:
        return _gpu_free_cache["mb"]
    mb = None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits",
             "-i", "0"], capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            mb = int(float(out.stdout.strip().splitlines()[0]))
    except Exception:
        mb = None
    _gpu_free_cache.update(at=now, mb=mb)
    return mb


def gpu_has_room(running_here: int, busy_elsewhere: int, free_mb) -> bool:
    """Whether a new job may start now.

    The job count covers both instances during a deploy handover. The free
    VRAM check covers what the count cannot see: jobs differ by an order of
    magnitude in memory. With nothing running anywhere the job always starts,
    so a card held by something outside our control can never stall the queue.
    """
    total = running_here + busy_elsewhere
    if total >= MAX_CONCURRENT_JOBS:
        return False
    if total == 0 or GPU_MIN_FREE_MB <= 0 or free_mb is None:
        return True
    return free_mb >= GPU_MIN_FREE_MB


async def _wait_for_shared_gpu():
    """Hold a new job until the GPU has room for it: this instance's jobs
    plus the ones another instance is still draining must fit
    MAX_CONCURRENT_JOBS, and the card must have GPU_MIN_FREE_MB free.
    Bounded: a killed instance stops heartbeating within
    HEARTBEAT_STALE_AFTER, a drain cannot outlive DRAIN_TIMEOUT_SECONDS, and
    past SHARED_GPU_MAX_WAIT the job starts anyway rather than wait forever."""
    waited = 0.0
    logged = False
    loop = asyncio.get_event_loop()
    while waited < SHARED_GPU_MAX_WAIT:
        # Reads every manifest on the shared disk: off the loop.
        elsewhere = await loop.run_in_executor(None, _jobs_busy_elsewhere)
        free_mb = await loop.run_in_executor(None, _gpu_free_mb)
        if gpu_has_room(_local_running(), elsewhere, free_mb):
            return
        if not logged:
            print(f"⏳ Holding the next job: {_local_running()} running here, "
                  f"{elsewhere} on a draining instance, "
                  f"{free_mb if free_mb is not None else '?'} MiB GPU free "
                  f"(needs {GPU_MIN_FREE_MB}).")
            logged = True
        await asyncio.sleep(SHARED_GPU_WAIT_SECONDS)
        waited += SHARED_GPU_WAIT_SECONDS


def _write_instance_marker():
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(os.path.join(OUTPUT_DIR, _INSTANCE_MARKER), "w") as f:
            f.write(INSTANCE_ID)
    except Exception as e:
        print(f"⚠️ Could not write instance marker: {e}")


def _read_instance_marker():
    try:
        with open(os.path.join(OUTPUT_DIR, _INSTANCE_MARKER)) as f:
            return f.read().strip()
    except Exception:
        return None


def _begin_drain(reason):
    global _draining
    if not _draining:
        _draining = True
        print(f"⏸️ Draining ({reason}): finishing {len(_running_jobs)} running job(s), "
              f"starting none.")


def _check_instance_marker():
    """A different id in the marker means a newer instance is up: drain."""
    other = _read_instance_marker()
    if other and other != INSTANCE_ID:
        _begin_drain(f"newer instance {other} is up")
        return True
    return False


async def _handover_watch():
    while not _draining:
        await asyncio.sleep(HANDOVER_CHECK_INTERVAL)
        _check_instance_marker()


async def _resume_scan():
    """Keep looking for manifests nobody is running (the old instance left
    them queued, or was killed at the end of its grace period)."""
    while True:
        await asyncio.sleep(RESUME_SCAN_INTERVAL)
        if _draining:
            continue
        try:
            await _resume_interrupted_jobs_async()
        except Exception as e:
            print(f"⚠️ Resume scan failed: {e}")


async def _drain_then_exit(previous_handler, timeout=None, proxy_grace=None,
                           hard_exit_after=None):
    """Wait for running jobs (bounded), let the proxy notice we are not ready,
    then hand the signal to uvicorn."""
    timeout = DRAIN_TIMEOUT_SECONDS if timeout is None else timeout
    proxy_grace = PROXY_DRAIN_SECONDS if proxy_grace is None else proxy_grace
    hard_exit_after = HARD_EXIT_SECONDS if hard_exit_after is None else hard_exit_after
    deadline = time.time() + timeout
    # A delivery (R2 upload, webhook, emails) runs after its job left the GPU
    # slot: wait for those too, so a deploy cuts none of them.
    while (_running_jobs or _delivering_jobs) and time.time() < deadline:
        await asyncio.sleep(1)
    if _running_jobs or _delivering_jobs:
        print(f"⏱️ Drain timeout after {timeout}s with {len(_running_jobs)} job(s) still "
              f"running and {len(_delivering_jobs)} delivering — they will resume on "
              f"the next instance.")
    else:
        print("✅ Drained: no running jobs.")
    if proxy_grace > 0:
        print(f"⏳ Serving {proxy_grace:.0f}s more while the proxy drops this instance.")
        await asyncio.sleep(proxy_grace)
    print("👋 Shutting down.")
    if hard_exit_after > 0:
        import threading
        threading.Timer(hard_exit_after, _hard_exit).start()
    if callable(previous_handler):
        previous_handler(signal.SIGTERM, None)
    else:
        os._exit(0)


def _hard_exit():
    print(f"⛔ Still alive {HARD_EXIT_SECONDS:.0f}s after the stop signal (a thread "
          f"is hanging) — exiting now.", flush=True)
    os._exit(0)


def _install_drain_signal_handler():
    """Replace uvicorn's SIGTERM handler with drain-first. uvicorn's own
    handler closes the listening socket at once, which would make Traefik
    send half the traffic to a refused port for the whole drain."""
    previous = signal.getsignal(signal.SIGTERM)
    loop = asyncio.get_running_loop()

    def on_sigterm():
        global _stopping
        _stopping = True
        _begin_drain("SIGTERM")
        asyncio.ensure_future(_drain_then_exit(previous))

    try:
        loop.add_signal_handler(signal.SIGTERM, on_sigterm)
    except (NotImplementedError, RuntimeError, ValueError) as e:
        print(f"⚠️ Drain-on-SIGTERM unavailable ({e}); jobs will resume on restart instead.")


def _write_resume_manifest(job_id, cmd, webhook_url=None, webhook_secret=None, base_url=None,
                           skip_statics=False, submitted_at=None):
    try:
        path = os.path.join(OUTPUT_DIR, job_id, _RESUME_FILE)
        # Atomic like every manifest write: the other instance reads them.
        write_json_atomic(path, {
                "cmd": cmd, "attempts": 0,
                # The caller's webhook must survive a redeploy: a pipeline that
                # relies on the callback would otherwise hang forever on a job
                # that resumed fine. The secret is the caller's own HMAC value,
                # stored next to their video on the same disk, not a server
                # credential (those are rebuilt from os.environ on resume).
                "webhook_url": webhook_url,
                "webhook_secret": webhook_secret,
                "base_url": base_url,
                # The probe's "statics bot-checked for this video" verdict.
                "skip_statics": bool(skip_statics),
                # The submission time keeps a resumed job's place in the queue.
                "submitted_at": submitted_at,
        })
    except Exception as e:
        print(f"⚠️ Could not write resume manifest for {job_id}: {e}")


def _clear_resume_manifest(job_id):
    """Drop the manifest once a job reaches a terminal state, so it is never
    re-run on a later restart. Only an interrupted (still-running) job keeps it."""
    try:
        os.remove(os.path.join(OUTPUT_DIR, job_id, _RESUME_FILE))
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"⚠️ Could not clear resume manifest for {job_id}: {e}")


def _legacy_submitted_at(job_id, manifest_mtime=None):
    """Submission time of a manifest written before ``submitted_at`` existed:
    the manifest's own mtime (its last write, the best left); else now."""
    return manifest_mtime if manifest_mtime else time.time()


def _resume_candidates(held: set, now: float):
    """Disk part of the resume scan (a worker thread on the periodic scan):
    ``[(job_id, manifest)]``. ``held`` is a copy of the
    ids this instance already has. Each picked manifest is rewritten with its
    attempt count, ``resumed_at`` and, for one written before it existed, the
    submission time, so its place in the queue is fixed from now on."""
    picked = []
    for job_id in _manifest_names():
        manifest_path = _manifest_path(job_id)
        # A manifest outlives only an interrupted run (the job wrapper drops it
        # at every terminal state), so a metadata file next to it means the job
        # was stopped mid-render, not that it finished: resume it too.
        try:
            with open(manifest_path) as f:
                m = json.load(f)
            mtime = os.path.getmtime(manifest_path)
        except Exception as e:
            print(f"⚠️ Bad resume manifest for {job_id}: {e}")
            continue
        if not isinstance(m, dict):
            continue

        if job_id in held:
            continue  # already ours (queued, running or recovered)
        if _manifest_busy_elsewhere(m, now):
            continue  # the other instance is on it; we take over if it goes stale

        # Only a run that STARTED since the last resume counts as an attempt:
        # the limit is there for a video that crashes the worker, and a video
        # that only waited in the queue through deploys has crashed nothing.
        # Counting every resume dropped long-queued videos that never ran
        # once a few deploys had gone by.
        ran = (float(m.get("run_started") or m.get("heartbeat") or 0)
               > float(m.get("resumed_at") or 0))
        attempts = int(m.get("attempts", 0)) + (1 if ran else 0)
        # A cut delivery runs no main.py again (nothing there can crash the
        # worker) and its render is done: never dropped as a poison job.
        if attempts > MAX_RESUME_ATTEMPTS and m.get("stage") != _DELIVERY_STAGE:
            # Poison job: don't resume; the user can retry by hand.
            print(f"🛑 Job {job_id} exceeded {MAX_RESUME_ATTEMPTS} resume attempts — giving up.")
            _clear_resume_manifest(job_id)
            continue

        m["attempts"] = attempts
        m["resumed_at"] = now
        if m.get("submitted_at") is None:
            m["submitted_at"] = _legacy_submitted_at(job_id, mtime)
        try:
            write_json_atomic(manifest_path, m)
        except Exception:
            pass
        picked.append((job_id, m))
    return picked


def _register_resumed(picked) -> int:
    """Loop part of the resume scan: a record and a heap entry per picked
    manifest. The heap orders them by submission time, whatever order the
    directory listing gave."""
    resumed = 0
    for job_id, m in picked:
        if job_id in jobs:
            continue  # registered while the disk was being read
        attempts = int(m.get("attempts", 0))
        job_path = os.path.join(OUTPUT_DIR, job_id)
        # Rebuild env from scratch: the manifest holds no secrets.
        env = child_env()
        if m.get("skip_statics"):
            env["DOWNLOAD_SKIP_STATICS"] = "1"
        else:
            env.pop("DOWNLOAD_SKIP_STATICS", None)

        jobs[job_id] = {
            'status': 'queued',
            'logs': _TimedLog([f"♻️ Resuming your video after a server update (attempt {attempts})."]),
            'cmd': m.get("cmd"),
            'env': env,
            'output_dir': job_path,
            'webhook_url': m.get("webhook_url"),
            'webhook_secret': m.get("webhook_secret"),
            'base_url': m.get("base_url"),
            'last_run': float(m.get("run_started") or m.get("heartbeat") or 0) or None,
            'submitted_at': float(m["submitted_at"] if m.get("submitted_at") is not None else time.time()),
        }
        if m.get("stage") == _DELIVERY_STAGE:
            # Rendered already, cut during its delivery: finish the delivery
            # (from the first step not done), never run main.py again.
            jobs[job_id]['logs'] = _TimedLog(["♻️ Finishing your video after a server update."])
            _resume_delivery_record(job_id, jobs[job_id], m)
            _pending_deliveries.append(job_id)
            resumed += 1
            continue
        _enqueue_job(job_id)
        resumed += 1
    if resumed:
        print(f"♻️  Re-enqueued {resumed} interrupted job(s) after restart.")
    return resumed


def _resume_interrupted_jobs():
    """Re-enqueue jobs that were mid-processing when the server last stopped.

    Runs after _recover_jobs_from_disk: a job whose clips already finished has a
    metadata JSON and is recovered as 'completed', so we only resume manifests
    with no metadata yet (analysis never finished). Also called periodically
    (_resume_scan, through ``_resume_interrupted_jobs_async``): a manifest
    another instance is heartbeating is left alone until that heartbeat goes
    stale, and a job this instance already holds is never enqueued twice.

    A poison job (too many attempts) is simply not resumed.
    """
    picked = _resume_candidates(set(jobs), time.time())
    _register_resumed(picked)


async def _resume_interrupted_jobs_async():
    """The periodic resume scan: the manifests are read (and rewritten) in a
    worker thread, only the records and the heap are touched on the loop."""
    picked = await asyncio.to_thread(_resume_candidates, set(jobs), time.time())
    _register_resumed(picked)
    _start_pending_deliveries()


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


# A job dir older than JOB_RETENTION_SECONDS can still be alive: on a busy day
# a free job waits over an hour in the queue, and a long render runs past the
# hour too. The sweeps used to delete those by age alone, under the worker's
# feet: no clips, and the minute reservation stayed held until the 3 h stuck
# sweep (22-sep-2026: 21 free jobs lost that way, most of them the user's
# first). A manifest pins its dir for at most this long, so one nobody will
# ever resume cannot keep a directory on disk forever.
ACTIVE_JOB_MAX_SECONDS = int(os.environ.get("ACTIVE_JOB_MAX_SECONDS", str(24 * 3600)))


def _job_is_active(job_id, now=None) -> bool:
    """True while a job is queued or running here, or its resume manifest says
    it has not reached a terminal state yet (the other instance's jobs during a
    handover included). The disk sweeps must never touch such a job."""
    if job_id in _running_jobs or job_id in _delivering_jobs:
        return True
    if (jobs.get(job_id) or {}).get('status') in ('queued', 'processing'):
        return True
    try:
        age = (time.time() if now is None else now) - os.path.getmtime(_manifest_path(job_id))
    except OSError:
        return False
    return age <= ACTIVE_JOB_MAX_SECONDS


def _upload_job_id(filename: str) -> str:
    """Source uploads are stored as ``<job_id>_<name>``."""
    return filename.split("_", 1)[0]


def _not_a_job_dir(name) -> bool:
    """Dot entries of OUTPUT_DIR are the instances' shared state, never a job:
    the country database (``.geoip/``), the queue watch and mode files, the
    priority stamp, lock files. No sweep, size cap or purge touches them."""
    return str(name).startswith(".")


def _sweep_expired_job_dirs(now):
    """Drop job dirs older than JOB_RETENTION_SECONDS, except live jobs."""
    for job_id in os.listdir(OUTPUT_DIR):
        if _not_a_job_dir(job_id):
            continue
        job_path = os.path.join(OUTPUT_DIR, job_id)
        if not os.path.isdir(job_path):
            continue
        try:
            expired = now - os.path.getmtime(job_path) > JOB_RETENTION_SECONDS
        except OSError:
            continue
        if not expired or _job_is_active(job_id, now):
            continue
        print(f"🧹 Purging old job: {job_id}")
        shutil.rmtree(job_path, ignore_errors=True)
        jobs.pop(job_id, None)


def _sweep_expired_uploads(now):
    """Drop source uploads older than JOB_RETENTION_SECONDS, except the source
    of a job that is still queued or running."""
    for filename in os.listdir(UPLOAD_DIR):
        file_path = os.path.join(UPLOAD_DIR, filename)
        try:
            if (now - os.path.getmtime(file_path) > JOB_RETENTION_SECONDS
                    and not _job_is_active(_upload_job_id(filename), now)):
                os.remove(file_path)
        except Exception:
            pass


def _enforce_uploads_size_cap():
    """Delete the oldest source uploads while UPLOAD_DIR is over UPLOADS_MAX_GB.

    Sources are only needed while a job runs (and for the preview afterwards),
    but they're the biggest files on disk — up to MAX_FILE_SIZE_MB each.
    """
    cap = UPLOADS_MAX_GB * 1024 ** 3
    if cap <= 0:
        return
    used = _dir_size(UPLOAD_DIR)
    if used <= cap:
        return
    files = []
    for name in os.listdir(UPLOAD_DIR):
        p = os.path.join(UPLOAD_DIR, name)
        if os.path.isfile(p) and not _job_is_active(_upload_job_id(name)):
            try:
                files.append((os.path.getmtime(p), p, os.path.getsize(p)))
            except OSError:
                pass
    files.sort()
    print(f"🧹 Uploads at {used / 1024**3:.1f} GB (cap {UPLOADS_MAX_GB} GB) — trimming.")
    for _mtime, path, size in files:
        if used <= cap:
            break
        try:
            os.remove(path)
            used -= size
            print(f"🧹 Size cap: removed upload {os.path.basename(path)}")
        except OSError:
            pass


def _enforce_output_size_cap():
    """Delete the oldest job dirs while OUTPUT_DIR is over OUTPUT_MAX_GB."""
    cap = OUTPUT_MAX_GB * 1024 ** 3
    if cap <= 0:
        return
    used = _dir_size(OUTPUT_DIR)
    if used <= cap:
        return
    candidates = []
    for job_id in os.listdir(OUTPUT_DIR):
        if _not_a_job_dir(job_id):
            continue
        p = os.path.join(OUTPUT_DIR, job_id)
        if os.path.isdir(p) and not _job_is_active(job_id):
            try:
                candidates.append((os.path.getmtime(p), p, job_id))
            except OSError:
                pass
    candidates.sort()  # oldest first
    print(f"🧹 Output dir at {used / 1024**3:.1f} GB (cap {OUTPUT_MAX_GB} GB) — trimming.")
    for _mtime, path, job_id in candidates:
        if used <= cap:
            break
        size = _dir_size(path)
        shutil.rmtree(path, ignore_errors=True)
        jobs.pop(job_id, None)
        used -= size
        print(f"🧹 Size cap: purged {job_id} ({size / 1024**2:.0f} MB)")


def _sweep_retained_sources(now=None):
    """Delete retained downloads older than SOURCE_RETENTION_SECONDS.

    Only the ``source_video`` a URL job kept in its own directory: an upload is
    the user's own file and ages out with the job like everything else. Yields
    the job ids it emptied. A no-op unless the knob is set below the job clock.
    """
    if SOURCE_RETENTION_SECONDS >= JOB_RETENTION_SECONDS:
        return
    now = time.time() if now is None else now
    for job_id in os.listdir(OUTPUT_DIR):
        if _not_a_job_dir(job_id):
            continue
        try:
            metas = glob.glob(os.path.join(OUTPUT_DIR, job_id, "*_metadata.json"))
            if not metas:
                continue
            with open(metas[0]) as f:
                name = json.load(f).get('source_video')
            if not name:
                continue
            src = os.path.join(OUTPUT_DIR, job_id, os.path.basename(name))
            if (os.path.exists(src)
                    and now - os.path.getmtime(src) > SOURCE_RETENTION_SECONDS):
                os.remove(src)
                yield job_id
        except Exception:
            continue


async def cleanup_jobs():
    """Background task to remove old jobs and files."""
    import time
    print("🧹 Cleanup task started.")
    while True:
        try:
            await asyncio.sleep(300) # Check every 5 minutes
            now = time.time()
            
            # Simple directory cleanup based on modification time
            # Check OUTPUT_DIR
            _sweep_expired_job_dirs(now)

            for job_id in _sweep_retained_sources(now):
                print(f"🧹 Dropped retained source for job {job_id}")

            # Hard disk cap. The time-based sweep above bounds the *age* of what
            # we keep, not its size: a burst of long videos can fill the volume
            # inside one retention window. Drop the oldest jobs until we're back
            # under the cap — clips are already archived to R2 and get restored
            # on demand, so this only costs a re-download.
            _enforce_output_size_cap()
            _enforce_uploads_size_cap()

            # Agent upload slots: expire with their file (the file sweep below
            # removes it; a slot whose file is gone or too old is dropped).
            for uid in _sweep_pending_uploads(now):
                print(f"🧹 Expired agent upload slot {uid}")

            # Cleanup Uploads
            _sweep_expired_uploads(now)

        except Exception as e:
            print(f"⚠️ Cleanup error: {e}")

async def process_queue():
    """Background worker to process jobs from the queue with concurrency limit."""
    print(f"🚀 Job Queue Worker started with {MAX_CONCURRENT_JOBS} concurrent slots.")
    while True:
        try:
            # Wait for a job (priority, seq, job_id) — lowest priority first.
            _priority, _seq, job_id = await job_queue.get()
            _queue_changed()

            if _draining:
                # Leave it on disk for the next instance (manifest, no heartbeat).
                print(f"⏸️ Draining — leaving {job_id} for the next instance.")
                job_queue.task_done()
                continue

            # Acquire semaphore slot (waits if max jobs are running)
            _admitting.add(job_id)
            await concurrency_semaphore.acquire()
            if _draining:
                _admitting.discard(job_id)
                concurrency_semaphore.release()
                job_queue.task_done()
                print(f"⏸️ Draining — leaving {job_id} for the next instance.")
                continue
            # The old container may still be draining its jobs on this GPU.
            await _wait_for_shared_gpu()
            _admitting.discard(job_id)
            if _draining:
                concurrency_semaphore.release()
                job_queue.task_done()
                print(f"⏸️ Draining — leaving {job_id} for the next instance.")
                continue
            print(f"🔄 Acquired slot for job: {job_id}")
            _running_jobs.add(job_id)
            if job_id in jobs:
                jobs[job_id]['run_started'] = time.time()
            _touch_manifest(job_id, started=True)
            # Process in background task to not block the loop (allowing other slots to fill)
            asyncio.create_task(run_job_wrapper(job_id))
            
        except Exception as e:
            print(f"❌ Queue dispatch error: {e}")
            await asyncio.sleep(1)

def _job_source_url(job) -> Optional[str]:
    """The ``-u`` argument of the job's main.py command, if it was a URL job."""
    cmd = list((job or {}).get('cmd') or [])
    # The command is ``python -u main.py -u <url>``: skip past the script
    # name first or the interpreter's own ``-u`` flag is what gets matched.
    start = next((i + 1 for i, a in enumerate(cmd) if str(a).endswith('main.py')), 0)
    for i in range(start, len(cmd) - 1):
        if cmd[i] in ('-u', '--url'):
            return cmd[i + 1]
    return None


def _release_slot(job_id):
    """The job left the running set: free what it held."""
    _running_jobs.discard(job_id)
    concurrency_semaphore.release()
    job_queue.task_done()


async def run_job_wrapper(job_id):
    """Wrapper to run job and release semaphore"""
    # Keep the record itself: run_job writes its status into this same dict,
    # and if the entry is dropped from ``jobs`` mid-run the reservation must
    # still be settled (released) instead of staying held for hours.
    job = jobs.get(job_id)
    interrupted = False
    started = time.monotonic()
    try:
        if job:
            await run_job(job_id, job)
            if job.get('status') == 'completed':
                _record_job_duration(time.monotonic() - started)
    except asyncio.CancelledError:
        # The server is shutting down (drain timeout of a deploy) and took this
        # task with it. Not a failure: the manifest stays on disk and the next
        # instance resumes the job (22-sep-2026: 6 jobs were marked failed and
        # refunded at once this way instead).
        interrupted = True
    except Exception as e:
         print(f"❌ Job wrapper error {job_id}: {e}")
    finally:
        if interrupted or (_stopping and job and job.get('status') != 'completed'):
            _release_slot(job_id)
            print(f"♻️ Server stopping: leaving {job_id} for the next instance.")
            if interrupted:
                raise asyncio.CancelledError()
            return
        if _should_auto_retry(job):
            _schedule_auto_retry(job_id, job)
            _release_slot(job_id)
            print(f"🔁 Released slot for job {job_id} (auto-retry scheduled).")
            return
        # The subprocess returned (success or genuine failure): a terminal
        # state. What is left (settle, R2 archive, unmark, webhook, emails) is
        # network I/O that never touches the card, so the GPU slot goes to the
        # next job now and the delivery runs outside it. The job enters the
        # delivery stage (manifest stamped, ``_delivering_jobs``) BEFORE it
        # leaves ``_running_jobs``: a drain or a scan never sees it in neither.
        if job is not None:
            _begin_delivery(job_id, job)
        _release_slot(job_id)
        print(f"✅ Released slot for job: {job_id}")
    if job is not None:
        await _deliver_job(job_id, job)
    else:
        _clear_resume_manifest(job_id)


# --- Delivery, outside the GPU slot -------------------------------------------
# A finished job still has to settle its minutes, upload its clips to R2, be
# unmarked if its owner paid meanwhile and tell the caller (webhook, email).
# That used to run while the job held its GPU slot, so the card sat idle for it
# on every job. Now it runs after the slot is released, at most
# DELIVERY_CONCURRENCY at a time (a slow R2 queues deliveries instead of piling
# up uploads), and:
#   * the user sees 'processing' until the clips are archived and unmarked
#     (``_presented_status``): no finished job with half of its delivery done;
#   * the resume manifest stays on disk, stamped ``stage: delivering`` with the
#     outcome and each step done, and is heartbeated, until the last step: a
#     drain waits for deliveries like for running jobs, and a delivery cut
#     anyway (drain timeout, killed container) is resumed by the next instance
#     from the first step not done, without running main.py again.
def _env_int(name, default, minimum=0):
    try:
        return max(minimum, int(os.environ.get(name, str(default)).strip()))
    except (TypeError, ValueError):
        return default


DELIVERY_CONCURRENCY = _env_int("DELIVERY_CONCURRENCY", 4, minimum=1)
_delivery_semaphore = asyncio.Semaphore(DELIVERY_CONCURRENCY)
_delivering_jobs: set = set()        # job ids whose delivery has not ended here
_pending_deliveries: list = []       # resumed deliveries waiting for the DB (boot)
_DELIVERY_STAGE = "delivering"
# Steps in order; the job reads finished to the user after the step named by
# _DELIVERY_VISIBLE_AFTER, before anything tells the caller (webhook, email),
# so a caller that polls on the webhook already reads 'completed'.
_DELIVERY_STEPS = (
    # The job reads finished to the user after this step, before anything
    # tells the caller.
    ("ready", lambda jid, job: _delivery_ready(jid, job)),
    # The caller's webhook.
    ("webhook", lambda jid, job: _notify_job_webhook(jid)),
)
_DELIVERY_VISIBLE_AFTER = "ready"


async def _delivery_ready(job_id, job):
    return


def _stamp_delivery(job_id, job, done):
    """Write the delivery stage into the job's manifest: outcome, the files
    main.py announced, the error text of a failed job and the steps done, so
    another instance can finish the delivery. No manifest: nothing to do."""
    m = _read_manifest(job_id)
    if m is None:
        return
    m["stage"] = _DELIVERY_STAGE
    m["outcome"] = job.get('status') if job.get('status') in ('completed', 'failed') else 'failed'
    m["ready_files"] = {str(k): v for k, v in (job.get('ready_files') or {}).items()}
    if m["outcome"] != 'completed':
        m["error"] = _job_error_text(list(job.get('logs') or []))[-500:]
    m["delivered"] = sorted(done)
    m["heartbeat"] = time.time()
    m["instance"] = INSTANCE_ID
    try:
        write_json_atomic(_manifest_path(job_id), m)
    except Exception as e:
        print(f"⚠️ Could not stamp the delivery of {job_id}: {e}")


def _begin_delivery(job_id, job):
    """Enter the delivery stage (on the loop, before the slot is released)."""
    _delivering_jobs.add(job_id)
    job['delivering'] = True
    _stamp_delivery(job_id, job, set(job.get('delivered') or ()))


async def _delivery_heartbeat(job_id):
    while True:
        await asyncio.sleep(HEARTBEAT_EVERY)
        _touch_manifest(job_id)


async def _deliver_job(job_id, job):
    """Run the delivery steps not done yet, then drop the manifest. Each step
    already guards its own errors; one that raises anyway is logged and the
    delivery goes on. Cancelled (the process is going away): the manifest
    stays, so the next instance finishes the delivery."""
    if job_id not in _delivering_jobs:
        _begin_delivery(job_id, job)
    done = set(job.get('delivered') or ())
    heartbeat = asyncio.create_task(_delivery_heartbeat(job_id))
    cancelled = False
    try:
        async with _delivery_semaphore:
            for name, step in _DELIVERY_STEPS:
                if name not in done:
                    try:
                        await step(job_id, job)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        print(f"⚠️ Delivery step {name} failed for {job_id}: {e}")
                    done.add(name)
                    job['delivered'] = sorted(done)
                    _stamp_delivery(job_id, job, done)
                if name == _DELIVERY_VISIBLE_AFTER:
                    job.pop('delivering', None)
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        heartbeat.cancel()
        if not cancelled:
            _clear_resume_manifest(job_id)
            job.pop('delivering', None)
        _delivering_jobs.discard(job_id)


def _result_from_disk(job_id, output_dir):
    """The result of a job whose main.py exited 0, rebuilt from its dir (the
    clips main.py announced, or the canonical files). None: nothing rendered."""
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
    if not json_files:
        return None
    with open(json_files[0], 'r') as f:
        data = json.load(f)
    base_name = os.path.basename(json_files[0]).replace('_metadata.json', '')
    rendered, _missing = _clips_actually_rendered(
        job_id, output_dir, base_name, data.get('shorts', []))
    if not rendered:
        return None
    return {'clips': rendered, 'cost_analysis': data.get('cost_analysis')}


def _resume_delivery_record(job_id, job, m):
    """Turn a resumed record into a delivery: outcome from the manifest, the
    result from the disk. Called with ``jobs[job_id]`` already set."""
    job['ready_files'] = {}
    for k, v in (m.get("ready_files") or {}).items():
        try:
            job['ready_files'][int(k)] = v
        except (TypeError, ValueError):
            pass
    job['delivered'] = list(m.get("delivered") or [])
    job['delivering'] = True
    status = m.get("outcome") if m.get("outcome") in ('completed', 'failed') else 'failed'
    if status == 'completed':
        try:
            job['result'] = _result_from_disk(job_id, job['output_dir'])
        except Exception as e:
            print(f"⚠️ Could not rebuild the result of {job_id}: {e}")
            job['result'] = None
        if not job['result']:
            status = 'failed'
            job['logs'].append("No clips could be rendered from this video.")
    elif m.get("error"):
        job['logs'].append(str(m["error"]))
    job['status'] = status


def _start_pending_deliveries():
    """Start the deliveries resumed from manifests (the DB is up by now)."""
    while _pending_deliveries:
        jid = _pending_deliveries.pop(0)
        job = jobs.get(jid)
        if job is not None and jid not in _delivering_jobs:
            _begin_delivery(jid, job)   # ours now: our instance id, fresh heartbeat
            print(f"📦 Finishing the delivery of {jid} left by another instance.")
            asyncio.create_task(_deliver_job(jid, job))


# --- Automatic retry of transient failures ----------------------------------
# A job that dies of a condition on OUR side (a full GPU, an NVENC session
# refused, a Gemini 5xx) is run once more by itself instead of showing the user
# "failed". Content problems (no audio, private video, nothing clip-shaped,
# policy block) fail as before: retrying cannot fix them.
AUTO_RETRY_LIMIT = int(os.environ.get("AUTO_RETRY_LIMIT", "1"))
AUTO_RETRY_DELAY_SECONDS = float(os.environ.get("AUTO_RETRY_DELAY_SECONDS", "30"))
_TRANSIENT_MARKERS = (
    "out of memory", "outofmemory", "cuda", "cublas", "cudnn", "nvenc",
    "generic error in an external library", "exit code 187", "broken pipe",
    "no clips could be rendered", "resource_exhausted", "resource exhausted",
    "503", "502", "500 internal", "overloaded", "deadline exceeded",
    "exit code -9", "exit code -15", "killed",
)
_PERMANENT_MARKERS = (
    "no audio", "no_audio", "video unavailable", "private video", "members-only",
    "prohibited_content", "blocked this video", "blocked its answer",
    "did not return usable clips", "clip detection failed", "short-form content",
    "sign in to confirm your age", "not available in your country",
)


def is_transient_failure(logs) -> bool:
    """Whether a failed job's logs describe something a second run can fix."""
    text = _job_error_text(list(logs or [])).lower()
    if any(m in text for m in _PERMANENT_MARKERS):
        return False
    return any(m in text for m in _TRANSIENT_MARKERS)


def _should_auto_retry(job) -> bool:
    if not (job and job.get('status') == 'failed' and not _draining and job.get('cmd')):
        return False
    # YouTube limiting the account is neither the video's fault nor a server
    # fault: the job waits out the cooldown, a few times at most, on its own
    # counter (it has not used its one retry for real failures).
    if job.get('yt_limited'):
        return int(job.get('yt_retries') or 0) < YT_LIMIT_RETRIES
    return bool(int(job.get('auto_retries') or 0) < AUTO_RETRY_LIMIT
                and is_transient_failure(job.get('logs')))


def _clean_for_retry(output_dir):
    """Drop the half-made outputs of a failed run; keep the source, the
    transcript checkpoint (the retry skips transcription), owner and manifest."""
    try:
        names = os.listdir(output_dir)
    except OSError:
        return
    for name in names:
        if ("_clip_" in name or name.endswith("_metadata.json")
                or name.startswith(("temp_", "hooked_", "autosubs_"))):
            try:
                os.remove(os.path.join(output_dir, name))
            except OSError:
                pass


YT_LIMIT_RETRIES = _env_int("YT_LIMIT_RETRIES", 3)
YT_LIMITED_LOG = ("⏳ YouTube is limiting downloads right now. Your video keeps its place "
                  "and starts again in about {n} min, no minutes are charged twice.")


def _schedule_auto_retry(job_id, job):
    delay = AUTO_RETRY_DELAY_SECONDS
    if job.pop('yt_limited', None):
        # Back in line for when the account's cooldown is over (plus a spread,
        # so the videos that waited do not all ask YouTube in the same second).
        job['yt_retries'] = int(job.get('yt_retries') or 0) + 1
        delay = max(delay, yt_account.cooling_down()) + 20 + (hash(job_id) % 120)
        job['logs'].append(YT_LIMITED_LOG.format(n=max(1, round(delay / 60))))
        print(f"⏳ {job_id} waits {delay:.0f}s for the YouTube account "
              f"({job['yt_retries']}/{YT_LIMIT_RETRIES}).")
    else:
        job['auto_retries'] = int(job.get('auto_retries') or 0) + 1
        job['logs'].append("🔁 A temporary server problem interrupted your video. "
                           "Retrying it automatically, no minutes are charged twice.")
        print(f"🔁 Auto-retry {job['auto_retries']}/{AUTO_RETRY_LIMIT} for {job_id} "
              f"in {AUTO_RETRY_DELAY_SECONDS:.0f}s.")
    job['status'] = 'queued'
    job['result'] = None
    job['ready_files'] = {}
    _clean_for_retry(job.get('output_dir') or os.path.join(OUTPUT_DIR, job_id))

    async def _later():
        await asyncio.sleep(delay)
        if job_id in jobs and jobs[job_id].get('status') == 'queued':
            _enqueue_job(job_id)

    asyncio.create_task(_later())


# Markers that identify a line as an actual error rather than progress noise.
# "Error:" (capital E) catches raised exception lines — RuntimeError:,
# DownloadError:, GeminiBlockedError: — which "ERROR:" alone missed, leaving
# alerts with a bare "Traceback ... exit code 1" and no cause (prod 20-ago).
_ERROR_MARKERS = ("❌", "ERROR:", "Error:", "Traceback", "FATAL", "Exception",
                  "Process failed with exit code", "No metadata file generated",
                  "Execution error:",
                  # A clip render dies in two steps: reframe_v2 raises (it has
                  # no False return, only `return True`), main.py prints this
                  # and falls back to the v1 loop, and only if v1 ALSO fails
                  # does "❌ Clip N failed" appear. The ❌ line then carries
                  # v1's exception, not the one that started it, so without
                  # this marker the alert names the fallback and hides the
                  # cause. Both lines are wanted.
                  "Reframe v2 failed")


def _job_error_text(logs) -> str:
    """The lines that explain WHY a job failed, for the alert's classifier.

    The tail of the log is usually progress noise (scene detection, ffmpeg
    banners), which made alerts blame whatever word happened to be nearby —
    a silent upload got reported as a broken download path, and a Gemini blip
    as an ffmpeg problem. Pick the error-bearing lines instead, newest last.
    """
    # Per-attempt download warnings are only noise when a later attempt
    # RECOVERED: HD-direct fails on every job (banned server IP) and a static
    # proxy takes over, yet its "Video unavailable" made whole alerts read as
    # download outages when the job actually died in Gemini. When no attempt
    # succeeded, those lines carry the real cause and must stay.
    recovered = any("Download succeeded" in ln for ln in logs)
    hits = [ln for ln in logs
            if any(m in ln for m in _ERROR_MARKERS)
            and not (recovered and "Download attempt" in ln)]
    if not hits:
        return " ".join(logs[-10:])  # nothing recognisable — fall back to the tail
    return " ".join(hits[-6:])


# --- Job completion webhooks --------------------------------------------------
# Agents and pipelines (n8n, cron, MCP clients) need push, not poll: a caller
# passes webhook_url on /api/process and gets one POST when the job reaches a
# terminal state. The URL goes through assert_public_url both at submit and at
# delivery time — the second check is what defeats DNS rebinding between them.
WEBHOOK_TIMEOUT = 10.0
WEBHOOK_RETRY_DELAYS = (0, 10, 60)  # seconds before each attempt


def _sign_webhook(body: bytes, secret: str) -> str:
    import hmac as _hmac
    import hashlib as _hashlib
    return "sha256=" + _hmac.new(secret.encode(), body, _hashlib.sha256).hexdigest()


async def _webhook_clip_entries(job_id, job):
    """The payload's clip list: absolute URLs, plus durable R2 links when the
    job was archived (a webhook consumer usually fetches later, after the
    1-hour local retention would have expired the /videos path)."""
    base = (job.get('base_url') or os.environ.get("PUBLIC_API_URL", "")).rstrip("/")
    clips = (job.get('result') or {}).get('clips') or []
    entries = []
    for i, clip in enumerate(clips):
        rel = clip.get('video_url') or ""
        entries.append({
            "index": i,
            "title": clip.get('title') or clip.get('video_title_for_youtube_short'),
            # Signed: a webhook consumer has no session to show /videos.
            "video_url": _signed_media_url(rel, base) if rel.startswith("/") else rel,
        })
    return entries


async def _deliver_webhook(url, body: bytes, secret):
    headers = {"Content-Type": "application/json", "User-Agent": "OpenShorts-Webhook/1.0"}
    if secret:
        headers["X-OpenShorts-Signature"] = _sign_webhook(body, secret)
    from security_utils import assert_public_url, UnsafeURLError
    loop = asyncio.get_event_loop()
    for attempt, delay in enumerate(WEBHOOK_RETRY_DELAYS, 1):
        if delay:
            await asyncio.sleep(delay)
        try:
            # Re-resolve on every attempt: the submit-time check is stale by now.
            await loop.run_in_executor(None, assert_public_url, url)
            async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT,
                                         follow_redirects=False) as client:
                resp = await client.post(url, content=body, headers=headers)
            if resp.status_code < 300:
                print(f"🪝 Webhook delivered to {url} (attempt {attempt})")
                return
            print(f"⚠️ Webhook attempt {attempt} to {url}: HTTP {resp.status_code}")
        except UnsafeURLError as e:
            print(f"🛑 Webhook URL no longer safe, dropping: {e}")
            return
        except Exception as e:
            print(f"⚠️ Webhook attempt {attempt} to {url} failed: {e}")
    print(f"❌ Webhook to {url} gave up after {len(WEBHOOK_RETRY_DELAYS)} attempts.")


async def _notify_job_webhook(job_id):
    """Fire the caller's webhook for a terminal job. Runs inside run_job_wrapper's
    finally AFTER the R2 archive, so durable links exist; the actual delivery
    (with its retry sleeps) is detached so the worker slot frees immediately."""
    job = jobs.get(job_id) or {}
    url = job.get('webhook_url')
    if not url or job.get('webhook_sent'):
        return
    job['webhook_sent'] = True
    completed = job.get('status') == 'completed'
    payload = {
        "event": "job.completed" if completed else "job.failed",
        "job_id": job_id,
        "status": job.get('status'),
        "clips": (await _webhook_clip_entries(job_id, job)) if completed else [],
    }
    if not completed:
        payload["error"] = _job_error_text(job.get('logs', []))[-500:]
    body = json.dumps(payload).encode()
    asyncio.create_task(_deliver_webhook(url, body, job.get('webhook_secret')))


# --- Queue watch hooks (cloud/queue_watch.py pages when the queue backs up) ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Rehydrate finished jobs from disk before serving (survives restarts).
    _recover_jobs_from_disk()
    # Re-enqueue jobs that were mid-processing when we stopped (redeploy).
    _resume_interrupted_jobs()
    # Deploy handover: claim the marker (any older instance sees it and drains),
    # keep watching it in case a newer one appears, keep looking for manifests
    # left behind, and drain instead of dying on docker stop.
    _write_instance_marker()
    _install_drain_signal_handler()
    asyncio.create_task(_handover_watch())
    asyncio.create_task(_resume_scan())
    cleanup_task = asyncio.create_task(cleanup_jobs())
    # Deliveries cut by the last instance (rendered, not delivered).
    _start_pending_deliveries()
    worker_task = asyncio.create_task(process_queue())
    yield
    # Cleanup (optional: cancel worker)

app = FastAPI(lifespan=lifespan)

# Brand banner library (self-host): /api/banners; a job picks one with banner_id.
import brand_api as _brand_api
app.include_router(_brand_api.router)
import projects_api
app.include_router(projects_api.router)

# Enable CORS for frontend. Cloud mode locks this down to the configured origins;
# self-host keeps the permissive wildcard it has always used.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Without this the browser hides them from fetch(), so the clip download had
    # no total to measure against and could not show progress. Safelisted or not,
    # they only become readable to JS once they are named here.
    expose_headers=["Content-Length", "Content-Range", "Accept-Ranges"],
)

# Mount static files for serving videos. ``_media_guard`` is the allowlist: only
# deliverable types (media_auth.is_servable), never a job's scratch files.
from restoring_static import RestoringStaticFiles
app.mount("/videos", RestoringStaticFiles(directory=OUTPUT_DIR, guard=_media_guard), name="videos")


def _signed_media_url(url_path: str, base: str = "") -> str:
    """A ``/videos`` path, absolute when ``base`` is given."""
    return f"{base.rstrip('/')}{url_path}" if base and url_path.startswith("/") else url_path


def _safe_under(base_dir: str, user_rel_path: str) -> Optional[str]:
    """Resolve ``user_rel_path`` under ``base_dir`` and reject path traversal.

    Returns the absolute path only if it stays inside ``base_dir`` (after
    following ``..``); otherwise None. Used to sanitize client-supplied file
    references so ``../../.env`` can't escape the output directories.
    """
    base = os.path.realpath(base_dir)
    target = os.path.realpath(os.path.join(base, user_rel_path))
    if target == base or target.startswith(base + os.sep):
        return target
    return None

class ProcessRequest(BaseModel):
    url: str

# Masks user:password credentials embedded in any URL (e.g. the residential
# proxy URL that yt-dlp echoes in its verbose debug output) before the line is
# ever printed to the server console or stored in the job log.
_CREDENTIAL_URL_RE = re.compile(r'(\w+://)[^:/@\s]+:[^@/\s]+@')


def _scrub_secrets(line: str) -> str:
    return _CREDENTIAL_URL_RE.sub(r'\1***:***@', line)


# Cloud users don't need (and shouldn't see) implementation details: the ingest
# plumbing (proxy / downloader / cookies) OR which AI model powers it, token
# usage and cost. These are dropped from the client view even when the line is
# emoji-prefixed. Never applied under DEBUG_LOGS (local dev sees everything).
_SENSITIVE_LOG_RE = re.compile(
    # Ingest plumbing
    r'proxy|yt[-_ ]?dlp|youtube-?dl|cookie|residential|po[_ ]?token'
    r'|player_client|extractor|\bdownload|descarg'
    # AI model / provider / cost / pipeline internals
    r'|gemini|openai|anthropic|\bflash\b|\bmodel\b|token|thinking'
    r'|\bcost\b|\$\s*[0-9]|scoring window|shortlist',
    re.IGNORECASE,
)


class _TimedLog(list):
    """A job's log lines plus the time each one arrived (``.times``).

    The dashboard used to stamp every line with the time it was RENDERED, so
    all of them showed the same clock and it changed on every poll.
    """

    def __init__(self, lines=()):
        super().__init__(lines)
        self.times = [time.time()] * len(self)

    def append(self, line):
        super().append(line)
        self.times.append(time.time())

    def extend(self, lines):
        lines = list(lines)
        super().extend(lines)
        self.times.extend([time.time()] * len(lines))


def _visible_logs_timed(logs):
    """(lines, times) to surface to the client; see _visible_logs."""
    times = getattr(logs, "times", None)
    if times is not None and len(times) != len(logs):
        times = None
    return list(logs), list(times) if times is not None else None


def _visible_logs(logs):
    """Logs to surface to the client.

    Self-host (BILLING off) shows the full pipeline output so people running
    their own instance can debug. Cloud shows a curated whitelist view
    (log_view.friendly_logs): plain progress for normal users — transcription
    percentage, clip counters — with no file paths, model names or pipeline
    internals.

    DEBUG_LOGS=true forces the full output even under billing — for local dev
    where you run in paid mode but still want the raw logs.
    """
    return logs


def _handle_job_line(job_id, line):
    """One output line of a job's main.py: internal markers are consumed, the
    rest goes to the job's log."""
    decoded_line = _scrub_secrets(line.strip())
    if not decoded_line:
        return
    # Internal marker: a clip finished its whole chain and this is the file to
    # serve for it. Consumed here like PROXY_BYTES so it never reaches the
    # user's log.
    if decoded_line.startswith("CLIP_READY "):
        try:
            _, index, filename = decoded_line.split(" ", 2)
            if job_id in jobs:
                jobs[job_id].setdefault('ready_files', {})[int(index)] = filename
        except ValueError:
            pass
        return
    if decoded_line.startswith("JOB_CPU "):
        # Internal marker: the job's CPU time (main.py, at exit).
        print(f"📊 [job-cpu] {job_id} {decoded_line[8:]}")
        return
    if decoded_line.startswith("YT_ACCOUNT_LIMITED"):
        # Internal marker from main.py's downloader: the download failed only
        # because YouTube is limiting the account (yt_account). The job goes
        # back in line for when the cooldown is over (_schedule_auto_retry).
        if job_id in jobs:
            jobs[job_id]['yt_limited'] = True
        return
    if decoded_line.startswith("PROXY_BYTES="):
        # Internal marker from main.py's downloader, not a log line.
        try:
            if job_id in jobs:
                jobs[job_id]['proxy_bytes'] = int(decoded_line.split("=", 1)[1])
        except ValueError:
            pass
        return
    if decoded_line.startswith("PROXY_ROUTE="):
        # Internal marker (which download attempt won); not shown to clients.
        return
    print(f"📝 [Job Output] {decoded_line}")
    if job_id in jobs:
        jobs[job_id]['logs'].append(decoded_line)



def enqueue_output(out, job_id):
    """Reads output from a subprocess and appends it to jobs logs."""
    try:
        for line in iter(out.readline, b''):
            _handle_job_line(job_id, line.decode('utf-8', 'replace'))
    except Exception as e:
        print(f"Error reading output for job {job_id}: {e}")
    finally:
        out.close()

def _job_nice():
    """Niceness for a job process and everything it starts (ffmpeg, ASR...).

    A job is batch work: when the host is short of CPU it should yield to the
    API answering a user, editor renders included, which run in this process
    at the normal priority. JOB_NICE=0 turns it off."""
    try:
        return max(0, min(19, int(os.environ.get("JOB_NICE", "10"))))
    except ValueError:
        return 10


def _lower_priority():
    n = _job_nice()
    if n:
        os.nice(n)


def _job_encode_env(env):
    """Job env plus the NVENC encode cap that bin/ffmpeg enforces.

    JOB_NVENC_SLOTS (default 4) job encodes at once on the card, host-wide:
    the lock files live on the shared output/ volume, so both containers of a
    deploy handover count against the same slots. Only job processes get it;
    editor renders in this process stay uncapped, like JOB_NICE. 0 turns it
    off."""
    env = dict(env)
    slots = os.environ.get("JOB_NVENC_SLOTS", "4").strip()
    env["NVENC_SLOTS"] = slots if slots.isdigit() else "4"
    env["NVENC_LOCK_DIR"] = os.path.abspath(OUTPUT_DIR)
    return env


def _refresh_partial_result(job_id, output_dir):
    """While a job runs: the clips main.py has announced so far, so the user
    sees each one as it lands."""
    try:
        json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
        if not json_files:
            return
        target_json = json_files[0]
        # It might be being written to: a partial file fails json.load and
        # the next look reads it whole.
        if os.path.getsize(target_json) <= 0:
            return
        with open(target_json, 'r') as f:
            data = json.load(f)
        clips = data.get('shorts', [])
        cost_analysis = data.get('cost_analysis')
        # Only clips main.py has announced as finished. It names the file
        # itself, so a clip shows up WITH its hook and captions instead of as
        # the bare reframe, and it is never served while ffmpeg is still
        # writing it. A clip whose marker never arrives (it failed) simply
        # stays hidden until the job ends and the result is rebuilt from disk.
        ready_files = (jobs.get(job_id) or {}).get('ready_files') or {}
        ready_clips = []
        for i, clip in enumerate(clips):
            clip_filename = ready_files.get(i)
            if not clip_filename:
                continue
            clip_path = os.path.join(output_dir, clip_filename)
            if os.path.exists(clip_path) and os.path.getsize(clip_path) > 0:
                clip['video_url'] = f"/videos/{job_id}/{clip_filename}"
                ready_clips.append(clip)
        if ready_clips:
            jobs[job_id]['result'] = {'clips': ready_clips, 'cost_analysis': cost_analysis}
    except Exception:
        # Ignore read errors during processing
        pass


def _finish_job(job_id, returncode, output_dir):
    """main.py exited (here or on the remote worker): the job's final status
    and result, from the exit code and what is on disk."""
    if returncode == 0:
        jobs[job_id]['status'] = 'completed'
        jobs[job_id]['logs'].append("Process finished successfully.")

        # Find result JSON
        json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
        if not json_files:
            # Backward-compat rescue if outputs were written to OUTPUT_DIR root
            if _relocate_root_job_artifacts(job_id, output_dir):
                json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
        if json_files:
            target_json = json_files[0]
            with open(target_json, 'r') as f:
                data = json.load(f)

            # Enhance result with video URLs
            base_name = os.path.basename(target_json).replace('_metadata.json', '')
            clips = data.get('shorts', [])
            cost_analysis = data.get('cost_analysis')

            rendered, missing = _clips_actually_rendered(
                job_id, output_dir, base_name, clips)
            if not rendered:
                # Nothing to hand over: fail the job.
                jobs[job_id]['status'] = 'failed'
                jobs[job_id]['logs'].append(
                    "No clips could be rendered from this video.")
            else:
                if missing:
                    jobs[job_id]['logs'].append(
                        f"⚠️ {missing} of {len(clips)} clips failed to render.")
                jobs[job_id]['result'] = {'clips': rendered, 'cost_analysis': cost_analysis}
        else:
            jobs[job_id]['status'] = 'failed'
            jobs[job_id]['logs'].append("No metadata file generated.")
    else:
        jobs[job_id]['status'] = 'failed'
        jobs[job_id]['logs'].append(_scrub_secrets(f"Process failed with exit code {returncode}"))


async def _run_main(job_id, cmd, env, tick=None):
    """Run a main.py command of ``job_id`` to its end and return its exit
    code: output lines into the job's log, the manifest heartbeated, ``tick``
    called every couple of seconds. Cancelled (the server is stopping): the
    child is killed so it cannot keep writing into the job dir while the next
    instance resumes the same job."""
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, # Merge stderr to stdout
        env=env,
        cwd=os.getcwd(),
        preexec_fn=_lower_priority,
    )
    # We need to capture logs in a thread because Popen isn't async
    t_log = threading.Thread(target=enqueue_output, args=(process.stdout, job_id))
    t_log.daemon = True
    t_log.start()

    last_heartbeat = time.time()
    while process.poll() is None:
        try:
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            try:
                process.kill()
            except Exception:
                pass
            raise
        if time.time() - last_heartbeat >= HEARTBEAT_EVERY:
            _touch_manifest(job_id)
            last_heartbeat = time.time()
        if tick is not None:
            tick()
    # The exit code can be read before the reader thread saw the last lines
    # (SOURCE_READY is printed right before the exit).
    await asyncio.to_thread(t_log.join, 5)
    return process.returncode


async def run_job(job_id, job_data):
    """Executes the subprocess for a specific job."""

    cmd = job_data['cmd']
    env = _job_encode_env(job_data['env'])
    output_dir = job_data['output_dir']

    jobs[job_id]['status'] = 'processing'
    jobs[job_id]['logs'].append("Job started by worker.")
    print(f"🎬 [run_job] Executing command for {job_id}: {' '.join(cmd)}")

    try:
        returncode = await _run_main(
            job_id, cmd, env, tick=lambda: _refresh_partial_result(job_id, output_dir))
        _finish_job(job_id, returncode, output_dir)
    except Exception as e:
        jobs[job_id]['status'] = 'failed'
        # Exception text can embed URLs with credentials (e.g. the proxy URL
        # inside a yt-dlp/httpx error) — scrub before it reaches client logs.
        jobs[job_id]['logs'].append(_scrub_secrets(f"Execution error: {str(e)}"))


@app.get("/health")
async def health():
    """Lightweight liveness probe for uptime monitoring."""
    return {"status": "ok"}


@app.get("/health/ready")
async def health_ready():
    """Readiness probe for the Docker HEALTHCHECK (Dockerfile). Traefik's docker
    provider drops a container from the load balancer as soon as it turns
    unhealthy, so answering 503 from the moment SIGTERM arrives pulls this
    instance out of rotation while it can still serve, instead of after its
    socket is gone. Only SIGTERM flips it: a drain triggered by the instance
    marker starts while the new container is still booting, and going
    unready then would leave nobody routable."""
    if _stopping:
        return JSONResponse({"status": "stopping"}, status_code=503)
    return {"status": "ready"}

@app.get("/api/config")
async def get_config():
    return {
        "youtubeUrlEnabled": not DISABLE_YOUTUBE_URL,
        "billingEnabled": False,
        "googleAuthEnabled": False,
        "jobRetentionSeconds": JOB_RETENTION_SECONDS,
        # Self-host only: tells the dashboard the Gemini key is optional
        # because the moment picker runs on an OpenAI-compatible server.
        "localLlm": llm_backend.describe(),
        # Self-host only: clips come from the user's own agent, so the dashboard asks for no key.
        "agentClips": AGENT_CLIPS,
    }

async def _probe_youtube_quality(url: str) -> dict:
    """What the source offers ({max_height, duration, ...}); {} on any failure
    (fail-open). The metering probe a moment ago already asked YouTube for
    this very video: its answer is reused, so a submission costs the account
    one request here instead of two. Otherwise quality_probe.py runs in a
    worker thread."""
    try:
        facts = None
    except Exception:
        facts = None
    if facts:
        return facts

    def _run():
        try:
            proc = subprocess.run(
                [sys.executable, QUALITY_PROBE_SCRIPT, "--url", url],
                capture_output=True, timeout=75, env=child_env(),
            )
            return json.loads(proc.stdout.decode(errors="replace").strip() or "{}")
        except Exception as e:
            print(f"⚠️ Quality probe failed ({e}); starting job without gate.")
            return {}

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


async def _validate_source_url(url: str):
    """400 unless ``url`` is safe to hand to yt-dlp: http(s) on a globally
    routable host (security_utils) and, on YouTube, one video rather than a
    search / playlist / channel page (yt_clients). The download and the
    metering probe check the same things again; this is the one in front of
    the quality probe, which used to have none."""
    from security_utils import assert_public_url, UnsafeURLError
    from yt_clients import youtube_non_video_reason
    try:
        await asyncio.get_event_loop().run_in_executor(None, assert_public_url, url)
    except UnsafeURLError as e:
        raise HTTPException(status_code=400, detail=f"This URL can't be processed: {e}")
    reason = youtube_non_video_reason(url)
    if reason:
        raise HTTPException(status_code=400, detail=(
            f"This link is {reason}. Paste the link of one video "
            "(youtube.com/watch?v=... or youtu.be/...)."))


async def _quality_gate(url: str, force_low: bool):
    """Run the pre-flight probe: raises 400 for a too-short source, returns
    the needs_confirmation response for a low-resolution one, else None."""
    probe = await _probe_youtube_quality(url)
    # Hard reject, no confirm-and-retry: a too-short source fails the same
    # way on every retry, so letting the user force it just burns the job.
    source_duration = int(probe.get("duration") or 0)
    if MIN_SOURCE_SECONDS > 0 and 0 < source_duration < MIN_SOURCE_SECONDS:
        _reject_short_source(source_duration)
    max_height = int(probe.get("max_height") or 0)
    if not force_low and QUALITY_GATE_MIN_HEIGHT > 0 \
            and 0 < max_height < QUALITY_GATE_MIN_HEIGHT:
        print(f"⚠️ Quality gate: only {max_height}p available for {url} — asking user first.")
        return JSONResponse({
            "needs_confirmation": True,
            "quality_check": {
                "max_height": max_height,
                "min_height": QUALITY_GATE_MIN_HEIGHT,
                "cookies_invalid": bool(probe.get("cookies_invalid")),
            },
        })
    return None


MAX_UPLOAD_NAME_BYTES = 120


def _clamp_upload_name(name: str, max_bytes: int = MAX_UPLOAD_NAME_BYTES) -> str:
    """Shorten a client-supplied file name so "<id>_<name>" fits the 255-byte
    limit most filesystems put on a name. The limit is in BYTES, not characters,
    and emoji or math-alphabet letters take 4 each. The extension is kept and
    the cut never lands inside a multi-byte character."""
    if len(name.encode("utf-8")) <= max_bytes:
        return name
    stem, ext = os.path.splitext(name)
    if len(ext.encode("utf-8")) > 16:
        stem, ext = name, ""
    room = max_bytes - len(ext.encode("utf-8"))
    stem = stem.encode("utf-8")[:room].decode("utf-8", "ignore").rstrip()
    return (stem or "upload") + ext


def _media_duration_seconds(path: str) -> float:
    """Container duration via ffprobe; 0.0 on any failure (fail-open)."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, timeout=30)
        return float(proc.stdout.decode().strip() or 0)
    except Exception:
        return 0.0


def _reject_short_source(duration: float):
    raise HTTPException(status_code=400, detail=(
        f"This video is only {int(duration)}s long — clip generation needs at "
        f"least {MIN_SOURCE_SECONDS}s of material to cut from. It already is "
        f"short-form content."))


# Layouts the caller can let the renderer choose from, mapped to the env var
# each one is gated on. The renderer only ever picks between layouts that are
# switched on here.
#
# This is opt-in per job, not a detector running on every video, because the
# detection is not good enough to be trusted unprompted: measured over the
# 48-clip corpus, routing every video through the on-screen-content check fixed
# 13 clips and spoiled 13 others (talking heads and corner tickers demoted to a
# layout they do not need). Asking the person who knows what they uploaded costs
# them one click and removes that whole class of error. It is also what OpusClip
# does — its "applicable auto layout" panel lets the user pick which layouts the
# AI may apply.
LAYOUT_ENV = {
    "split": "SPLIT_LAYOUT",          # two speakers stacked
    "screencast": "SCREENCAST_LAYOUT",  # slides/screen share over the speaker
    "speaker_cut": "SPEAKER_CUT",     # hard cuts to whoever is talking
    "punch_in": "PUNCH_IN",           # small push on the clip's beats
}

# Stacking and cutting both need to know who is speaking.
LAYOUT_IMPLIES = {
    "split": ["SPEAKER_SIGNAL"],
    "speaker_cut": ["SPEAKER_SIGNAL"],
}


# --------------------------------------------------------------------------- #
# Agent uploads: a two-step path for callers that hold a video FILE, not a URL
# (an MCP client handed the file by the user). POST reserves an id and returns
# a PUT URL; the client streams the raw bytes there with no auth beyond the
# unguessable id (so `curl -T` works from any agent runtime); /api/process then
# takes the upload_id. Files live in UPLOAD_DIR under the same retention sweep
# as every other source upload, and the owner recorded at POST is checked at
# process time so a leaked id cannot start a job on someone else's account.
# --------------------------------------------------------------------------- #
pending_uploads: Dict[str, Dict] = {}
# Unconsumed slots are gone after this; a consumed one becomes the job's
# input and follows the job's own retention instead.
UPLOAD_TTL_SECONDS = int(os.environ.get("UPLOAD_TTL_SECONDS", str(6 * 3600)))


def _upload_url_base(request):
    return os.environ.get("PUBLIC_API_URL", "").rstrip("/") or str(request.base_url).rstrip("/")


@app.post("/api/uploads")
async def create_upload(request: Request):
    """Reserve an upload slot. Body (JSON, optional): {"filename": "..."}."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    filename = _clamp_upload_name(os.path.basename(str((body or {}).get("filename") or "video.mp4")) or "video.mp4")
    upload_id = str(uuid.uuid4())
    pending_uploads[upload_id] = {
        "filename": filename,
        "path": os.path.join(UPLOAD_DIR, f"pending_{upload_id}_{filename}"),
        "created": time.time(),
        "bytes": 0,
        "complete": False,
    }
    return {
        "upload_id": upload_id,
        "upload_url": f"{_upload_url_base(request)}/api/uploads/{upload_id}",
        "method": "PUT",
        "max_mb": MAX_FILE_SIZE_MB,
        "expires_in": UPLOAD_TTL_SECONDS,
        "hint": "PUT the raw video bytes to upload_url (e.g. curl -T video.mp4 <upload_url>), "
                "then call /api/process (or the process_video tool) with this upload_id. "
                "The slot and file are deleted after expires_in seconds if unused, or "
                "DELETE this URL to drop them sooner.",
    }


@app.put("/api/uploads/{upload_id}")
async def put_upload(upload_id: str, request: Request):
    """Receive the raw video body for a reserved slot. Streams to disk, capped
    at MAX_FILE_SIZE_MB; a second PUT replaces the first."""
    slot = pending_uploads.get(upload_id)
    if not slot or time.time() - slot["created"] > UPLOAD_TTL_SECONDS:
        pending_uploads.pop(upload_id, None)
        raise HTTPException(status_code=404, detail="Unknown or expired upload_id")
    limit_bytes = MAX_FILE_SIZE_MB * 1024 * 1024
    size = 0
    with open(slot["path"], "wb") as out:
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit_bytes:
                out.close()
                os.remove(slot["path"])
                raise HTTPException(status_code=413, detail=f"File too large. Max size {MAX_FILE_SIZE_MB}MB")
            out.write(chunk)
    if size == 0:
        os.remove(slot["path"])
        raise HTTPException(status_code=400, detail="Empty body")
    slot.update({"bytes": size, "complete": True})
    duration = await asyncio.get_event_loop().run_in_executor(None, _media_duration_seconds, slot["path"])
    if duration <= 0:
        os.remove(slot["path"])
        slot["complete"] = False
        raise HTTPException(status_code=400, detail="The body is not a readable video file")
    return {"upload_id": upload_id, "bytes": size, "duration_seconds": round(duration, 1),
            "hint": "Now call /api/process with upload_id."}


@app.delete("/api/uploads/{upload_id}")
async def delete_upload(upload_id: str, request: Request):
    """Drop a slot and its file before it expires (owner only in cloud mode)."""
    slot = pending_uploads.get(upload_id)
    if not slot:
        raise HTTPException(status_code=404, detail="Unknown or expired upload_id")
    pending_uploads.pop(upload_id, None)
    try:
        os.remove(slot["path"])
    except OSError:
        pass
    return {"deleted": upload_id}


def _sweep_pending_uploads(now=None):
    """Expire agent upload slots older than UPLOAD_TTL_SECONDS (file included).
    Returns the ids removed. Called from the cleanup loop; pure enough to test."""
    now = now or time.time()
    gone = []
    for uid, slot in list(pending_uploads.items()):
        if now - slot["created"] > UPLOAD_TTL_SECONDS:
            pending_uploads.pop(uid, None)
            try:
                os.remove(slot["path"])
            except OSError:
                pass
            gone.append(uid)
    return gone


def _take_pending_upload(upload_id):
    """The completed upload for this caller, or an HTTPException."""
    slot = pending_uploads.get(upload_id)
    if not slot:
        raise HTTPException(status_code=404, detail="Unknown or expired upload_id")
    if not slot.get("complete") or not os.path.exists(slot["path"]):
        raise HTTPException(status_code=409, detail="Upload not received yet: PUT the video to upload_url first")
    return slot


def layout_env(requested):
    """Env overrides for the layouts this job allows. Unknown names are ignored
    rather than rejected: a newer dashboard must not break an older API.

    The special value "auto" hands the choice to Gemini (one call per video).
    It composes with explicit picks: layout_picker only ever adds, so asking for
    "auto,punch_in" means "decide the layout yourself, and punch in regardless".
    "none" is the opposite: it switches the picker OFF for this job even when
    the deployment runs with AUTO_LAYOUT=1, for the user who wants the plain
    single crop and nothing clever.
    """
    env = {}
    for name in requested or []:
        key = str(name).strip().lower()
        if key == "auto":
            env["AUTO_LAYOUT"] = "1"
            continue
        if key == "none":
            env["AUTO_LAYOUT"] = "0"
            continue
        var = LAYOUT_ENV.get(key)
        if not var:
            continue
        env[var] = "1"
        for extra in LAYOUT_IMPLIES.get(key, []):
            env[extra] = "1"
    return env


@app.post("/api/process")
async def process_endpoint(
    request: Request,
    file: Optional[UploadFile] = File(None),
    url: Optional[str] = Form(None),
    acknowledged: Optional[str] = Form(None),
    output_format: Optional[str] = Form(None),
    layouts: Optional[str] = Form(None),
    force_low_quality: Optional[str] = Form(None),
    webhook_url: Optional[str] = Form(None),
    webhook_secret: Optional[str] = Form(None),
    target_clips: Optional[str] = Form(None),
    clip_min_seconds: Optional[str] = Form(None),
    clip_max_seconds: Optional[str] = Form(None),
    auto_hook: Optional[str] = Form(None),
    auto_hook_style: Optional[str] = Form(None),
    captions: Optional[str] = Form(None),
    upload_id: Optional[str] = Form(None),
    banner_id: Optional[str] = Form(None),
    banner_position: Optional[str] = Form(None),
    project_id: Optional[str] = Form(None),
):
    api_key = await resolve_gemini(request)
    if not api_key and not AGENT_CLIPS and not llm_backend.active():
        # Self-host with an OpenAI-compatible server configured needs no
        # Google key for the core pipeline: the moment picker runs there and
        # the frame-based stages degrade on their own (layout_picker returns
        # "none", silent videos fail with a message that says why).
        raise gemini_missing_error()

    ack_flag = str(acknowledged).lower() in ("1", "true", "yes")
    force_low = str(force_low_quality).lower() in ("1", "true", "yes")

    # Handle JSON body manually for URL payload
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
        url = body.get("url")
        ack_flag = bool(body.get("acknowledged"))
        force_low = bool(body.get("force_low_quality"))
        output_format = body.get("output_format")
        layouts = body.get("layouts")
        webhook_url = body.get("webhook_url")
        webhook_secret = body.get("webhook_secret")
        target_clips = body.get("target_clips")
        clip_min_seconds = body.get("clip_min_seconds")
        clip_max_seconds = body.get("clip_max_seconds")
        auto_hook = body.get("auto_hook")
        auto_hook_style = body.get("auto_hook_style")
        captions = body.get("captions")
        upload_id = body.get("upload_id")

    # Normalize output format (auto = keep pipeline default).
    if output_format not in ("vertical", "horizontal", "square"):
        output_format = "auto"

    # Accepts a JSON list or a comma-separated form field.
    if isinstance(layouts, str):
        layouts = [p for p in layouts.split(",") if p.strip()]
    elif not isinstance(layouts, list):
        layouts = []

    # Agent upload (POST /api/uploads + PUT): the file is already on disk.
    upload_slot = None
    if upload_id and not url and not file:
        upload_slot = _take_pending_upload(upload_id)

    if not url and not file and not upload_slot:
        raise HTTPException(status_code=400, detail="Must provide URL, File or upload_id")

    # Completion callback: reject unsafe targets NOW (clear 400) — delivery
    # re-validates anyway, but failing at submit is the debuggable behavior.
    if webhook_url:
        from security_utils import assert_public_url, UnsafeURLError
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, assert_public_url, webhook_url)
        except UnsafeURLError as e:
            raise HTTPException(status_code=400, detail=f"Invalid webhook_url: {e}")

    if not ack_flag:
        raise HTTPException(status_code=400, detail="You must confirm you own the content or have rights to process it.")

    if url and DISABLE_YOUTUBE_URL:
        raise HTTPException(status_code=403, detail="YouTube URL ingest is disabled on this deployment. Please upload a file you own.")

    # Refuse a URL no server-side fetch may touch before anything fetches it:
    # the quality probe below used to run yt-dlp on it unvalidated (tailnet
    # hosts included) and ahead of the balance / rate checks.
    if url:
        await _validate_source_url(url)

    # Capture attestation context for legal record (IP + timestamp + UA)
    client_ip = request.client.host if request.client else "unknown"
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        client_ip = fwd.split(",")[0].strip()
    user_agent = request.headers.get("user-agent", "")
    attestation = {
        "acknowledged": True,
        "ip": client_ip,
        "user_agent": user_agent,
        "timestamp": time.time(),
        "source": "upload_id" if upload_slot else "url" if url else "file",
    }

    job_id = str(uuid.uuid4())
    job_output_dir = os.path.join(OUTPUT_DIR, job_id)
    os.makedirs(job_output_dir, exist_ok=True)

    # Prepare Command
    # sys.executable, not "python": bare "python" resolves against PATH, which
    # outside Docker is whatever interpreter happens to be first — not the venv
    # running this server. Every job then dies on `import cv2`. The quality
    # probe above already gets this right.
    cmd = [sys.executable, "-u", "main.py"] # -u for unbuffered
    env = child_env()
    if api_key:
        env["GEMINI_API_KEY"] = api_key # Override with key from request
    else:
        env.pop("GEMINI_API_KEY", None)  # local-LLM job: main.py must not find a stale key
    # The stdio fix above only covers this process. main.py prints an emoji on
    # its first line and configures nothing, so on a cp1252 console the child
    # still dies before it renders anything -- the server starts and every job
    # fails instead. setdefault, so an explicit PYTHONIOENCODING still wins.
    env.setdefault("PYTHONIOENCODING", "utf-8")

    # Optional layouts are per job. The renderer reads these at import time in
    # the subprocess, so they must be set before Popen — same path WATERMARK
    # already takes.
    chosen = layout_env(layouts)
    env.update(chosen)
    if chosen:
        print(f"[layouts] job={job_id} enabled={sorted(chosen)}")

    # Auto-hook: burn each clip's Gemini hook text during the render. Off when
    # the field is absent, so API/MCP/webhook callers keep their old output
    # byte-for-byte; the dashboard sends an explicit value either way.
    if str(auto_hook).lower() in ("1", "true", "yes"):
        env["AUTO_HOOK"] = "1"
        from hooks import HOOK_STYLES
        if auto_hook_style in HOOK_STYLES:
            env["AUTO_HOOK_STYLE"] = auto_hook_style
        print(f"[auto-hook] job={job_id} style={env.get('AUTO_HOOK_STYLE', 'classic')}")

    # Brand banner picked at submit time; unknown ids are ignored, not trusted.
    import brand_banner
    import projects
    project = projects.get(project_id)
    if project:
        env["PROJECT_ID"] = project["id"]
        if not banner_id and project["banner_id"]:  # the project's banner, unless the request picked one
            banner_id, banner_position = project["banner_id"], project["banner_position"]
    if brand_banner.get(banner_id):
        env["BANNER_ID"] = banner_id
        if banner_position in brand_banner.POSITIONS:
            env["BANNER_POSITION"] = banner_position

    # Manual generation controls (discussion #65): optional clip-count target
    # and duration band, forwarded to the selection prompts via the same env
    # overrides the A/B harness already reads (clip_selection.py). All three
    # are honest TARGETS, not guarantees — the model may return fewer clips
    # when the material doesn't hold them. Bad values 400 instead of silently
    # producing something the user didn't ask for.
    def _gen_control(raw, name, lo, hi, integer=False):
        if raw in (None, ""):
            return None
        try:
            val = float(raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"{name} must be a number")
        if integer and val != int(val):
            raise HTTPException(status_code=400, detail=f"{name} must be an integer")
        if not (lo <= val <= hi):
            raise HTTPException(status_code=400,
                                detail=f"{name} must be between {lo:g} and {hi:g}")
        return int(val) if integer else val

    n_clips = _gen_control(target_clips, "target_clips", 1, 15, integer=True)
    min_secs = _gen_control(clip_min_seconds, "clip_min_seconds", 5, 175)
    max_secs = _gen_control(clip_max_seconds, "clip_max_seconds", 10, 180)
    if min_secs is not None and max_secs is not None and max_secs < min_secs + 5:
        raise HTTPException(status_code=400,
                            detail="clip_max_seconds must be at least 5s above clip_min_seconds")
    if n_clips is not None:
        env["CLIP_TARGET_MIN"] = env["CLIP_TARGET_MAX"] = str(n_clips)
    if min_secs is not None:
        env["CLIP_MIN_SECONDS"] = str(min_secs)
    if max_secs is not None:
        env["CLIP_MAX_SECONDS"] = str(max_secs)
    if n_clips is not None or min_secs is not None or max_secs is not None:
        print(f"[gen-controls] job={job_id} clips={n_clips} band={min_secs}-{max_secs}")

    # captions=false: the source already carries burned-in subtitles (or the
    # caller adds its own later), so skip the free auto-caption pass instead
    # of stacking a second layer. Absent → the deployment default (on).
    if captions is not None and str(captions).lower() in ("0", "false", "no"):
        env["AUTO_CAPTIONS"] = "0"
        print(f"[captions] job={job_id} auto-captions off")

    input_path = None
    if url:
        # Keep the downloaded source inside the job dir: the clip editor's
        # re-render path cuts new segments from it, and it ages out with the
        # rest of the job (retention window + OUTPUT_MAX_GB cap) either way.
        cmd.extend(["-u", url, "--keep-original"])
    elif upload_slot:
        # Move the pre-uploaded file under the job's name so it is cleaned up
        # with the job like any other upload; the slot is consumed.
        src = upload_slot["path"]
        src_duration = _media_duration_seconds(src)
        if MIN_SOURCE_SECONDS > 0 and 0 < src_duration < MIN_SOURCE_SECONDS:
            shutil.rmtree(job_output_dir, ignore_errors=True)
            _reject_short_source(src_duration)
        input_path = os.path.join(UPLOAD_DIR, f"{job_id}_{upload_slot['filename']}")
        os.replace(src, input_path)
        pending_uploads.pop(upload_id, None)
        cmd.extend(["-i", input_path])
    else:
        # Save uploaded file with size limit check.
        # basename() strips any path components from the client-supplied
        # filename so a name like "../../main.py" can't escape UPLOAD_DIR.
        safe_name = _clamp_upload_name(os.path.basename(file.filename or "upload") or "upload")
        input_path = os.path.join(UPLOAD_DIR, f"{job_id}_{safe_name}")

        # Read file in chunks to check size
        size = 0
        limit_bytes = MAX_FILE_SIZE_MB * 1024 * 1024

        with open(input_path, "wb") as buffer:
            while content := await file.read(1024 * 1024): # Read 1MB chunks
                size += len(content)
                if size > limit_bytes:
                    os.remove(input_path)
                    shutil.rmtree(job_output_dir)
                    raise HTTPException(status_code=413, detail=f"File too large. Max size {MAX_FILE_SIZE_MB}MB")
                buffer.write(content)

        upload_duration = _media_duration_seconds(input_path)
        if MIN_SOURCE_SECONDS > 0 and 0 < upload_duration < MIN_SOURCE_SECONDS:
            os.remove(input_path)
            shutil.rmtree(job_output_dir, ignore_errors=True)
            _reject_short_source(upload_duration)

        cmd.extend(["-i", input_path])

    cmd.extend(["-o", job_output_dir])
    if output_format and output_format != "auto":
        cmd.extend(["--format", output_format])

    print(f"[attestation] job={job_id} ip={attestation['ip']} source={attestation['source']} ack=true")

    # Pre-flight quality gate: probe the offered resolution BEFORE starting, so
    # the user can abort (refresh cookies / update yt-dlp) instead of burning
    # 20 min on a 360p-only source. Fail-open: any probe error starts normally.
    # The probe also runs under force_low_quality so the short-source check
    # can't be bypassed through the quality-gate confirm. It runs AFTER the
    # metering step (entitlement, job limit, balance, the hourly probe cap), so
    # an account with no minutes cannot use it as a free yt-dlp runner; a
    # rejection hands the reservation back.
    if url and (QUALITY_GATE_MIN_HEIGHT > 0 or MIN_SOURCE_SECONDS > 0):
        try:
            gate = await _quality_gate(url, force_low)
        except BaseException:
            shutil.rmtree(job_output_dir, ignore_errors=True)
            raise
        if gate is not None:
            shutil.rmtree(job_output_dir, ignore_errors=True)
            return gate

    if url:
        # The probe already learned whether this video is bot-checked on every
        # static IP; the download skips those attempts then (DOWNLOAD_SKIP_STATICS).
        skip_statics = bool(getattr(request.state, "skip_statics", False))
    else:
        skip_statics = False
    if skip_statics:
        env["DOWNLOAD_SKIP_STATICS"] = "1"
    else:
        env.pop("DOWNLOAD_SKIP_STATICS", None)

    # Absolute-URL base for the webhook payload: explicit env wins (the API may
    # sit behind a proxy whose forwarded headers we can't trust), else what the
    # caller connected to.
    api_base = os.environ.get("PUBLIC_API_URL", "").rstrip("/") or str(request.base_url).rstrip("/")

    # Enqueue Job
    jobs[job_id] = {
        'status': 'queued',
        'logs': _TimedLog([f"Job {job_id} queued."]),
        'cmd': cmd,
        'env': env,
        'output_dir': job_output_dir,
        'attestation': attestation,
        'webhook_url': webhook_url,
        'webhook_secret': webhook_secret,
        'base_url': api_base,
        'submitted_at': _arrival_now(),
    }

    # Resume manifest: enough to re-run this job if the container dies mid-flight
    # (a redeploy). No secrets: the env is rebuilt from os.environ on resume.
    _write_resume_manifest(job_id, cmd, webhook_url=webhook_url, webhook_secret=webhook_secret,
                           base_url=api_base, skip_statics=skip_statics,
                           submitted_at=jobs[job_id]['submitted_at'])

    _enqueue_job(job_id)

    return {"job_id": job_id, "status": "queued"}

def _job_view_from_disk(job_id):
    """What the disk says about a job this instance does not hold in memory.

    During a deploy handover a poll can land on either instance; the one that
    did not accept the job must still answer from the shared directory: a
    metadata JSON means completed (recovered like at startup), a manifest
    means the other instance has it (heartbeat) or will pick it up (queued).
    """
    job_path = os.path.join(OUTPUT_DIR, job_id)
    if not os.path.isdir(job_path):
        return None
    m = _read_manifest(job_id)
    if m is None:
        if glob.glob(os.path.join(job_path, "*_metadata.json")):
            _recover_jobs_from_disk()
            return jobs.get(job_id)
        return None
    alive = time.time() - float(m.get("heartbeat") or 0) < HEARTBEAT_STALE_AFTER
    return {
        # Rendered, delivery pending: the next instance finishes it, no queue.
        'status': 'processing' if alive or m.get("stage") == "delivering" else 'queued',
        'logs': _TimedLog(["♻️ The server was updated; your video continues on the new instance."]),
        'result': None,
    }


def _presented_status(job_id, job):
    """A job we hold as 'queued' while draining is really the next instance's:
    if it has started it, say so instead of showing a queue that never moves."""
    if job.get('status') == 'queued' and _draining:
        m = _read_manifest(job_id)
        if m and _manifest_busy_elsewhere(m):
            return 'processing'
    if job.get('delivering'):
        # main.py is done but the clips are not archived/unmarked yet: not
        # finished for the user until the delivery says so.
        return 'processing'
    return job['status']


@app.get("/api/status/{job_id}")
async def get_status(job_id: str, request: Request):
    job = jobs.get(job_id)
    if job is None:
        job = _job_view_from_disk(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    _logs_view = _visible_logs_timed(job['logs'])
    return {
        "status": _presented_status(job_id, job),
        "logs": _logs_view[0],
        # When each line arrived (epoch seconds, parallel to "logs"), or None.
        "log_times": _logs_view[1],
        "result": job.get('result'),
        # Position in line and a rough wait while the job is still queued.
        "queue": queue_snapshot(job_id),
    }


def _locate_source(job_id: str):
    """Find a job's source video on disk, or None.

    Upload jobs keep it in uploads/{job_id}_*; URL jobs keep the download in
    the job dir (--keep-original) under the name recorded as ``source_video``
    in metadata.json. Either way it ages out with the normal retention caps.
    """
    matches = [
        f for f in glob.glob(os.path.join(UPLOAD_DIR, f"{glob.escape(job_id)}_*"))
        if not os.path.basename(f).startswith("thumb_")
    ]
    if matches:
        return matches[0]
    try:
        meta_files = glob.glob(os.path.join(OUTPUT_DIR, job_id, "*_metadata.json"))
        if meta_files:
            with open(meta_files[0], 'r') as f:
                name = json.load(f).get('source_video')
            if name:
                candidate = os.path.join(OUTPUT_DIR, job_id, os.path.basename(name))
                if os.path.exists(candidate):
                    return candidate
    except Exception:
        pass
    return None


def _job_record(job_id: str):
    """The in-memory job record, or what the shared disk says about it."""
    return jobs.get(job_id) or _job_view_from_disk(job_id)


def _signed_source_url(job_id: str) -> str:
    """The URL to hand a `<video>` tag for this job's source."""
    return f"/api/source/{job_id}"


@app.get("/api/source-url/{job_id}")
async def get_source_url(job_id: str, request: Request):
    """The URL to hand a `<video>` tag for this job's source."""
    return {"url": _signed_source_url(job_id)}


@app.get("/api/source/{job_id}")
async def get_source_video(job_id: str, request: Request):
    """Stream a job's original source video for the live-analysis preview and
    the clip editor's source monitor.

    Uploaded sources are blob URLs in the browser and don't survive a reload,
    so the recovered session points the preview here instead.

    This one endpoint serves the untouched original, which for a URL job is the
    file we downloaded.
    """
    source_path = _locate_source(job_id)
    if not source_path:
        raise HTTPException(status_code=404, detail="Source not found")
    return FileResponse(source_path, media_type="video/mp4")


@app.get("/api/jobs/{job_id}/download-all")
async def download_all_clips(job_id: str, request: Request):
    """Bundle the current version of every clip of a job into one ZIP."""
    output_dir = os.path.join(OUTPUT_DIR, job_id)
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
    if not json_files:
        raise HTTPException(status_code=404, detail="Job not found")

    with open(json_files[0], 'r', encoding='utf-8') as f:
        data = json.load(f)

    # The metadata file on disk never carries video_url — the pipeline doesn't
    # write it, it's injected into the in-memory job record. So prefer the live
    # record (it also tracks edits like subtitled_/hook_ renames) and fall back
    # to the canonical name a job/restore rebuilds, instead of finding nothing.
    base_name = os.path.basename(json_files[0]).replace('_metadata.json', '')
    mem_clips = ((jobs.get(job_id) or {}).get('result') or {}).get('clips') or []

    files = []
    for i, clip in enumerate(data.get('shorts', [])):
        url = None
        if i < len(mem_clips):
            url = (mem_clips[i] or {}).get('video_url')
        url = url or clip.get('video_url')
        filename = (os.path.basename(url.split('/')[-1]) if url
                    else _canonical_clip_file(output_dir, base_name, i))
        path = os.path.join(output_dir, filename)
        if filename and os.path.exists(path):
            files.append((i, path))

    if not files:
        raise HTTPException(status_code=404, detail="No clip files found for this job")

    # One file per request: two downloads of the same job in the same second
    # used to share a timestamped name, so the second truncated the ZIP the
    # first was still streaming and the first cleanup deleted it under the second.
    zip_path = os.path.join(output_dir, f"clips_{uuid.uuid4().hex}.zip")

    def build_zip():
        # Videos are already compressed; store instead of deflate for speed.
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_STORED) as zf:
            for i, path in files:
                zf.write(path, arcname=f"clip_{i + 1:02d}_{os.path.basename(path)}")

    def remove_zip():
        with suppress(FileNotFoundError):
            os.remove(zip_path)

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, build_zip)
    except BaseException:
        remove_zip()
        raise

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"openshorts_clips_{job_id[:8]}.zip",
        background=BackgroundTask(remove_zip),
    )


# Job ids are uuid4 strings.
_JOB_ID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


from editor import VideoEditor
from subtitles import generate_srt, generate_ass, burn_subtitles, generate_srt_from_video, CAPTION_PRESETS
from hooks import add_hook_to_video

class EditRequest(BaseModel):
    job_id: str
    clip_index: int
    api_key: Optional[str] = None
    input_filename: Optional[str] = None

@app.post("/api/edit")
async def edit_clip(
    req: EditRequest,
    request: Request,
):
    # Cloud (paid) mode disables BYOK: ignore any body api_key so it can't skip
    # the entitlement gate or metering (mirrors resolve_gemini ignoring the
    # header). Self-host keeps BYOK — the body key wins there.
    body_key = req.api_key
    final_api_key = body_key or await resolve_gemini(request)

    if not final_api_key:
        raise gemini_missing_error()

    if req.job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[req.job_id]
    if 'result' not in job or 'clips' not in job['result']:
        raise HTTPException(status_code=400, detail="Job result not available")

    try:
        # Resolve Input Path: Prefer explict input_filename from frontend (chaining edits)
        if req.input_filename:
            # Security: Ensure just a filename, no paths
            safe_name = os.path.basename(req.input_filename)
            input_path = os.path.join(OUTPUT_DIR, req.job_id, safe_name)
            filename = safe_name
        else:
            # Fallback to original clip
            clip = job['result']['clips'][req.clip_index]
            filename = clip['video_url'].split('/')[-1]
            input_path = os.path.join(OUTPUT_DIR, req.job_id, filename)
        
        if not os.path.exists(input_path):
             raise HTTPException(status_code=404, detail=f"Video file not found: {input_path}")

        # Edit the clip WITHOUT its burned captions, then put them back on top —
        # otherwise the captions are baked into the edit and the next subtitle
        # pass stacks a second layer over them (see _reapply_captions).
        clean_name = await asyncio.to_thread(
            _strip_burned_captions, os.path.join(OUTPUT_DIR, req.job_id), filename)
        had_captions = clean_name != filename
        if had_captions:
            filename = clean_name
            input_path = os.path.join(OUTPUT_DIR, req.job_id, clean_name)

        # Define output path for edited video
        edited_filename = f"edited_{filename}"
        output_path = os.path.join(OUTPUT_DIR, req.job_id, edited_filename)
        
        # Run editing in a thread to avoid blocking main loop
        # Since VideoEditor uses blocking calls (subprocess, API wait)
        def run_edit():
            editor = VideoEditor(api_key=final_api_key)
            
            # SAFE FILE RENAMING STRATEGY (Avoid UnicodeEncodeError in Docker)
            # Create a safe ASCII filename in the same directory
            safe_filename = f"temp_input_{req.job_id}.mp4"
            safe_input_path = os.path.join(OUTPUT_DIR, req.job_id, safe_filename)
            
            # Copy original file to safe path
            # (Copy is safer than rename if something crashes, we keep original)
            shutil.copy(input_path, safe_input_path)
            
            try:
                # 1. Upload (using safe path)
                vid_file = editor.upload_video(safe_input_path)
                
                # 2. Get duration
                import cv2
                cap = cv2.VideoCapture(safe_input_path)
                fps = cap.get(cv2.CAP_PROP_FPS)
                frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                duration = frame_count / fps if fps else 0
                cap.release()
                
                # Load transcript from metadata
                transcript = None
                try:
                    meta_files = glob.glob(os.path.join(OUTPUT_DIR, req.job_id, "*_metadata.json"))
                    if meta_files:
                        with open(meta_files[0], 'r') as f:
                            data = json.load(f)
                            transcript = data.get('transcript')
                except Exception as e:
                    print(f"⚠️ Could not load transcript for editing context: {e}")

                # 3. Get Plan (Filter String)
                # Zooms would crop burned-in captions/hooks off screen, so tell
                # the editor when the source already carries them. `filename` is
                # the original clip name (safe_input_path is an ASCII temp copy).
                has_captions = ("subtitled_" in filename) or ("hook_" in filename) or ("hooked_" in filename)
                filter_data = editor.get_ffmpeg_filter(vid_file, duration, fps=fps, width=width, height=height, transcript=transcript, has_captions=has_captions)
                
                # 4. Apply
                # Use safe output name first
                safe_output_path = os.path.join(OUTPUT_DIR, req.job_id, f"temp_output_{req.job_id}.mp4")
                editor.apply_edits(safe_input_path, safe_output_path, filter_data)
                
                # Move result to final destination (rename works even if dest name has unicode if filesystem supports it, 
                # but python might still struggle if locale is broken? No, os.rename usually handles it better than subprocess args)
                # Actually, output_path is defined above: f"edited_{filename}"
                # If filename has unicode, output_path has unicode.
                # Let's hope shutil.move / os.rename works.
                if os.path.exists(safe_output_path):
                    shutil.move(safe_output_path, output_path)
                
                return filter_data
            finally:
                # Cleanup temp safe input
                if os.path.exists(safe_input_path):
                    os.remove(safe_input_path)

        # Run in thread pool
        loop = asyncio.get_event_loop()
        plan = await loop.run_in_executor(None, run_edit)

        # Captions back on top, so the clip the user sees keeps them and the
        # clean edited file stays available for a later restyle.
        if had_captions:
            recap = await loop.run_in_executor(
                None, _reapply_captions, req.job_id, req.clip_index, output_path)
            if recap:
                edited_filename = os.path.basename(recap)

        new_video_url = f"/videos/{req.job_id}/{edited_filename}"

        # Persist the new current file like /api/subtitle does: in-memory job
        # result + metadata.json, so reload/recovery/re-archive see this version.
        if req.clip_index < len(job['result']['clips']):
            job['result']['clips'][req.clip_index]['video_url'] = new_video_url
        try:
            meta_files = glob.glob(os.path.join(OUTPUT_DIR, req.job_id, "*_metadata.json"))
            if meta_files:
                with open(meta_files[0], 'r') as f:
                    meta = json.load(f)
                shorts = meta.get('shorts', [])
                if req.clip_index < len(shorts):
                    shorts[req.clip_index]['video_url'] = new_video_url
                    meta['shorts'] = shorts
                    write_json_atomic(meta_files[0], meta, indent=4)
        except Exception as e:
            print(f"⚠️ Failed to update metadata.json: {e}")


        return {
            "success": True,
            "new_video_url": new_video_url,
            "edit_plan": plan
        }

    except Exception as e:
        print(f"❌ Edit Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

class CaptionWordIn(BaseModel):
    """One user-edited caption word, clip-relative ms — the same shape the
    /transcript endpoint hands the subtitle modal."""
    text: str
    startMs: int
    endMs: int


class SubtitleRequest(BaseModel):
    job_id: str
    clip_index: int
    position: str = "bottom" # top, middle, bottom
    font_size: int = 16
    font_name: str = "Verdana"
    font_color: str = "#FFFFFF"
    border_color: str = "#000000"
    border_width: int = 2
    bg_color: str = "#000000"
    bg_opacity: float = 0.0
    style: str = "classic"  # classic (uniform color) or karaoke (word highlight)
    highlight_color: str = "#FFD700"
    effect: str = "none"  # none | glow | pop | box (karaoke only)
    base_opacity: float = 1.0  # opacity of non-active words (dimmed modern look)
    uppercase: bool = False
    reveal: bool = False  # karaoke: words appear as they are spoken
    shadow: int = 0  # karaoke: drop shadow depth, 0-6
    # Words per line, as a character budget (1 = one word at a time). None
    # keeps the generator's defaults, which is what old clients send.
    max_chars: Optional[int] = None
    max_duration: Optional[float] = None
    # Named look (subtitles.CAPTION_PRESETS); any field sent explicitly
    # alongside it overrides that field of the preset.
    preset: Optional[str] = None
    input_filename: Optional[str] = None
    # User-edited caption words. When present, the burn uses them VERBATIM
    # instead of regenerating from the stored transcript — without this, text
    # edits in the modal were silently discarded on the server render path.
    words: Optional[List[CaptionWordIn]] = None


@app.get("/api/clip/{job_id}/{clip_index}/transcript")
async def get_clip_transcript(job_id: str, clip_index: int, request: Request):
    """Return word-level captions for a specific clip, formatted for Remotion."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    output_dir = os.path.join(OUTPUT_DIR, job_id)
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))

    if not json_files:
        raise HTTPException(status_code=404, detail="Metadata not found")

    with open(json_files[0], 'r') as f:
        data = json.load(f)

    transcript = data.get('transcript')
    if not transcript:
        raise HTTPException(status_code=400, detail="Transcript not found in metadata")

    clips = data.get('shorts', [])
    if clip_index >= len(clips):
        raise HTTPException(status_code=404, detail="Clip not found")

    clip_data = clips[clip_index]

    # Recut clips are concatenations of source segments; remap the transcript
    # onto the clip timeline so every consumer keeps its flat start..end logic.
    recipe_segments = (clip_data.get('recipe') or {}).get('segments')
    if recipe_segments:
        transcript = recut.virtual_transcript(transcript, recipe_segments)
        clip_start, clip_end = 0.0, recut.total_duration(recipe_segments)
    else:
        clip_start = clip_data.get('start', 0)
        clip_end = clip_data.get('end', 0)

    # Extract words within clip range and convert to CaptionWord format
    captions = []
    for segment in transcript.get('segments', []):
        for word_info in segment.get('words', []):
            if word_info['end'] > clip_start and word_info['start'] < clip_end:
                captions.append({
                    "text": word_info.get('word', '').strip(),
                    "startMs": int((max(0, word_info['start'] - clip_start)) * 1000),
                    "endMs": int((max(0, word_info['end'] - clip_start)) * 1000),
                })

    duration_sec = clip_end - clip_start

    return {
        "captions": captions,
        "durationSec": duration_sec,
        "language": transcript.get('language', 'en'),
    }


# --- Clip editor: EDL + re-render ---

# The editor ships the WHOLE source transcript, not a window around the clip:
# the point of the source track is extending a cut into material the clip never
# covered, and you cannot pick a new in-point from words you were not sent.
# Cost is about 7 KB of JSON per minute of speech, fetched once per editor open.


def _source_duration_seconds(path):
    """Probe a video's duration; None when it can't be read."""
    try:
        import cv2
        cap = cv2.VideoCapture(path)
        fps = cap.get(cv2.CAP_PROP_FPS)
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        cap.release()
        # OpenCV reports -1/-1 for files it can't read — a naive truthiness
        # check would turn that into a phantom 1.0s duration.
        if fps > 0 and frames > 0:
            return round(frames / fps, 3)
    except Exception:
        pass
    return None


def _clip_recipe_parts(clip):
    """(segments, canonical_range) for a clip — synthesized from the flat
    start/end for clips that were never recut."""
    recipe = clip.get('recipe') or {}
    fallback = {"start": float(clip.get('start', 0) or 0),
                "end": float(clip.get('end', 0) or 0)}
    segments = recipe.get('segments') or [dict(fallback)]
    canonical_range = recipe.get('canonical_range') or dict(fallback)
    return segments, canonical_range


@app.get("/api/clip/{job_id}/{clip_index}/edl")
async def get_clip_edl(job_id: str, clip_index: int, request: Request):
    """The clip's editable recipe: which source segments it was cut from, the
    word timeline around them, and whether the source is still available for
    cuts outside the original range."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]

    output_dir = os.path.join(OUTPUT_DIR, job_id)
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
    if not json_files:
        raise HTTPException(status_code=404, detail="Metadata not found")
    with open(json_files[0], 'r') as f:
        data = json.load(f)

    clips = data.get('shorts', [])
    if clip_index < 0 or clip_index >= len(clips):
        raise HTTPException(status_code=404, detail="Clip not found")
    clip = clips[clip_index]

    segments, canonical_range = _clip_recipe_parts(clip)
    transcript = data.get('transcript') or {}
    words = recut.transcript_words(transcript)

    source_path = _locate_source(job_id)
    source_duration = _source_duration_seconds(source_path) if source_path else None
    duration_estimated = source_duration is None
    if duration_estimated:
        # Best remaining scale for the source track: the last spoken word or
        # the furthest point any recipe touches.
        candidates = [canonical_range['end']] + [s['end'] for s in segments]
        if words:
            candidates.append(words[-1]['e'])
        source_duration = round(max(candidates), 3)

    words_out = [
        {"w": w["w"], "s": round(w["s"], 3), "e": round(w["e"], 3)}
        for w in words
    ]

    current_file = (clip.get('video_url') or '').split('/')[-1]
    if not current_file:
        base_name = os.path.basename(json_files[0]).replace('_metadata.json', '')
        current_file = _canonical_clip_file(output_dir, base_name, clip_index)

    total = recut.total_duration(segments)
    return {
        "job_id": job_id,
        "clip_index": clip_index,
        "title": clip.get('video_title_for_youtube_short') or '',
        "segments": segments,
        "framing": (clip.get('recipe') or {}).get('framing') or 'auto',
        "canonical_range": canonical_range,
        "duration": total,
        "current_file": current_file,
        "has_captions": bool(re.match(r'^subtitled_\d+_', current_file)),
        "words": words_out,
        "source": {
            "available": bool(source_path),
            "url": _signed_source_url(job_id) if source_path else None,
            "duration": source_duration,
            "duration_estimated": duration_estimated,
        },
        "limits": {
            "max_segments": recut.MAX_SEGMENTS,
            "min_segment_seconds": recut.MIN_SEGMENT_SECONDS,
            "max_total_seconds": recut.MAX_TOTAL_SECONDS,
        },
        "rerender_minutes": 0,
    }


class RerenderSegment(BaseModel):
    start: float
    end: float


class RerenderRequest(BaseModel):
    job_id: str
    clip_index: int
    segments: List[RerenderSegment]
    snap_to_words: bool = False
    reapply_captions: bool = True
    # None = inherit the recipe's framing (so plain trims keep the look);
    # 'auto' resets to the classifier; 'full'/'track' force a layout.
    framing: Optional[str] = None


# Manual framing -> reframe-engine strategy. 'full' shows the whole source
# frame (WIDE: no side-cropping, blurred filler bands); 'track' forces the
# subject-tracking crop. Anything non-auto needs the retained source video.
_FRAMING_STRATEGIES = {"auto": None, "full": "WIDE", "track": "TRACK"}


# One lock per job (same pattern as _restore_locks): rerenders on the same job
# share metadata.json and the canonical files, so they must not interleave.
_rerender_locks: Dict[str, asyncio.Lock] = {}

# Scene-listing builds write stable preview/thumbnail names per job; serialize
# them so overlapping editor opens don't tear each other's files.
_scenes_locks: Dict[str, asyncio.Lock] = {}


@app.post("/api/clip/rerender")
async def rerender_clip(req: RerenderRequest, request: Request):
    """Re-render a clip from an edited EDL (the clip editor's save button).

    Two paths, chosen automatically:
    - FAST: every segment stays inside the range the canonical clip was cut
      from → recut straight from the already-reframed canonical file. No ML,
      no source needed.
    - SOURCE: a segment reaches outside → recut from the retained source and
      re-reframe with the same engine the pipeline used. 409 when the source
      already aged out.
    """
    if req.job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[req.job_id]

    # Serialize rerenders per job: concurrent saves (easy for an MCP agent to
    # produce) would otherwise race on the shared metadata.json
    # read-modify-write below, with the last writer silently reverting the
    # other clip's recipe/video_url.
    lock = _rerender_locks.setdefault(req.job_id, asyncio.Lock())
    async with lock:
        return await _rerender_locked(req, request, job)


async def _rerender_locked(req: RerenderRequest, request: Request, job):
    output_dir = os.path.join(OUTPUT_DIR, req.job_id)
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
    if not json_files:
        raise HTTPException(status_code=404, detail="Metadata not found")
    with open(json_files[0], 'r') as f:
        data = json.load(f)

    clips = data.get('shorts', [])
    if req.clip_index < 0 or req.clip_index >= len(clips):
        raise HTTPException(status_code=404, detail="Clip not found")
    clip = clips[req.clip_index]
    transcript = data.get('transcript') or {}

    base_name = os.path.basename(json_files[0]).replace('_metadata.json', '')
    clean_name = f"{base_name}_clip_{req.clip_index + 1}.mp4"
    canonical_path = os.path.join(output_dir, clean_name)

    _, canonical_range = _clip_recipe_parts(clip)
    source_path = _locate_source(req.job_id)
    source_duration = _source_duration_seconds(source_path) if source_path else None

    framing = req.framing or (clip.get('recipe') or {}).get('framing') or 'auto'
    if framing not in _FRAMING_STRATEGIES:
        raise HTTPException(status_code=400,
                            detail="framing must be one of: auto, full, track")
    force_strategy = _FRAMING_STRATEGIES[framing]

    try:
        segments = recut.normalize_segments(
            [{"start": s.start, "end": s.end} for s in req.segments],
            source_duration)
        if req.snap_to_words:
            snap_bound = source_duration or max(s['end'] for s in segments)
            segments = recut.snap_segments(segments, transcript, snap_bound)
            if not source_path:
                # Snapping trails into silence and may nudge a boundary past
                # the canonical range; without a source the fast path is the
                # only path, so clamp back instead of failing with a 409.
                segments = [
                    {"start": round(max(s['start'], canonical_range['start']), 3),
                     "end": round(min(s['end'], canonical_range['end']), 3)}
                    for s in segments]
            # Re-validate after snapping/clamping: a segment fully outside the
            # canonical range clamps to an inverted (end < start) window, and
            # snapping can stretch the total past the cap. Without this it
            # reaches ffmpeg and dies as a 500 instead of a clean 400.
            segments = recut.normalize_segments(segments, source_duration)
    except recut.RecutError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # A framing override always re-reframes from the source: the canonical file
    # has the old layout baked into its pixels.
    fast = (force_strategy is None
            and os.path.exists(canonical_path)
            and recut.within_range(segments, canonical_range['start'],
                                   canonical_range['end']))
    if not fast and not source_path:
        raise HTTPException(
            status_code=409,
            detail=("The source video is no longer on the server; changing the "
                    "framing needs it." if force_strategy is not None else
                    "The source video is no longer on the server, so segments "
                    "must stay within the original clip range."))

    total = recut.total_duration(segments)
    v_transcript = (recut.virtual_transcript(transcript, segments)
                    if req.reapply_captions else None)

    def run_recut():
        if fast:
            return recut.perform_recut(
                input_path=canonical_path,
                segments=recut.rebase_segments(
                    segments, canonical_range['start'], canonical_range['end']),
                output_dir=output_dir, clean_name=clean_name,
                reframe=False, captions_transcript=v_transcript,
                captioner=_recut_captioner(clip))
        return recut.perform_recut(
            input_path=source_path, segments=segments,
            output_dir=output_dir, clean_name=clean_name,
            reframe=True, output_format=data.get('output_format', 'auto'),
            force_strategy=force_strategy,
            captions_transcript=v_transcript,
            captioner=_recut_captioner(clip),
            # Trim the source's own subtitles only when ours go back on.
            subtitle_band=(data.get('source_subtitle_band')
                           if v_transcript else None))

    try:
        loop = asyncio.get_event_loop()
        served_name, _clean_recut_name = await loop.run_in_executor(None, run_recut)

        new_video_url = f"/videos/{req.job_id}/{served_name}"
        new_recipe = {"v": 1, "segments": segments,
                      "canonical_range": canonical_range}
        if framing != 'auto':
            new_recipe["framing"] = framing
        # Covering range, deliberately not segments[0]/segments[-1]: segments
        # may legally be out of source order, and downstream consumers only
        # need a sane positive window (the recipe is the real timeline).
        new_start = min(s['start'] for s in segments)
        new_end = max(s['end'] for s in segments)

        updates = {'video_url': new_video_url, 'start': new_start,
                   'end': new_end, 'recipe': new_recipe,
                   # The stacked stretches of THIS render (empty on the fast
                   # path, which never reframes): captions follow them.
                   'layout_ranges': layout_ranges.read(
                       os.path.join(output_dir, _clean_recut_name))}
        # Per-scene manual framing is keyed by scene indices of a specific cut;
        # this render neither applied it nor can it survive a changed cut, so
        # clear it rather than let /scenes serve stale overrides against the
        # wrong shots (re-frame after trimming to re-apply by hand).
        if clip.get('crop_overrides'):
            updates['crop_overrides'] = None
        # Edited caption words are timed against the previous cut; the new one
        # captions from the transcript (in the recorded style, if any).
        if clip.get('caption_words'):
            updates['caption_words'] = None
        clip.update(updates)
        data['shorts'] = clips
        write_json_atomic(json_files[0], data, indent=2)
        mem_clips = (job.get('result') or {}).get('clips') or []
        if req.clip_index < len(mem_clips):
            mem_clips[req.clip_index].update(updates)

        return {
            "success": True,
            "new_video_url": new_video_url,
            "recipe": new_recipe,
            "framing": framing,
            "start": new_start,
            "end": new_end,
            "duration": total,
            "render_path": "fast" if fast else "source",
        }
    except Exception as e:
        print(f"❌ Rerender Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# --- Manual framing -----------------------------------------------------------
#
# The reframe engine picks the crop automatically, and on a podcast it is right
# most of the time and grossly wrong occasionally: a wide shot of the whole
# table averages over one face, so the scene falls to GENERAL (letterboxed) or
# tracks the wrong person. There was no way to say "no, frame it here".
#
# The unit is the SCENE, not the clip, because a podcast cuts between a fixed
# close camera and a fixed wide one, and the right crop differs per camera.
# Scene boundaries already are the camera changes: PySceneDetect finds them.
#
# Scenes the user never touches keep the automatic camera, so correcting one
# bad shot cannot spoil the ones the tracker got right.

class ReframeRequest(BaseModel):
    job_id: str
    clip_index: int
    # scene index (string key, JSON-style) -> either a crop centre as a
    # fraction of the source width, or {"top": f, "bottom": f} to stack two
    # regions. Fractions travel instead of pixels so the editor never needs to
    # know the source dimensions.
    crop_overrides: Dict[str, Any]
    reapply_captions: bool = True


def _clip_scene_workfile(source_path, segments, output_dir, token):
    """Cut the clip out of the source so scenes can be detected on it.

    The name is unique per call, unlike the thumbnails and the preview: two
    editor opens on the same clip would otherwise write the same temp file at
    once, and the first to finish deletes it out from under the second.
    """
    work_path = os.path.join(output_dir, f"scenes_{token}.mp4")
    recut.run_cut_concat(source_path, segments, work_path, output_dir)
    return work_path


@app.get("/api/clip/{job_id}/{clip_index}/scenes")
async def get_clip_scenes(job_id: str, clip_index: int, request: Request):
    """Scenes of a clip, each with a SOURCE frame to frame it against.

    The frames come from the uncropped cut, not the delivered clip: the point
    is to show what the automatic crop threw away, which the 9:16 file no
    longer contains.
    """
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]
    output_dir = os.path.join(OUTPUT_DIR, job_id)
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
    if not json_files:
        raise HTTPException(status_code=404, detail="Metadata not found")
    with open(json_files[0], 'r') as f:
        data = json.load(f)

    clips = data.get('shorts', [])
    if clip_index < 0 or clip_index >= len(clips):
        raise HTTPException(status_code=404, detail="Clip not found")

    if data.get('output_format') == 'horizontal':
        raise HTTPException(
            status_code=400,
            detail="Horizontal clips keep the full frame; there is no crop to reframe.")

    clip = clips[clip_index]
    segments, _canonical_range = _clip_recipe_parts(clip)
    # What was applied last time. Without this the editor reopens blank, and
    # since a re-render rebuilds from source using ONLY what it is sent, the
    # next save would silently drop every earlier adjustment.
    saved_overrides = clip.get('crop_overrides') or {}
    source_path = _locate_source(job_id)
    if not source_path:
        raise HTTPException(
            status_code=409,
            detail="The source video is no longer on the server, so the "
                   "framing of this clip can no longer be changed.")

    def build():
        import cv2
        import main as m

        # Stable for the files the browser fetches (no accumulation), unique
        # for the temp cut (no collision between overlapping requests).
        # temp_ prefix keeps these editor-only artifacts out of the self-host
        # S3 backup (it skips temp_*); stable names still avoid accumulation.
        token = str(clip_index)
        work_token = f"{clip_index}_{uuid.uuid4().hex[:8]}"
        preview_name = f"temp_preview_{clip_index}.mp4"
        preview_path = os.path.join(output_dir, preview_name)
        work_path = _clip_scene_workfile(source_path, segments, output_dir, work_token)
        try:
            scenes, fps = m.detect_scenes(work_path)
            fps = float(fps) or 30.0
            orig_w, orig_h = m.get_video_resolution(work_path)

            cap = cv2.VideoCapture(work_path)
            if not scenes:
                total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
                bounds = [(0, total)]
            else:
                bounds = [(s.get_frames(), e.get_frames()) for s, e in scenes]

            out = []
            for idx, (start_f, end_f) in enumerate(bounds):
                mid = (start_f + end_f) // 2
                cap.set(cv2.CAP_PROP_POS_FRAMES, mid)
                ok, frame = cap.read()
                thumb_name = f"temp_scene_{token}_{idx:03d}.jpg"
                suggested = 0.5
                suggested_y = 0.5
                if ok:
                    cv2.imwrite(os.path.join(output_dir, thumb_name), frame,
                                [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                    # Start the rectangle on the biggest face in the shot, so
                    # the common case is a nudge rather than a hunt.
                    try:
                        faces = m.detect_face_candidates(frame)
                        if faces:
                            box = max(faces,
                                      key=lambda f: f['box'][2] * f['box'][3])['box']
                            suggested = min(1.0, max(0.0,
                                                     (box[0] + box[2] / 2) / orig_w))
                            # SPLIT halves crop vertically too, so the face's
                            # height matters there (TRACK ignores it).
                            suggested_y = min(1.0, max(0.0,
                                                       (box[1] + box[3] / 2) / orig_h))
                    except Exception:
                        pass
                else:
                    thumb_name = None

                out.append({
                    "index": idx,
                    "start": round(start_f / fps, 3),
                    "end": round(end_f / fps, 3),
                    "thumbnail_url": (f"/videos/{job_id}/{thumb_name}"
                                      if thumb_name else None),
                    "suggested_center": round(suggested, 4),
                    "suggested_center_y": round(suggested_y, 4),
                })
            cap.release()
            return orig_w, orig_h, out, preview_name
        finally:
            # The uncropped cut becomes a light preview instead of being
            # discarded: judging the framing means knowing who is talking, and
            # the delivered 9:16 file no longer shows the rest of the room.
            try:
                if os.path.exists(work_path):
                    subprocess.run(
                        ["ffmpeg", "-y", "-loglevel", "error", "-i", work_path,
                         "-vf", "scale=640:-2", "-c:v", "libx264", "-preset",
                         "veryfast", "-crf", "30", "-c:a", "aac", "-b:a", "96k",
                         # faststart: the editor's <video> streams the preview;
                         # a tail moov would stall it until fully downloaded.
                         "-movflags", "+faststart",
                         preview_path], check=True, timeout=600)
            except Exception as exc:
                print(f"Scene preview failed: {exc}")
            finally:
                if os.path.exists(work_path):
                    os.remove(work_path)

    # Serialized per job: two overlapping opens would run two ffmpeg writers
    # on the same stable preview/thumbnail names and serve a torn file.
    lock = _scenes_locks.setdefault(job_id, asyncio.Lock())
    async with lock:
        try:
            loop = asyncio.get_event_loop()
            orig_w, orig_h, scenes_out, preview_name = await loop.run_in_executor(None, build)
        except Exception as e:
            print(f"Scene listing error: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    # Width of the crop window as a fraction of the source width: the editor
    # draws the rectangle with it and needs no pixel arithmetic of its own.
    # The aspect follows the job's output format (9:16, or 1:1 for square).
    aspect = 1.0 if data.get('output_format') == 'square' else 9.0 / 16.0
    crop_w = min(orig_w, orig_h * aspect)
    return {
        "job_id": job_id,
        "clip_index": clip_index,
        "source_width": orig_w,
        "source_height": orig_h,
        "crop_width_fraction": round(crop_w / orig_w, 4),
        "preview_url": f"/videos/{job_id}/{preview_name}",
        "saved_overrides": saved_overrides,
        "scenes": scenes_out,
    }


@app.post("/api/clip/reframe")
async def reframe_clip(req: ReframeRequest, request: Request):
    """Re-render a clip with hand-framed scenes, leaving its cut untouched.

    Deliberately separate from /api/clip/rerender: the overrides are keyed by
    scene index, and a scene index only means anything against a given cut. If
    framing rode along with a trim save, changing the trim would silently move
    every override onto the wrong shot.
    """
    if req.job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[req.job_id]

    def _fraction(v):
        return min(1.0, max(0.0, float(v)))

    overrides = {}
    for key, value in (req.crop_overrides or {}).items():
        try:
            idx = int(key)
            if isinstance(value, dict):
                def _half(h):
                    if isinstance(h, dict):
                        return {"x": _fraction(h["x"]), "y": _fraction(h.get("y", 0.5))}
                    return {"x": _fraction(h), "y": 0.5}
                overrides[idx] = {"top": _half(value["top"]),
                                  "bottom": _half(value["bottom"])}
            else:
                overrides[idx] = _fraction(value)
        except (KeyError, TypeError, ValueError):
            continue
    if not overrides:
        raise HTTPException(status_code=400,
                            detail="No scene framing was provided.")

    # Same lock as /rerender: both read-modify-write the job's metadata.json,
    # and the metadata must be read INSIDE the lock or a rerender committing
    # in between gets clobbered by a write of stale data.
    lock = _rerender_locks.setdefault(req.job_id, asyncio.Lock())
    async with lock:
        return await _reframe_locked(req, request, job, overrides)


async def _reframe_locked(req: ReframeRequest, request: Request, job, overrides):
    output_dir = os.path.join(OUTPUT_DIR, req.job_id)
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
    if not json_files:
        raise HTTPException(status_code=404, detail="Metadata not found")
    with open(json_files[0], 'r') as f:
        data = json.load(f)

    clips = data.get('shorts', [])
    if req.clip_index < 0 or req.clip_index >= len(clips):
        raise HTTPException(status_code=404, detail="Clip not found")
    clip = clips[req.clip_index]

    if data.get('output_format') == 'horizontal':
        raise HTTPException(
            status_code=400,
            detail="Horizontal clips keep the full frame; there is no crop to reframe.")

    segments, canonical_range = _clip_recipe_parts(clip)
    source_path = _locate_source(req.job_id)
    if not source_path:
        raise HTTPException(
            status_code=409,
            detail="The source video is no longer on the server, so the "
                   "framing of this clip can no longer be changed.")

    # Whole-clip framing (recipe.framing, the clip editor's selector) still
    # applies to the scenes the user did NOT hand-position: apply_crop_overrides
    # runs after force_strategy, so a per-scene choice beats the whole-clip one.
    framing = (clip.get('recipe') or {}).get('framing') or 'auto'
    force_strategy = _FRAMING_STRATEGIES.get(framing)

    # Every default clip ships with burned captions; re-rendering without them
    # would silently hand back a caption-less file.
    v_transcript = (recut.virtual_transcript(data.get('transcript') or {}, segments)
                    if req.reapply_captions else None)

    base_name = os.path.basename(json_files[0]).replace('_metadata.json', '')
    # The CLEAN base name, exactly as /api/clip/rerender does — never the
    # current derived file. perform_recut prefixes whatever it is given, so
    # feeding it the newest derivative made every re-render add another
    # recut_<ts>_<hex>_ in front of the previous one. A few rounds of framing
    # and captioning and the path blew past the filesystem limit:
    # "Error opening output ...: File name too long".
    clean_name = f"{base_name}_clip_{req.clip_index + 1}.mp4"

    def run():
        return recut.perform_recut(
            input_path=source_path, segments=segments,
            output_dir=output_dir, clean_name=clean_name,
            reframe=True, output_format=data.get('output_format', 'auto'),
            force_strategy=force_strategy,
            crop_overrides=overrides,
            captions_transcript=v_transcript,
            captioner=_recut_captioner(clip),
            # Trim the source's own subtitles only when ours go back on.
            subtitle_band=(data.get('source_subtitle_band')
                           if v_transcript else None))

    try:
        loop = asyncio.get_event_loop()
        served_name, _clean = await loop.run_in_executor(None, run)

        new_video_url = f"/videos/{req.job_id}/{served_name}"
        new_recipe = {"v": 1, "segments": segments,
                      "canonical_range": canonical_range}
        if framing != 'auto':
            new_recipe["framing"] = framing
        updates = {
            'video_url': new_video_url,
            'recipe': new_recipe,
            'crop_overrides': {str(k): v for k, v in overrides.items()},
            'layout_ranges': layout_ranges.read(os.path.join(output_dir, _clean)),
        }
        clip.update(updates)
        data['shorts'] = clips
        write_json_atomic(json_files[0], data, indent=2)
        mem_clips = (job.get('result') or {}).get('clips') or []
        if req.clip_index < len(mem_clips):
            mem_clips[req.clip_index].update(updates)

        # The cut is untouched, but the response mirrors /rerender's shape
        # so the dashboard can reuse one handler without clearing the
        # clip's timing fields.
        return {
            "success": True,
            "new_video_url": new_video_url,
            "recipe": new_recipe,
            "start": min(s['start'] for s in segments),
            "end": max(s['end'] for s in segments),
            "framed_scenes": sorted(overrides),
        }
    except Exception as e:
        print(f"Reframe Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# --- Remotion Render Proxy ---
RENDER_SERVICE_URL = os.getenv("RENDER_SERVICE_URL", "http://renderer:3100")

RENDER_MAX_FRAMES = int(os.getenv("RENDER_MAX_FRAMES", str(60 * 60 * 5)))  # 5 min at 60 fps
RENDER_MAX_SIDE = int(os.getenv("RENDER_MAX_SIDE", "2160"))


async def _check_render_body(request, body):
    """What the render service may be asked to do, decided here: it trusts
    its caller, writes under OUTPUT_DIR/<jobId> and loads ``videoUrl`` in a
    headless browser. In cloud mode the job must be the caller's and the
    video one of its files; everywhere the size of the render is bounded."""
    if not isinstance(body, dict) or not isinstance(body.get("props"), dict):
        raise HTTPException(status_code=400, detail="Invalid render request")
    job_id = str(body.get("jobId") or "")
    if not _JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=400, detail="jobId must be a job id")
    props = body["props"]
    try:
        frames, fps = int(props.get("durationInFrames")), float(props.get("fps"))
        width, height = int(props.get("width")), int(props.get("height"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid render dimensions")
    if not (0 < frames <= RENDER_MAX_FRAMES and 0 < fps <= 120
            and 0 < width <= RENDER_MAX_SIDE and 0 < height <= RENDER_MAX_SIDE):
        raise HTTPException(status_code=413, detail=(
            f"Render too large: at most {RENDER_MAX_FRAMES} frames, 120 fps and "
            f"{RENDER_MAX_SIDE}px a side."))


@app.post("/api/render")
async def proxy_render(request: Request):
    """Proxy render requests to the Node.js Remotion render service."""
    body = await request.json()
    await _check_render_body(request, body)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{RENDER_SERVICE_URL}/render", json=body)
        return resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Render service unavailable: {e}")

@app.get("/api/render/{render_id}")
async def proxy_render_status(render_id: str, request: Request):
    """Proxy render status polling to the Node.js Remotion render service."""
    import httpx
    # The id goes into the upstream path: ids are uuids, nothing else passes.
    if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", render_id or ""):
        raise HTTPException(status_code=404, detail="Not found")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{RENDER_SERVICE_URL}/render/{render_id}")
            return resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Render service unavailable: {e}")


class EffectsGenerateRequest(BaseModel):
    job_id: str
    clip_index: int
    input_filename: Optional[str] = None

@app.post("/api/effects/generate")
async def generate_effects_config(
    req: EffectsGenerateRequest,
    request: Request,
):
    """Generate structured EffectsConfig JSON for Remotion rendering via Gemini AI."""
    final_api_key = await resolve_gemini(request)

    if not final_api_key:
        raise gemini_missing_error()

    if req.job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[req.job_id]
    if 'result' not in job or 'clips' not in job['result']:
        raise HTTPException(status_code=400, detail="Job result not available")

    try:
        # Resolve input path
        if req.input_filename:
            safe_name = os.path.basename(req.input_filename)
            input_path = os.path.join(OUTPUT_DIR, req.job_id, safe_name)
        else:
            clip = job['result']['clips'][req.clip_index]
            filename = clip['video_url'].split('/')[-1]
            input_path = os.path.join(OUTPUT_DIR, req.job_id, filename)

        if not os.path.exists(input_path):
            raise HTTPException(status_code=404, detail=f"Video file not found: {input_path}")

        def run_effects_generation():
            editor = VideoEditor(api_key=final_api_key)

            # Create safe ASCII filename to avoid encoding issues
            safe_filename = f"temp_effects_{req.job_id}.mp4"
            safe_input_path = os.path.join(OUTPUT_DIR, req.job_id, safe_filename)
            shutil.copy(input_path, safe_input_path)

            try:
                # Upload video to Gemini
                vid_file = editor.upload_video(safe_input_path)

                # Get video metadata via ffprobe
                probe_cmd = [
                    'ffprobe', '-v', 'error',
                    '-select_streams', 'v:0',
                    '-show_entries', 'stream=width,height,r_frame_rate,duration',
                    '-show_entries', 'format=duration',
                    '-of', 'json',
                    safe_input_path
                ]
                probe_result = subprocess.check_output(probe_cmd).decode().strip()
                probe_data = json.loads(probe_result)

                stream = probe_data.get('streams', [{}])[0]
                width = int(stream.get('width', 1080))
                height = int(stream.get('height', 1920))

                # Parse fps from r_frame_rate (e.g. "30/1")
                r_frame_rate = stream.get('r_frame_rate', '30/1')
                num, den = r_frame_rate.split('/')
                fps = round(int(num) / int(den), 2)

                # Get duration from stream or format
                duration = float(stream.get('duration', 0))
                if duration == 0:
                    duration = float(probe_data.get('format', {}).get('duration', 0))

                # Load transcript from metadata
                transcript = None
                try:
                    meta_files = glob.glob(os.path.join(OUTPUT_DIR, req.job_id, "*_metadata.json"))
                    if meta_files:
                        with open(meta_files[0], 'r') as f:
                            data = json.load(f)
                            transcript = data.get('transcript')
                except Exception as e:
                    print(f"⚠️ Could not load transcript for effects config: {e}")

                # Generate effects config
                effects_config = editor.get_effects_config(
                    vid_file, duration, fps=fps, width=width, height=height, transcript=transcript
                )

                return effects_config
            finally:
                if os.path.exists(safe_input_path):
                    os.remove(safe_input_path)

        loop = asyncio.get_event_loop()
        effects_config = await loop.run_in_executor(None, run_effects_generation)

        if effects_config is None:
            raise HTTPException(status_code=500, detail="Failed to generate effects config from Gemini")

        return {"effects": effects_config}

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Effects Generation Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/subtitle")
async def add_subtitles(req: SubtitleRequest, request: Request):
    if req.preset:
        preset = CAPTION_PRESETS.get(req.preset.strip().lower())
        if preset is None:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown preset. Use one of: {', '.join(CAPTION_PRESETS)}.")
        req = req.model_copy(update={k: v for k, v in preset.items()
                                     if k not in req.model_fields_set})
    if req.job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    
    # Reload job data from disk just in case metadata was updated
    job = jobs[req.job_id]

    # We need to access metadata.json to get the transcript
    output_dir = os.path.join(OUTPUT_DIR, req.job_id)
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
    
    if not json_files:
        raise HTTPException(status_code=404, detail="Metadata not found")
        
    with open(json_files[0], 'r') as f:
        data = json.load(f)
        
    transcript = data.get('transcript')
    if not transcript:
        raise HTTPException(status_code=400, detail="Transcript not found in metadata. Please process a new video.")
        
    clips = data.get('shorts', [])
    if req.clip_index >= len(clips):
        raise HTTPException(status_code=404, detail="Clip not found")
        
    clip_data = clips[req.clip_index]

    # Recut clips concatenate several source segments, so their caption window
    # is not the flat start..end range — restyle against the clip-relative
    # remapped transcript instead.
    recipe_segments = (clip_data.get('recipe') or {}).get('segments')
    if recipe_segments:
        sub_transcript = recut.virtual_transcript(transcript, recipe_segments)
        sub_start, sub_end = 0.0, recut.total_duration(recipe_segments)
    else:
        sub_transcript = transcript
        sub_start = clip_data.get('start', 0)
        sub_end = clip_data.get('end', 0)

    # User-edited captions win over both: build a synthetic clip-relative
    # transcript from them so the SRT/ASS generators burn the edited words
    # verbatim (issue #69 — edits used to be dropped on this path).
    caption_words = None
    if req.words:
        if len(req.words) > 2000:
            raise HTTPException(status_code=400, detail="Too many caption words (max 2000).")
        # Leading space = Whisper's word-boundary convention; without it the
        # block collector treats each word as a continuation fragment and
        # glues the whole line together.
        edited = [
            {"word": " " + w.text.strip(), "start": max(0.0, w.startMs / 1000.0),
             "end": max(0.0, w.endMs / 1000.0)}
            for w in req.words if w.text.strip() and w.endMs > w.startMs >= 0
        ]
        if edited:
            sub_transcript = _words_transcript(
                edited, (transcript or {}).get("language", "en"))
            sub_start, sub_end = 0.0, sub_transcript["segments"][0]["end"]
            caption_words = sub_transcript["segments"][0]["words"]

    # Video Path
    if req.input_filename:
        # Use chained file
        filename = os.path.basename(req.input_filename)
    else:
        # Fallback to standard naming
        filename = clip_data.get('video_url', '').split('/')[-1]
        if not filename:
             base_name = os.path.basename(json_files[0]).replace('_metadata.json', '')
             filename = f"{base_name}_clip_{req.clip_index+1}.mp4"

    # Re-subtitling must replace previous subtitles instead of burning over them.
    filename = await asyncio.to_thread(_strip_burned_captions, output_dir, filename)

    input_path = os.path.join(output_dir, filename)
    if not os.path.exists(input_path):
        # Try looking for edited version if url implied it?
        # Just fail if not found.
        raise HTTPException(status_code=404, detail=f"Video file not found: {input_path}")

    # Define outputs
    generation_id = int(time.time())
    is_karaoke = req.style == "karaoke"
    srt_filename = f"subs_{req.clip_index}_{generation_id}.{'ass' if is_karaoke else 'srt'}"
    srt_path = os.path.join(output_dir, srt_filename)

    # Style options shared by the karaoke ASS generator paths.
    # Stacked (SPLIT) stretches put their captions on the seam. The metadata
    # copy is authoritative; the sidecar covers clips rendered before it was
    # recorded there.
    seam_ranges = layout_ranges.split_ranges(
        clip_data.get('layout_ranges') or layout_ranges.read(input_path))
    caption_style = {k: getattr(req, k) for k in CAPTION_STYLE_FIELDS}
    karaoke_opts = dict(
        split_ranges=seam_ranges,
        alignment=req.position, fontsize=req.font_size, font_name=req.font_name,
        font_color=req.font_color, border_color=req.border_color,
        border_width=req.border_width, highlight_color=req.highlight_color,
        bg_color=req.bg_color, bg_opacity=req.bg_opacity,
        effect=req.effect, base_opacity=req.base_opacity, uppercase=req.uppercase,
        reveal=req.reveal, shadow=req.shadow,
    )
    if req.max_chars is not None:
        karaoke_opts["max_chars"] = max(1, min(40, int(req.max_chars)))
    if req.max_duration is not None:
        karaoke_opts["max_duration"] = max(0.5, min(5.0, float(req.max_duration)))

    # Output video
    # We create a new file "subtitled_..."
    output_filename = f"subtitled_{generation_id}_{filename}"
    output_path = os.path.join(output_dir, output_filename)

    try:
        # Same code path the hook / edit / recut re-burn uses, so the look
        # recorded below is the look that was burned here.
        success = await asyncio.get_event_loop().run_in_executor(
            None, _burn_caption_style, input_path, output_path, srt_path,
            sub_transcript, sub_start, sub_end, caption_style, seam_ranges)
        if not success:
            raise HTTPException(status_code=400, detail="No words found for this clip range.")

    except Exception as e:
        print(f"❌ Subtitle Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))



    # 3. Update Result and Metadata
    # Update InMemory Jobs
    if req.clip_index < len(job['result']['clips']):
         job['result']['clips'][req.clip_index]['video_url'] = f"/videos/{req.job_id}/{output_filename}"
    
    # Update Metadata on Disk (Persistence)
    try:
        if req.clip_index < len(clips):
            clips[req.clip_index]['video_url'] = f"/videos/{req.job_id}/{output_filename}"
            # The look (and edited words) the hook / edit / recut paths burn
            # again when they put captions back (_reapply_captions, #82).
            clips[req.clip_index]['caption_style'] = caption_style
            if caption_words:
                clips[req.clip_index]['caption_words'] = caption_words
            else:
                clips[req.clip_index].pop('caption_words', None)
            # Update the main data structure
            data['shorts'] = clips
            
            # Write back
            write_json_atomic(json_files[0], data, indent=4)
            print(f"✅ Metadata updated with subtitled video for clip {req.clip_index}")
    except Exception as e:
        print(f"⚠️ Failed to update metadata.json: {e}")
        # Non-critical, but good for persistence


    return {
        "success": True,
        "new_video_url": f"/videos/{req.job_id}/{output_filename}"
    }

class RemoveSubtitlesRequest(BaseModel):
    job_id: str
    clip_index: int
    input_filename: Optional[str] = None


@app.post("/api/subtitle/remove")
async def remove_subtitles(req: RemoveSubtitlesRequest, request: Request):
    """Point a clip back at its un-captioned original.

    Clips ship captioned by default now, so there has to be a way out — without
    this, a user who doesn't want captions is stuck with them. No re-encode and
    no quota: the pipeline always keeps the clean file next to the derived
    ``subtitled_<ts>_`` one, so removing is just choosing the other file.
    """
    if req.job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[req.job_id]

    output_dir = os.path.join(OUTPUT_DIR, req.job_id)
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
    if not json_files:
        raise HTTPException(status_code=404, detail="Metadata not found")
    with open(json_files[0], 'r') as f:
        data = json.load(f)
    clips = data.get('shorts', [])
    if req.clip_index < 0 or req.clip_index >= len(clips):
        raise HTTPException(status_code=404, detail="Clip not found")

    filename = os.path.basename(
        req.input_filename
        or (clips[req.clip_index].get('video_url') or '').split('/')[-1]
        or f"{os.path.basename(json_files[0]).replace('_metadata.json', '')}"
           f"_clip_{req.clip_index + 1}.mp4")

    # Same walk-back the burn path uses, so this undoes any number of restyles.
    filename = await asyncio.to_thread(_strip_burned_captions, output_dir, filename)

    if not os.path.exists(os.path.join(output_dir, filename)):
        raise HTTPException(status_code=404,
                            detail="The original clip is no longer available.")

    new_url = f"/videos/{req.job_id}/{filename}"
    if req.clip_index < len(job.get('result', {}).get('clips', [])):
        job['result']['clips'][req.clip_index]['video_url'] = new_url
    try:
        clips[req.clip_index]['video_url'] = new_url
        data['shorts'] = clips
        write_json_atomic(json_files[0], data, indent=4)
    except Exception as e:
        print(f"⚠️ Failed to update metadata.json: {e}")

    return {"success": True, "new_video_url": new_url}


class HookRequest(BaseModel):
    job_id: str
    clip_index: int
    text: Optional[str] = ""
    input_filename: Optional[str] = None
    # auto (off the faces and the captions, hooks/hook_placement), or an
    # explicit top / center / bottom, which is drawn exactly there.
    position: Optional[str] = "auto"
    size: Optional[str] = "M" # S, M, L
    duration_seconds: Optional[float] = None  # None = hook visible for the whole clip
    style: Optional[str] = "pill"  # pill/classic/dark/yellow/red/outline/outline_yellow
    font: Optional[str] = None  # montserrat/anton/serif (hooks.HOOK_FONTS); None = the style's own
    remove: Optional[bool] = False  # strip the burned hook instead of adding one

@app.post("/api/hook")
async def add_hook(req: HookRequest, request: Request):
    if req.job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[req.job_id]
    output_dir = os.path.join(OUTPUT_DIR, req.job_id)
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
    
    if not json_files:
        raise HTTPException(status_code=404, detail="Metadata not found")
        
    with open(json_files[0], 'r') as f:
        data = json.load(f)
        
    clips = data.get('shorts', [])
    if req.clip_index >= len(clips):
        raise HTTPException(status_code=404, detail="Clip not found")
        
    clip_data = clips[req.clip_index]
    
    # Video Path
    if req.input_filename:
        filename = os.path.basename(req.input_filename)
    else:
        filename = clip_data.get('video_url', '').split('/')[-1]
        if not filename:
             base_name = os.path.basename(json_files[0]).replace('_metadata.json', '')
             filename = f"{base_name}_clip_{req.clip_index+1}.mp4"
         
    input_path = os.path.join(output_dir, filename)
    if not os.path.exists(input_path):
        raise HTTPException(status_code=404, detail=f"Video file not found: {input_path}")

    if not req.remove and not (req.text or "").strip():
        raise HTTPException(status_code=400, detail="Hook text is required")

    # Same invariant as /api/edit: derive from the clip WITHOUT its burned
    # captions, then put them back on top, so a later restyle never stacks a
    # second caption layer (see _reapply_captions). The hook layer is stripped
    # too: a new hook REPLACES the burned one (auto-hook or a previous manual
    # one) instead of stacking on top of it.
    clean_name = _strip_burned_captions(output_dir, filename, materialize=False)
    had_captions = clean_name != filename
    clean_name = _strip_burned_hook(output_dir, clean_name)
    filename = clean_name
    input_path = os.path.join(output_dir, clean_name)

    if req.remove:
        # Nothing to burn: the hook-less file is the target; captions (if the
        # clip had them) go back on below.
        output_filename = filename
        output_path = input_path
    else:
        output_filename = f"hooked_{int(time.time())}_{filename}"
        output_path = os.path.join(output_dir, output_filename)

        # Map Size to Scale
        size_map = {"S": 0.8, "M": 1.0, "L": 1.3}
        font_scale = size_map.get(req.size, 1.0)

        try:
            # Run in thread pool
            def run_hook():
                return add_hook_to_video(
                    input_path, req.text, output_path, position=req.position or "auto",
                    font_scale=font_scale, duration=req.duration_seconds, style=req.style,
                    font=req.font, layout_ranges=clip_data.get('layout_ranges'),
                    has_captions=had_captions)

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, run_hook)

        except Exception as e:
            print(f"❌ Hook Error: {e}")
            raise HTTPException(status_code=500, detail=str(e))


    # Captions back on top (see /api/edit for the same invariant).
    if had_captions:
        recap = await asyncio.get_event_loop().run_in_executor(
            None, _reapply_captions, req.job_id, req.clip_index, output_path)
        if recap:
            output_filename = os.path.basename(recap)


    # Record the burned hook so the editor knows what the clip carries (the
    # auto-hook pipeline writes the same key).
    if req.remove:
        clip_data.pop('auto_hook', None)
    else:
        clip_data['auto_hook'] = {
            "text": req.text, "style": req.style, "font": req.font, "position": req.position,
            "duration_seconds": req.duration_seconds,
        }

    # Update Persistence (Same logic as subtitles)
    # Update InMemory Jobs
    if req.clip_index < len(job['result']['clips']):
        mem_clip = job['result']['clips'][req.clip_index]
        mem_clip['video_url'] = f"/videos/{req.job_id}/{output_filename}"
        if req.remove:
            mem_clip.pop('auto_hook', None)
        else:
            mem_clip['auto_hook'] = clip_data['auto_hook']

    # Update Metadata on Disk
    try:
        if req.clip_index < len(clips):
            clips[req.clip_index]['video_url'] = f"/videos/{req.job_id}/{output_filename}"
            data['shorts'] = clips
            write_json_atomic(json_files[0], data, indent=4)
            print(f"✅ Metadata updated with hook video for clip {req.clip_index}")
    except Exception as e:
        print(f"⚠️ Failed to update metadata.json: {e}")


    return {
        "success": True,
        "new_video_url": f"/videos/{req.job_id}/{output_filename}",
        "burned_hook": None if req.remove else clip_data['auto_hook'],
    }

