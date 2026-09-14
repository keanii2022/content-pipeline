# content-pipeline — Build Plan

Scope reminder (from [CLAUDE.md](CLAUDE.md)): source clips only from creators
who've explicitly permitted reuse, add AI-generated voiceover commentary,
assemble into 9:16 short-form video for TikTok/Reels/Shorts, and stop —
publishing is always a manual, human action outside this pipeline.

Language split: Python for all media-processing/pipeline logic (fetch, TTS,
ffmpeg assembly, format validation, orchestration); Node for the review
dashboard only (Steps 10, 12).

Agent use: candidate clip discovery (Step 2) uses the `researcher` agent to
surface options for you to pick from; commentary script drafting (Step 5)
uses the dedicated `script-writer` agent (project-local:
`.claude/agents/script-writer.md`) for a first-pass draft, which the
`content-reviewer` agent then screens for framing/tone. **As of Step 14,
that content-reviewer screen is the gate** — a per-script human
read-through is no longer required before the pipeline continues. You can
still open and edit any `work/<job_id>/script.md` by hand at any point;
nothing prevents that. But it's no longer a blocking checkpoint the
pipeline waits on. Separately, the `code-reviewer` agent (project-local,
mirrors the global `reviewer` agent) screens the actual pipeline code
produced by Steps 1, 3, 4, 6, 7, 9, and 14 before each is committed.

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

- **Status:** done
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

- **Status:** done
- **Scope:** create `raw/`, `work/`, `staged/` directories (each with
  `.gitkeep`; binary contents ignored via `.gitignore`, already in place).
  *(Actual: `work/.gitkeep` and `staged/.gitkeep` are tracked; `raw/` has no
  tracked `.gitkeep` because `.gitignore`'s `/raw/` rule ignores the whole
  directory, not just media files in it — see docs/pipeline-stages.md.)*
  Add `docs/pipeline-stages.md` documenting the stage boundary rule:
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

- **Status:** done
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

- **Status:** done
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

- **Status:** done
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

- **Status:** done
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

- **Status:** done
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

- **Status:** done
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

- **Status:** done
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

- **Status:** done
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

## 12. Review dashboard: batch run visibility

- **Status:** done
- **Scope:** extend `review-app/` (Node) to also surface Step 11's batch
  runs: a new `GET /api/runs` (list every `data/jobs/<run_id>/state.json`)
  and `GET /api/runs/:run_id` (one run's detail) in `review-app/server.js`,
  plus a "Batch Runs" view in `review-app/public/` listing each run's
  creators with the same derived stage/next-action a human would get from
  `run_batch.py status <run_id>` (recomputed in JS from the same state
  fields — `batch_id`/`job_id`/`clip_id`/`voiceover_generated`/`assembled`/
  `format_profile`/`format_passed` — not read from a separately stored
  status string, so it can't drift from `_describe_entry`'s logic the way
  two independent implementations of the same derivation could). Purely
  read-only: this view is for seeing where a run stands, not for driving
  it.
- **Dependencies:** Step 10 (`review-app/` already exists — server/client
  conventions, static-file serving, `isSafePathComponent`-style path
  safety), Step 11 (`data/jobs/<run_id>/state.json` schema to read).
- **Out of scope:** the dashboard must never write to `data/jobs/` —
  `state.json` stays owned and mutated only by `run_batch.py`, the same
  way Step 10 never writes outside `staged/`. No button or endpoint that
  triggers any pipeline stage (discover/fetch/voiceover/assemble/etc.)
  from the dashboard — advancing a run stays a CLI action. No changes to
  Step 10's existing staged-job review flow. Still never calls any
  posting/publishing API.

## 13. Control panel: candidate picker, creators/content lists, action buttons

- **Status:** done
- **Scope:** extend `review-app/` into a fuller control panel, so a batch
  run's candidate-selection and deterministic stages can be driven from
  the UI instead of the CLI, cutting toward the goal of prepping ~3
  approved videos/week with less manual command-running. Discovery and
  script drafting still require a live Claude Code session (they hand off
  to the `researcher`/`script-writer`/`content-reviewer` agents, which no
  plain web server can invoke) — those stay chat-driven exactly as today;
  this step only takes over the parts of `run_batch.py`/`run_single.py`
  that are already deterministic Python with no agent involved:
  - **Candidates view**: `GET /api/candidates` (and
    `/api/candidates/:creator_id`) lists recorded batches from
    `data/candidates/`; clicking a candidate calls a new endpoint that
    wraps `pipeline.script.draft.start_script_job` (creates the job dir,
    returns the script-writer prompt for you to hand to Claude) — this
    mirrors `select-candidate`, which needs no agent itself even though
    the very next step does.
  - **Creators list**: `GET /api/creators` reads and displays
    `data/permissions/allowlist.yaml` (read-only) — requires adding a
    YAML-parsing dependency to `review-app/` (e.g. `js-yaml`), the first
    real npm dependency it's had; Steps 10/12 needed none.
  - **Action buttons**, each wrapping the matching deterministic pipeline
    function directly (not shelling out to the CLI): fetch
    (`pipeline.fetch.fetch_clip`), voiceover
    (`pipeline.voiceover.generate.generate_voiceover`), assemble
    (`pipeline.assemble.assemble.assemble_clip`), format-check
    (`pipeline.format.validate.validate_format`). Each button's result
    (success/failure, output path) is shown in the UI the same way the
    CLI prints it today.
  - This **deliberately reverses** Step 10 and Step 12's "no button or
    endpoint triggers a pipeline stage" rule, scoped narrowly to the four
    stages above (plus candidate-selection's job-creation step) — not a
    blanket reversal. Discovery, script drafting, and the human
    script-approval checkpoint are explicitly excluded from that reversal
    and stay exactly as manual as they are today.
- **Dependencies:** Step 10 (`review-app/` conventions), Step 12 (batch
  run state to display alongside), Step 11 (`run_batch.py`'s underlying
  functions, called directly rather than via its CLI), the pipeline
  modules each button wraps (Steps 4/6/7/9).
- **Out of scope:** still never calls any posting/publishing API — the
  finished, approved video sitting in `staged/` is the end of the line;
  uploading to YouTube stays a manual action you take yourself. No
  discover/script-drafting automation — those still need you and Claude
  in a live session. No changes to the permissions ledger from the UI
  (creators list is read-only). No scheduling/calendar features in this
  step — just making the deterministic stages clickable.

## 14. Auto-finish: collapse fetch/voiceover/assemble/check-format into one action

- **Status:** done
- **Scope:** `pipeline/orchestrate/auto_finish.py` (`auto_finish_job`),
  wired into `pipeline/control/api.py` as the `auto-finish` action and into
  `run_single.py` as the `auto-finish` CLI subcommand, plus a matching
  "Auto-finish" button in `review-app/public/app.js`. Once a job's
  `work/<job_id>/script.md` exists and has passed the `content-reviewer`
  agent's screen, `auto-finish` runs fetch, voiceover, assemble, and
  check-format back to back and lands the result in `staged/` — one action
  instead of four separate manual clicks/commands, each previously
  requiring you to wait and confirm before the next.
- **Why:** you don't have time to read and approve every script by hand
  before a batch can finish. The human checkpoint for a given clip moves
  from "approve the script text mid-pipeline" to "skim the finished list
  in the control panel and pick what sounds good" — a ~30-second glance at
  titles/sources, not a per-video watch, unless you want to watch one.
  `auto-finish` still refuses to run (via `load_script`'s well-formedness
  check) if no script exists yet for that job, so it can't silently
  process an empty or malformed draft.
- **What stays exactly as before:** discovery and script drafting still
  require a live Claude Code session to invoke the `researcher` and
  `script-writer` agents — nothing here automates picking a candidate or
  writing a script. The permissions ledger gate (Step 1) is unchanged and
  still re-validated inside `fetch_clip`/`load_candidate`. Uploading a
  staged, approved video to YouTube (or any platform) is still a fully
  manual action you take yourself outside this repo — copy the file, drop
  it into YouTube's uploader, write a title, hit publish. Nothing here
  calls a posting/publishing API; see `docs/pipeline-stages.md`'s boundary
  rule, which this step does not touch.
- **Dependencies:** Steps 4, 6, 7, 9 (the four functions this chains),
  Step 13 (control panel button), the `code-reviewer` agent.
- **Out of scope:** no change to discovery or script-writer/content-reviewer
  agent invocation; no change to the permissions ledger; no
  posting/publishing integration of any kind; no removal of the
  `load_script` well-formedness check.
