# content-pipeline

[![tests](https://github.com/keanii2022/content-pipeline/actions/workflows/tests.yml/badge.svg)](https://github.com/keanii2022/content-pipeline/actions/workflows/tests.yml)

A content-automation pipeline for short-form video. It sources clips from
creators who've explicitly OK'd reuse, layers an AI-generated voiceover on
top, and assembles a finished 9:16 video — then stops. Publishing is a
manual, human decision everywhere, with one narrow exception: a YouTube
upload path that still requires a human click and a human-typed title
every time.

I built this to get from "here's an interesting clip" to "finished short
with commentary" without babysitting every step by hand, while keeping the
two things that actually matter — content permissions and the decision to
publish — firmly in human hands.

## How it works

- **Discover** — an AI research pass scans a permitted creator's public
  catalog and surfaces candidate clips for a human to pick from.
- **Script** — an AI drafts commentary for the chosen clip; a second AI
  pass screens the draft for tone/framing before anything downstream runs.
- **Fetch → voiceover → assemble → format-check** — deterministic steps,
  no AI involved: download the clip, generate a TTS voiceover, stitch it
  together with ffmpeg into 9:16, and validate the result against
  TikTok/Reels/Shorts specs.
- **Review** — a local dashboard lists finished jobs with their source
  clip, permission record, and format-check result, so a human can decide
  what's actually worth posting.
- **Publish** — manual everywhere except YouTube, where an API button
  still requires a human click and a human-typed title per video.
- **Local only** — no deployed link; the dashboard is a `localhost` tool
  you run alongside the pipeline (see below).

## Tech stack

- **Python** — pipeline logic: clip fetch (`yt-dlp`), TTS voiceover
  (ElevenLabs), video assembly (`ffmpeg`), format validation, and a
  permissions ledger (`PyYAML`)
- **Node.js** — local review dashboard (vanilla JS, no framework)
- **YouTube Data API v3** — the one deliberate, human-triggered publish
  integration

## Running locally

Requires Python 3.9+, Node 18+, and `ffmpeg`/`yt-dlp` on your `PATH`.

```bash
# Pipeline
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env   # fill in your ElevenLabs credentials

# Review dashboard
cd review-app && npm install && node server.js
# -> http://localhost:4173
```

Drive individual pipeline stages from the CLI:

```bash
python -m pipeline.orchestrate.run_single discover <creator_id>
python -m pipeline.orchestrate.run_single auto-finish <job_id> <creator_id> <batch_id> <candidate_index> <profile>
python -m pipeline.orchestrate.run_single publish <job_id> "<title>"
```

...or drive the deterministic stages (fetch/voiceover/assemble/format-check/
select-candidate) from the dashboard's Control Panel tab instead. Discovery
and script drafting are AI-assisted and run through a live Claude Code
session rather than the dashboard.

Publishing to YouTube needs a Google Cloud OAuth client saved as
`credentials.json` in the repo root, and the optional dependency group:
`pip install -e .[youtube]`.

## Status

- **Working:** permissions ledger, clip fetch, TTS voiceover, ffmpeg
  assembly, format validation, batch orchestration, and the review
  dashboard/control panel.
- **Scaffolded, not yet run end-to-end:** YouTube publishing
  (`pipeline/publish/youtube.py`) is fully implemented but I haven't wired
  up my own Google Cloud OAuth client to actually test an upload yet.
- **Not done:** no automated test suite yet.
