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
- **Publishing is manual, not automated.** This pipeline prepares finished
  video files only. Every finished piece requires manual review and approval
  before it is posted anywhere. No component of this project should
  auto-publish, auto-upload, or auto-post content to any platform.

## Working with this repo

- Treat any code that touches source-clip permissions or licensing metadata
  as safety-critical — don't relax or bypass those checks.
- Keep the publish step a manual, explicit action (e.g. a human running a
  command or clicking a button) rather than something triggered
  automatically by the pipeline finishing.
