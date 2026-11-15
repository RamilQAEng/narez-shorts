"""Re-render clips of a finished job with a project's two-box layout, to look at the result.

    python -m scripts.project_demo JOB_DIR PROJECT_JSON 0 2 3 [--clips clips.json] [--out DIR]

(run from the repo root)

Cuts each clip from the job's source video (clips.json times), lays it out with project_layout
(zoom_moments from the clip entry), then burns the hook and the default captions like the pipeline.
"""
import argparse
import glob
import json
import os

import layout_ranges
import main as m
import project_layout
import projects


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("job_dir")
    ap.add_argument("project_json")
    ap.add_argument("indexes", type=int, nargs="+")
    ap.add_argument("--clips", help="clips.json to read (default: the job's own)")
    ap.add_argument("--out", default=os.path.join("output", "_demo_projects"))
    a = ap.parse_args()

    with open(a.project_json, encoding="utf-8") as f:
        project = projects.clean(json.load(f))
    with open(a.clips or os.path.join(a.job_dir, "clips.json"), encoding="utf-8") as f:
        shorts = json.load(f)["shorts"]
    with open(os.path.join(a.job_dir, "transcript.json"), encoding="utf-8") as f:
        transcript = json.load(f)
    source = next(p for p in glob.glob(os.path.join(a.job_dir, "*.mp4"))
                  if "_clip_" not in p and not os.path.basename(p).startswith(("temp_", "scenes_", "subtitled_", "hooked_")))
    os.makedirs(a.out, exist_ok=True)

    for i in a.indexes:
        clip = shorts[i]
        start, end = clip["start"], clip["end"]
        cut, final = (os.path.join(a.out, f"{n}_{i + 1}.mp4") for n in ("cut", "project_clip"))
        m.cut_clip(source, cut, start, end, i + 1)
        ok = project_layout.render(cut, final, project, clip.get("zoom_moments"), start, end)
        os.remove(cut)
        if not ok:
            print(f"clip {i + 1}: layout failed")
            continue
        clip["layout_ranges"] = layout_ranges.read(final)
        path = final
        hooked = m.auto_hook_clip(path, clip)  # (path, config) or None
        if hooked:
            path = hooked[0]
        path = m.auto_caption_clip(path, transcript, start, end) or path
        print(f"clip {i + 1}: {path}")


if __name__ == "__main__":
    main()
