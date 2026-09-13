# content-pipeline — Build Plan

Scope reminder (from [CLAUDE.md](CLAUDE.md)): source clips only from creators
who've explicitly permitted reuse, add AI-generated voiceover commentary,
assemble into 9:16 short-form video for TikTok/Reels/Shorts, and stop —
publishing is always a manual, human action outside this pipeline.

Language split: Python for all media-processing/pipeline logic (fetch, TTS,
ffmpeg assembly, format validation, orchestration); Node for the review
dashboard only (Step 10).

Agent use: candidate clip discovery (Step 2) uses the `researcher` agent to
surface options for you to pick from; commentary script drafting (Step 5)
uses the dedicated `script-writer` agent (`~/.claude/agents/script-writer.md`)
for a first-pass draft, which the `content-reviewer` agent then screens for
framing/tone before it reaches you — you still review/edit the script
yourself; neither agent's output is ever final or auto-approved. Discovery
still has no dedicated custom agent yet; it can be handed to a purpose-built
one later without changing the pipeline's structure. Separately, the
`code-reviewer` agent (project-local, mirrors the global `reviewer` agent)
screens the actual pipeline code produced by Steps 1, 3, 4, 6, 7, and 9
before each is committed.

---

## 1. Permissions ledger schema & validator

- **Status:** done
- **Scope:** `data/permissions/allowlist.schema.json` (schema definition),
  `data/permissions/allowlist.yaml` (starts empty/with one example entry),
  `pipeline/permissions/__init__.py`, `pipeline/permissions/validate.py`
  (loads the ledger, validates entries against the schema, exposes a
  lookup function `is_permitted(creator_id, source_url) -> bool`). Before
  this code is committed, the `code-reviewer` agent screens the diff;
  findings are addressed first.
- **Dependencies:** none (plus the `code-reviewer` agent definition already
  existing at the project level).
- **Out of scope:** no fetch tooling, no TTS, no assembly, no UI. This step
  does not download or process any actual media — it only defines and
  validates the permission records that later steps will be gated on.

## 2. Candidate clip discovery

- **Status:** current
- **Scope:** `pipeline/discover/__init__.py`, `pipeline/discover/find_candidates.py`
  — invokes the `researcher` agent, scoped to a single permitted creator
  looked up via `pipeline.permissions.validate.is_permitted`, to scan that
  creator's recent public content and surface candidate clips/timestamps
  (URL, timestamp range, short rationale) worth using. Output is written to
  `data/candidates/<creator_id>/<batch_id>.json` for you to review and pick
  from or reject.
- **Dependencies:** Step 1 (only scans creators already on the permissions
  ledger).
- **Out of scope:** this step never fetches, downloads, or persists any
  actual media — only candidate metadata (URLs/timestamps/rationale) for
  human selection. No automatic selection of a "best" candidate; you choose.

## 3. Pipeline state-boundary scaffolding

- **Status:** not-started
- **Scope:** create `raw/`, `work/`, `staged/` directories (each with
  `.gitkeep`; binary contents ignored via `.gitignore`, already in place),
  add `docs/pipeline-stages.md` documenting the stage boundary rule:
  *nothing in this repo reads from `staged/` in order to post anywhere;
  moving content out of `staged/` to an actual platform is always a manual,
  external human action.* This step has no application code to speak of, but
  any scripting it does add still goes through the `code-reviewer` agent
  before commit, same as the other code-bearing steps.
- **Dependencies:** Step 1 (shares the `data/` layout convention), the
  `code-reviewer` agent.
- **Out of scope:** no processing logic; no changes to the permissions
  ledger beyond referencing its location in the docs.

## 4. Single-clip fetch tool

- **Status:** not-started
- **Scope:** `pipeline/fetch/__init__.py`, `pipeline/fetch/fetch_clip.py`
  (wraps yt-dlp; takes a candidate you selected from Step 2's output; before
  downloading, re-validates it via
  `pipeline.permissions.validate.is_permitted`; on success writes the clip
  and a `metadata.json` — source URL, creator id, permission-ledger
  reference, originating candidate-batch id, fetch timestamp — into
  `raw/<creator_id>/<clip_id>/`). Before this code is committed, the
  `code-reviewer` agent screens the diff; findings are addressed first.
- **Dependencies:** Step 1 (ledger + validator), Step 2 (candidate to fetch
  comes from here), Step 3 (`raw/` convention), the `code-reviewer` agent.
- **Out of scope:** no batch/multi-clip fetching, no voiceover, no
  assembly, no format checks. Fetching a clip not on the allowlist must
  fail loudly, not warn-and-continue.

## 5. Commentary script drafting

- **Status:** not-started
- **Scope:** `pipeline/script/__init__.py`, `pipeline/script/draft.py` —
  invokes the dedicated `script-writer` agent
  (`~/.claude/agents/script-writer.md`) with a selected candidate's metadata
  (Step 2's rationale/description) to produce a first-pass commentary script
  as plain text, written to `work/<job_id>/script.md`. Before it reaches you,
  the `content-reviewer` agent screens that draft for framing/tone issues.
  The draft (and the content-reviewer's notes) are never passed onward
  automatically: you review and edit `script.md` directly, and only the
  version you've approved is used as input to Step 6.
- **Dependencies:** Step 2 (candidate metadata to write about), Step 3
  (`work/` convention), the `script-writer` agent definition already existing
  at `~/.claude/agents/script-writer.md`, and the `content-reviewer` agent.
- **Out of scope:** no voiceover generation, no video processing. This step
  produces a text draft only — it does not decide the script is final; that
  decision is yours.

## 6. AI voiceover generation

- **Status:** not-started
- **Scope:** `pipeline/voiceover/__init__.py`, `pipeline/voiceover/generate.py`
  (takes your human-approved `work/<job_id>/script.md` from Step 5 as input
  text, calls a configured TTS provider, writes the resulting audio to
  `work/<job_id>/voiceover.<ext>`); TTS provider/API key read from `.env`
  (already gitignored). Before this code is committed, the `code-reviewer`
  agent screens the diff; findings are addressed first.
- **Dependencies:** Step 3 (`work/` convention), Step 5 (approved script
  text to synthesize), the `code-reviewer` agent.
- **Out of scope:** no recorded-human-voice path (explicitly deferred per
  your answer — AI voiceover only for now), no video processing, no clip
  fetching, no batching over multiple scripts.

## 7. Single-clip assembly

- **Status:** not-started
- **Scope:** `pipeline/assemble/__init__.py`, `pipeline/assemble/assemble.py`
  (ffmpeg invocation combining one raw clip + one voiceover audio track into
  one 9:16 output at `staged/<job_id>/output.mp4`, plus a
  `staged/<job_id>/manifest.json` recording source clip, permissions-ledger
  reference, approved script used, and timestamps). Before this code is
  committed, the `code-reviewer` agent screens the diff; findings are
  addressed first.
- **Dependencies:** Step 4 (clip available in `raw/`), Step 6 (voiceover
  audio available in `work/`), Step 3 (`staged/` convention), the
  `code-reviewer` agent.
- **Out of scope:** no platform format-compliance validation yet (Step 9),
  no batching, no review UI, no writing outside `staged/<job_id>/`.

## 8. End-to-end vertical slice orchestration

- **Status:** not-started
- **Scope:** `pipeline/orchestrate/__init__.py`,
  `pipeline/orchestrate/run_single.py` — a CLI entrypoint that runs
  discovery → (you pick a candidate) → script draft → (you approve/edit
  the script) → fetch → voiceover → assemble for exactly **one** clip/one
  script/one output, so the full chain, including its two human
  checkpoints, can be proven end-to-end.
- **Dependencies:** Steps 1, 2, 3, 4, 5, 6, 7.
- **Out of scope:** no batch/queue logic, no scheduling, no review UI, no
  format validation (Step 9 comes after the slice is proven), and no
  removal of either human checkpoint (candidate selection, script
  approval) — those stay manual even once this is automated end-to-end.

## 9. Platform format-compliance profile & validator

- **Status:** not-started
- **Scope:** `pipeline/format/profiles.py` (or `.yaml`) defining specs for
  TikTok/Reels/Shorts (aspect ratio, max duration, codec/bitrate) as named,
  config-driven profiles; `pipeline/format/validate.py` checks a staged
  output against a chosen profile and records pass/fail + reasons into that
  job's `manifest.json`. Wired into `run_single.py` as the step after
  assembly. Before this code is committed, the `code-reviewer` agent
  screens the diff; findings are addressed first.
- **Dependencies:** Step 7 (staged output + manifest exist), Step 8 (wired
  into the single-job run), the `code-reviewer` agent.
- **Out of scope:** no platforms beyond the three named; validation only —
  no automatic re-encoding or fixing of non-compliant output.

## 10. Local review dashboard (Node)

- **Status:** not-started
- **Scope:** new `review-app/` directory (Node project — `package.json`,
  a small server + frontend) that reads `staged/*/manifest.json`, lets you
  browse jobs, preview the video, see the format-compliance result, and
  toggle a `reviewed`/`approved` flag in that job's manifest.
- **Dependencies:** Step 7 (staged output + manifest format), Step 9
  (manifest includes format-compliance result to display).
- **Out of scope:** the dashboard must never call any posting/publishing
  API and must never write to `raw/`, `work/`, `data/candidates/`, or the
  permissions ledger — read/annotate `staged/` manifests only. Marking
  something "approved" here is a note for you, not a trigger for anything
  automated.

## 11. Batch/queue generalization

- **Status:** not-started
- **Scope:** `pipeline/orchestrate/run_batch.py` — iterates the permissions
  allowlist (or a new `data/jobs/queue.yaml`) and runs the discovery →
  script-draft → fetch → voiceover → assemble → format-validate chain per
  clip, reusing Steps 2–9 unchanged, including both human checkpoints per
  clip (candidate selection, script approval) unless you later decide
  otherwise.
- **Dependencies:** Steps 1, 2, 3, 4, 5, 6, 7, 8, 9 (requires the
  single-job path already proven).
- **Out of scope:** no changes to the review dashboard, no changes to the
  manual-approval boundary, no new voice sources or platforms.
