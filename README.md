# content-pipeline

[![tests](https://github.com/keanii2022/content-pipeline/actions/workflows/tests.yml/badge.svg)](https://github.com/keanii2022/content-pipeline/actions/workflows/tests.yml)

## What it does

It turns a clip whose reuse I've verified is permitted into a finished
short-form video with AI-written commentary and an AI voiceover. AI does
the research and drafting, plain code does the video work, and a person
makes every decision about what gets published.

## How it works

Three AI agents, each with one narrow job (defined in `.claude/agents/`):

| Agent | Job | What it can't do |
| --- | --- | --- |
| `script-writer` | Researches the clip and drafts a 50–80 word commentary script | Pick the clip, fetch media, or call its own script final |
| `content-reviewer` | Checks the draft for tone, framing, and length, then approves it or flags it for a human | Edit anything |
| `code-reviewer` | Reviews code changes to this repo before I approve them | Change code (read-only) |

After the script is approved, plain code (no AI) downloads the clip,
makes the voiceover, stitches the video together in 9:16, and checks it
against TikTok/Reels/Shorts specs.

**Where a human approves:**

1. **Which creators are allowed.** Only creators on a hand-kept allowlist
   (`data/permissions/allowlist.yaml`) can be sourced. Each entry has
   evidence of why reuse is allowed (right now: NASA, whose media is
   public domain). Every job records which permission it relied on.
2. **Which clip.** The research pass suggests clips; a person picks one.
3. **Flagged scripts.** If `content-reviewer` flags a script, it waits
   for a person.
4. **What's worth posting.** Finished videos land in a local review
   dashboard showing the source, permission record, and format check.
   A person decides.
5. **Publishing.** Manual everywhere. The one exception is YouTube, and
   that still needs a person to click Publish and type the title for
   every video. Publishing is never chained to the pipeline finishing.

## How I know it works

The release gates and the word-count rule have tests that run on every
push (badge above). They run offline: the network is blocked and the
YouTube login is faked, so they can't post anything. Real output:

```
test_publish_refuses_job_missing_permission_provenance PASSED
test_publish_refuses_job_without_passed_format_check[format-check-never-ran] PASSED
test_publish_refuses_job_without_passed_format_check[format-check-failed] PASSED
test_publish_lets_an_eligible_job_through_to_login PASSED
test_content_reviewer_word_limit_matches_code PASSED
test_word_count_accepts_scripts_at_its_limits[50] PASSED
test_word_count_accepts_scripts_at_its_limits[80] PASSED
test_word_count_rejects_scripts_just_outside_its_limits[49] PASSED
test_word_count_rejects_scripts_just_outside_its_limits[81] PASSED
9 passed
```

I also checked that the tests catch real breakage. I broke the code on
purpose in a scratch copy: I removed the permission check, loosened the
format check, and moved the word limits. Each change made a test fail.

## A bug I found, and what it changed

The reviewer agent's instructions said scripts should be "roughly 75–200
words". The code actually rejects anything outside 50–80. So the AI
reviewer was approving scripts that the next step would throw out. I
fixed the instructions to match the code. Then I added
`test_content_reviewer_word_limit_matches_code`, which reads the agent's
instructions and fails if their word range ever drifts from the limit
the code enforces. What I took from it: an agent's prompt is part of the
system, so it gets tested like code.

## How to run it

Needs Python 3.9+, Node 18+, and `ffmpeg` and `yt-dlp` installed.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[test]"
.venv/bin/pytest -v                 # the tests above

cp .env.example .env                # add your ElevenLabs key for voiceovers
cd review-app && npm install && node server.js   # dashboard at http://localhost:4173
```

Pipeline stages can also be run from the command line:

```bash
python -m pipeline.orchestrate.run_single discover <creator_id>
python -m pipeline.orchestrate.run_single auto-finish <job_id> <creator_id> <batch_id> <candidate_index> <profile>
python -m pipeline.orchestrate.run_single publish <job_id> "<title>"
```

YouTube publishing needs your own Google Cloud OAuth client saved as
`credentials.json` and `pip install -e ".[youtube]"`.

## How I build it with Claude Code

`CLAUDE.md` holds the project's ground rules for Claude Code. It
covers sourcing only permitted clips and treating permission checks as
safety-critical. It also keeps publishing a manual, human action, and
says any request to automate it is a scope change to raise, not
something to just do. The agent files in `.claude/agents/` keep each AI
step's job, tools, and limits separate.

## Status

- **Working:** permission checks, clip fetch, voiceover, video assembly,
  format checks, batch runs, and the review dashboard.
- **Built but not yet run for real:** the YouTube upload. The safety
  checks before it are tested, but I haven't connected my own Google
  account to do a live upload yet.
- Runs locally only; there's no hosted version.
