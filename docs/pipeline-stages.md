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

Clip mode (Steps 16–22) uses the same three stages for a different job:
cutting short clips out of one long video, with the original audio.

- **`raw/clip/<source_id>/`** — the source video and a `metadata.json`
  recording where it came from and its `rights_basis`: `allowlist` (the
  channel is on the allowlist), `own` (the owner's own footage) or
  `unverified` (reuse rights have not been checked). Clip mode takes any
  URL or local file, so the allowlist is not a gate here; the recorded
  provenance is what later stages rely on.
- **`work/clip-<source_id>/`** — the transcript and the recorded
  highlights, each carrying the source's provenance block unchanged.
- **`staged/<job_id>/`** — one cut clip per highlight, in the same
  finished list as commentary jobs. Its `manifest.json` has `mode: "clip"`,
  the highlight it was cut from, the format-compliance result, and the
  provenance block under `source`.

## The boundary rule

**Nothing in this repo reads from `staged/` in order to post content
anywhere.** No component — no script, no scheduled job, no orchestrator
step (see Step 8's `run_single.py` and Step 11's future `run_batch.py`) —
is permitted to upload, publish, or otherwise transmit a `staged/` output
to any external platform or posting API. The review dashboard (Step 10)
may read and annotate `staged/*/manifest.json` (e.g. toggling a
`reviewed`/`approved` flag), but that flag is a note for a human, not a
trigger for any automated action.

Moving a finished piece out of `staged/` and onto an actual platform is a
manual, external action performed by a person, with one named exception:
[`pipeline/publish/youtube.py`](../pipeline/publish/youtube.py) (Step 15),
which uploads a `staged/<job_id>/` output to YouTube via the Data API.
That exception is narrow and explicit, not a general permission to wire up
publishing:

- It's YouTube only. TikTok, Reels, and any other platform still get a
  finished file and nothing more — this codebase does not talk to their
  APIs.
- It still requires a human to trigger it per job (the `publish` CLI
  subcommand or the control panel's "Publish to YouTube" button) and to
  supply the title. Nothing chains it onto `auto-finish` or any other
  stage automatically.
- It refuses to run against a job with no recorded provenance or a
  format-compliance check that's missing or failed — see
  `pipeline.publish.youtube._check_eligibility`. What counts as
  provenance depends on the mode:
  - Commentary jobs need a recorded `permission_ledger_reference`.
  - Clip-mode jobs need their `source` provenance block, complete and
    still matching the source record in `raw/clip/<source_id>/`. If that
    record has been deleted, only an `unverified` clip can still go out
    (it claims nothing that needs backing); `allowlist` and `own` are
    refused. Then the `rights_basis` decides: `allowlist` is checked against the ledger
    again at publish time, `own` goes through, and `unverified` is
    refused unless the person publishing acknowledges it in that same
    action — the "I've confirmed I have the right to post this clip"
    checkbox in the control panel (unticked every time), or
    `--acknowledge-unverified-rights` on the `publish` command. The
    acknowledgment and its timestamp are written to the manifest before
    the upload starts. A recorded acknowledgment is a record of one
    upload; it is never read back as permission for the next.
- Cutting never leads to an upload. Clip mode ends at `staged/` exactly
  as commentary mode does.

If a future change needs to touch this boundary further (another
platform's API, or auto-triggering an upload), that is a scope change to
raise explicitly, not something to introduce incidentally while building
on top of `staged/`.

## Related conventions

Directory layout for permission records lives under
[`data/permissions/allowlist.yaml`](../data/permissions/allowlist.yaml)
(validated against `allowlist.schema.json` in the same directory) (Step 1);
candidate-discovery output lives under `data/candidates/<creator_id>/`
(Step 2). Both are separate from the `raw/` / `work/` / `staged/`
processing stages described above.
