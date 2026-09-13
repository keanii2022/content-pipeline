# Pipeline stage boundaries

This pipeline moves a piece of content through three on-disk stages before
anything is ready for a human to post it anywhere:

- **`raw/`** — clips fetched from permitted creators (Step 4), one
  subdirectory per `<creator_id>/<clip_id>/`, plus a `metadata.json`
  recording provenance (source URL, permissions-ledger reference,
  originating candidate batch, fetch timestamp). Not tracked in git — see
  `.gitignore`'s `/raw/` rule, which ignores the whole directory (not just
  media files in it), so there is no tracked placeholder here the way there
  is for `work/` and `staged/`. A fresh clone will not have a `raw/`
  directory on disk at all; the fetch tool (Step 4) is responsible for
  creating it (e.g. `os.makedirs(..., exist_ok=True)`) as well as
  populating it.
- **`work/`** — in-progress artifacts for a job that aren't final output:
  drafted/approved commentary scripts (Step 5) and generated voiceover
  audio (Step 6), under `work/<job_id>/`.
- **`staged/`** — finished, assembled video output (Step 7) plus its
  `manifest.json` (source clip, permissions-ledger reference, approved
  script, format-compliance result once Step 9 lands), under
  `staged/<job_id>/`. This is the last stage inside the pipeline.

## The boundary rule

**Nothing in this repo reads from `staged/` in order to post content
anywhere.** No component — no script, no scheduled job, no orchestrator
step (see Step 8's `run_single.py` and Step 11's future `run_batch.py`) —
is permitted to upload, publish, or otherwise transmit a `staged/` output
to any external platform or posting API. The review dashboard (Step 10)
may read and annotate `staged/*/manifest.json` (e.g. toggling a
`reviewed`/`approved` flag), but that flag is a note for a human, not a
trigger for any automated action.

Moving a finished piece out of `staged/` and onto an actual platform is
always a manual, external action performed by a person — outside this
codebase, using whatever tool that platform requires. This mirrors the
scope boundary in [CLAUDE.md](../CLAUDE.md): "Publishing is manual, not
automated."

If a future change needs to touch this boundary (e.g. wiring up a
publishing integration), that is a scope change to raise explicitly, not
something to introduce incidentally while building on top of `staged/`.

## Related conventions

Directory layout for permission records lives under
[`data/permissions/allowlist.yaml`](../data/permissions/allowlist.yaml)
(validated against `allowlist.schema.json` in the same directory) (Step 1);
candidate-discovery output lives under `data/candidates/<creator_id>/`
(Step 2). Both are separate from the `raw/` / `work/` / `staged/`
processing stages described above.
