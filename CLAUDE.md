# content-pipeline

A content-automation pipeline for short-form video. It has two modes:

- **Commentary mode** (Steps 1–15): sources clips from creators who have
  explicitly permitted clipping/reuse of their content, adds original
  commentary (either the owner's own voice or an AI-generated voiceover),
  and assembles the result into short-form videos ready for posting.
- **Clip mode** (Steps 16+, Ssemble-style): takes any long video — a URL
  or a local file — transcribes it, has an agent pick the best moments,
  and cuts them into 9:16 short clips with the original audio.

The step-by-step build log is `notes/PLAN.md`. It's kept local and is not
committed, so public docs shouldn't point to it.

## Scope and boundaries

- **Sourcing — commentary mode**: only clips from creators who have
  explicitly granted permission to clip/reuse their content (the
  allowlist in `data/permissions/allowlist.yaml`). Unchanged.
- **Sourcing — clip mode**: accepts any URL or local file, by deliberate
  choice (2026-11-13). The allowlist is not a gate here. Instead, every
  clip-mode source must record its provenance and a `rights_basis`:
  - `allowlist` — the source's creator is on the allowlist
  - `own` — the owner's own footage (local upload he marks as his)
  - `unverified` — anything else; reuse rights have not been checked
  Provenance is recorded, never assumed or invented. Rights for
  `unverified` sources are decided by the human at publish time, and that
  risk is the owner's.
- **Commentary**: original commentary layered on top of sourced clips, via
  either a recorded human voiceover or an AI-generated one (commentary
  mode only; clip mode keeps the source audio).
- **Assembly**: combining clip + commentary (and any supporting captions,
  overlays, etc.) into a finished short-form video file.
- **Publishing is manual by default, with one explicit, scoped exception:
  YouTube.** `pipeline/publish/youtube.py` uploads a staged, eligible job
  via the YouTube Data API — see `docs/pipeline-stages.md` and
  `pipeline.publish.youtube._check_eligibility` for exactly what
  "eligible" means and why this is the only platform this repo talks to
  directly. Every other platform (TikTok, Reels) and every other path
  through this pipeline stays exactly as before: no auto-publish,
  auto-upload, or auto-post. Uploading a staged video anywhere other than
  YouTube is still a fully manual action taken outside this repo.
- **Clip-mode jobs publish under the same rules**, plus one more: a job
  whose `rights_basis` is `unverified` can only be published after the
  human explicitly acknowledges it in that publish action (Step 22).
- **The human checkpoint is the control panel's finished list, not a
  per-script approval mid-pipeline.** Once
  `content-reviewer` has screened a drafted script, `auto-finish` runs
  fetch/voiceover/assemble/check-format without pausing for a human to
  read the script first — the review point is skimming staged output in
  `review-app` (title, source, format-check result) and deciding what's
  worth uploading, not approving every script's text before it's used.
  Clip mode works the same way: highlights are cut and staged, and the
  human picks what's worth posting from the finished list.
- **Publishing to YouTube still requires a human click and a human-typed
  title, every time.** Nothing calls `upload_video`
  automatically after `auto-finish` or after clip-mode cutting — the two
  are deliberately not chained. Treat any future request to chain them
  (or to add a second platform's API, or auto-scheduling) as a scope
  change to raise explicitly, the same way `docs/pipeline-stages.md`
  already asks for the original boundary.

## Working with this repo

- Treat any code that touches source-clip permissions, provenance, or
  licensing metadata as safety-critical — don't relax or bypass those
  checks. In commentary mode that means the allowlist gate. In clip mode
  it means provenance and `rights_basis` are always recorded, never
  dropped, defaulted to `own`/`allowlist` without evidence, or faked.
- Keep the publish step a manual, explicit action (e.g. a human running a
  command or clicking a button) rather than something triggered
  automatically by the pipeline finishing.
- An agent's prompt is part of the system: when an agent's instructions
  state a limit the code also enforces (word counts, clip lengths, number
  of highlights), add a test that fails if the two drift apart.
