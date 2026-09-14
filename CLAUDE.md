# content-pipeline

A content-automation pipeline for short-form video. It sources clips from
creators who have explicitly permitted clipping/reuse of their content, adds
original commentary (either the owner's own voice or an AI-generated
voiceover), and assembles the result into short-form videos ready for
posting.

## Scope and boundaries

- **Sourcing**: only clips from creators who have explicitly granted
  permission to clip/reuse their content. Provenance and permission for each
  source clip should be tracked, not assumed.
- **Commentary**: original commentary layered on top of sourced clips, via
  either a recorded human voiceover or an AI-generated one.
- **Assembly**: combining clip + commentary (and any supporting captions,
  overlays, etc.) into a finished short-form video file.
- **Publishing is manual by default, with one explicit, scoped exception:
  YouTube.** `pipeline/publish/youtube.py` uploads a staged, eligible job
  via the YouTube Data API — see PLAN.md Step 15 for exactly what
  "eligible" means and why this is the only platform this repo talks to
  directly. Every other platform (TikTok, Reels) and every other path
  through this pipeline stays exactly as before: no auto-publish,
  auto-upload, or auto-post. Uploading a staged video anywhere other than
  YouTube is still a fully manual action taken outside this repo.
- **The human checkpoint is the control panel's finished list, not a
  per-script approval mid-pipeline** (see PLAN.md Step 14). Once
  `content-reviewer` has screened a drafted script, `auto-finish` runs
  fetch/voiceover/assemble/check-format without pausing for a human to
  read the script first — the review point is skimming staged output in
  `review-app` (title, source, format-check result) and deciding what's
  worth uploading, not approving every script's text before it's used.
- **Publishing to YouTube still requires a human click and a human-typed
  title, every time** (see PLAN.md Step 15). Nothing calls `upload_video`
  automatically after `auto-finish` — the two are deliberately not chained.
  Treat any future request to chain them (or to add a second platform's
  API) as a scope change to raise explicitly, the same way
  `docs/pipeline-stages.md` already asks for the original boundary.

## Working with this repo

- Treat any code that touches source-clip permissions or licensing metadata
  as safety-critical — don't relax or bypass those checks.
- Keep the publish step a manual, explicit action (e.g. a human running a
  command or clicking a button) rather than something triggered
  automatically by the pipeline finishing.
