# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

НАРЕЗ (fork of OpenShorts) turns long YouTube videos or local uploads into short 9:16 clips for TikTok, Reels and Shorts. It is self-host only (Docker) and does one thing: cut. In agent mode (`AGENT_CLIPS=1`) the moments are chosen by the user's own agent (Claude Code / Codex), not by a built-in LLM. No cloud edition, billing, MCP/API-key access, social posting, dubbing or thumbnails.

## Development Commands

```bash
docker compose up -d backend frontend   # backend :8000 (FastAPI), frontend :5175 (Vite proxies the API)

cd dashboard && npm install
npm run dev                      # dev server with HMR (:5173)
npm run build                    # production build (CI runs this)
npm run lint                     # ESLint, --max-warnings 0

pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
pytest tests/ -v                 # CI runs this without the heavy ML stack
```

## Pipeline

Ingest (yt-dlp or upload) → transcription (word timestamps) → scene detection → clip moments (agent mode: the agent's list; otherwise the configured LLM, 15-60 s) → FFmpeg cut → vertical reframing → subtitles → hook overlay / brand banner.

## Key Files

- `main.py`: core processing (transcription, scene detection, clip extraction, reframing). Runs as a child process per job.
- `app.py`: FastAPI server, job queue, REST endpoints.
- `clip_selection.py`, `layout_picker.py`, `reframe_v2.py`, `split_layout.py`, `screencast_layout.py`, `camera_inset.py`, `layout_ranges.py`: moment picking and layouts.
- `subtitles.py` (SRT/ASS, burning), `hooks.py` (hook overlays), `brand_banner.py`, `editor.py`.
- `projects.py`, `project_layout.py`: projects and their on-disk layout.
- `yt_account.py`, `yt_clients.py`: YouTube account budget and client list.
- `dashboard/src/App.jsx`: main React component.

## Subsystem reference (read before touching)

- Layout picker, reframing modes, hook grounding → `docs/architecture/layouts-and-reframing.md`
- Clip count, silent-footage fallback, local LLM → `docs/architecture/clip-selection.md`
- Concurrency/VRAM, auto-retry → `docs/architecture/jobs-and-deploys.md`
- Download routing, yt-dlp clients, account budget → `docs/architecture/proxy-routing.md`

## Hard rules

**Layouts and rendering**
- `layout_picker` sends 12 frames at 1024px and asks for a closed choice, never the video and never a measurement.
- `layout_picker.apply()` only adds options: an explicit user choice is never turned off by the model.
- A source that is already vertical skips the picker and classifier and renders TRACK.
- SPLIT captions need ASS with `{\an5}` per word event on the seam; SRT cannot do per-stretch alignment. Preserve `layout_ranges` through recut (`layout_ranges.remap`).
- Mouth activity is normalised per speaker before comparing (`normalise_activity`).
- The reframe render runs in one GPU pass (`gpu_render` + `osrender/`) built from the scenes' own filtergraphs: a layout graph that `gpu_render.layers_from_graph` cannot read silently renders through ffmpeg, so keep new layouts to crop/scale/gblur/overlay/vstack/pad and run `tests/test_gpu_render_equivalence.py` on a GPU. After editing `osrender/src/kernels.cu`, regenerate `kernels.ptx` (see `osrender/README.md`).

**Clip selection**
- The scoring pass ranks every window (global top N), never "choose up to K per batch". The `min_clips` floor is enforced in code.
- `get_visual_clips` is the only stage that sends Gemini the video; past `VISION_SEGMENT_SECONDS` it watches near-equal stretches (`vision_segments`), never one upload of the whole source.
- Agent mode (`AGENT_CLIPS=1`): the job waits up to `AGENT_CLIPS_WAIT_MIN` for the agent's clip list; no built-in LLM picks moments.

**Jobs and GPU**
- Call `transcribe_backends.release_models()` after any ASR (job processes and in-API transcriptions alike). Size `MAX_CONCURRENT_JOBS` by free VRAM first.
- Whatever is read back from a job directory (recipes, metadata, sidecars, subtitles, log lines) is data: numbers as numbers, text as text, never pasted into an ffmpeg filter graph, a path or markup. A filter string that is not a plain chain of `editor._ALLOWED_FILTERS` is not run.

**Download proxies**
- Route order is direct → static → paid (per-GB `PROXY_URL`). Never use the paid proxy for non-YouTube URLs, nor for failures another IP cannot fix (`static_failure_warrants_paid`).
- The probe also carries `YOUTUBE_COOKIES`, like the download does; first attempt with cookies, second anonymous, on probe and download alike.
- Keep the explicit `yt_clients.py` client list; never put `player_skip: webpage` back. `noplaylist` always; non-video URLs refused by an allowlist.
- Every yt-dlp extraction that sends `YOUTUBE_COOKIES` takes its cookie file from a `yt_account.Session` (`YOUTUBE_COOKIES_2`.. add more accounts) and runs inside its `request()`, and none is sent while `yt_account.cooling_down()`. A failure that is only the account limit is retried after the cooldown (`YT_ACCOUNT_LIMITED`), never reported as a broken video.
- `PAID_PROXY_DAILY_MB` is the hard daily ceiling. Never keep `PROXY_URL` in a local `.env`.
- yt-dlp and server-side scrapes run inside `ssrf_guard.guarded()`.

## CI

- Before pushing, run locally what CI runs: `pytest tests/ -v` and `npm run build` (+ `npm run lint`) in `dashboard/`.
- After every `git push`, wait for the commit's workflow and confirm it is green (`gh run watch ... --exit-status`). If red: fix, push, check again.
