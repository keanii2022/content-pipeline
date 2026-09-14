// Local review dashboard / control panel server.
//
// Reads staged/*/manifest.json, serves each job's staged video for preview,
// and lets a human toggle a reviewed/approved note on that job's manifest.
// Scope boundary (see PLAN.md Step 10): originally this app only read and
// annotated files under staged/<job_id>/ and never wrote to raw/, work/,
// data/candidates/, or the permissions ledger — Step 13 below narrowly
// extends that (fetch/voiceover/select-candidate now write to raw/ and
// work/ as a deliberate, scoped exception). It still never calls any
// posting/publishing API — marking a job "approved" here is a note for a
// human, not a trigger for anything automated, and nothing here ever
// uploads a finished video anywhere.
//
// Step 12 adds read-only visibility into Step 11's batch runs: GET /api/runs
// and GET /api/runs/:run_id read data/jobs/<run_id>/state.json. This app
// never writes to data/jobs/ — state.json stays owned and mutated only by
// run_batch.py.
//
// Step 13 turns this into a fuller control panel, deliberately reversing
// the "no button triggers a pipeline stage" rule above — but only for the
// stages that need no Claude Code agent: GET /api/creators (read-only,
// parses data/permissions/allowlist.yaml), GET /api/candidates[/:creator_id]
// (read-only, lists data/candidates/ batches), and POST
// /api/actions/:action (select-candidate/fetch/voiceover/assemble/
// check-format), which shells out to `pipeline.control.api` — a thin JSON
// adapter over the exact same functions run_single.py's CLI calls, so
// there is still exactly one implementation of each stage. Discovery,
// script drafting, and the human script-approval checkpoint are NOT
// exposed here and still require a live Claude Code session; nothing here
// ever calls a posting/publishing API.

import http from "node:http";
import fs from "node:fs/promises";
import { createReadStream } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";
import yaml from "js-yaml";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "..");
const STAGED_DIR = path.join(REPO_ROOT, "staged");
const JOBS_DIR = path.join(REPO_ROOT, "data", "jobs");
const CANDIDATES_DIR = path.join(REPO_ROOT, "data", "candidates");
const PERMISSIONS_PATH = path.join(REPO_ROOT, "data", "permissions", "allowlist.yaml");
const PUBLIC_DIR = path.join(__dirname, "public");
const PORT = process.env.PORT ? Number(process.env.PORT) : 4173;

// The venv python, not a bare "python3" — this repo's dependencies
// (PyYAML, yt-dlp) are installed there, not necessarily on PATH.
const PYTHON_BIN = path.join(REPO_ROOT, ".venv", "bin", "python");
const CONTROL_ACTIONS = new Set([
  "select-candidate",
  "fetch",
  "voiceover",
  "assemble",
  "check-format",
  // Chains fetch -> voiceover -> assemble -> check-format in one call for a
  // job whose script.md already exists (script-writer + content-reviewer
  // have already run) — see pipeline/orchestrate/auto_finish.py. There is
  // deliberately no separate human-approval action gating this: the human
  // checkpoint is skimming the finished result in the staged-jobs list,
  // not reading the script text before these deterministic stages run.
  "auto-finish",
  // The one deliberate, human-triggered exception to "never call a
  // posting/publishing API" — see pipeline/publish/youtube.py. Still
  // requires a human-supplied title and an explicit click; refuses jobs
  // with no recorded permission provenance or a failed/missing
  // format-compliance check.
  "publish-youtube",
]);

const SAFE_PATH_COMPONENT = /^[A-Za-z0-9_.-]+$/;

const STATIC_CONTENT_TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
};

const VIDEO_CONTENT_TYPES = {
  ".mp4": "video/mp4",
  ".mov": "video/quicktime",
  ".webm": "video/webm",
};

function isSafePathComponent(value) {
  // SAFE_PATH_COMPONENT alone would accept "." and ".." (both consist only
  // of allowed characters), which — arriving as a percent-encoded route
  // segment like %2e%2e — would let a job id climb out of staged/<job_id>/
  // before path.join ever runs. Reject them explicitly.
  return (
    typeof value === "string" &&
    value !== "." &&
    value !== ".." &&
    SAFE_PATH_COMPONENT.test(value)
  );
}

function sendJson(res, status, body) {
  const data = JSON.stringify(body);
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(data),
  });
  res.end(data);
}

async function readManifest(jobId) {
  const manifestPath = path.join(STAGED_DIR, jobId, "manifest.json");
  const raw = await fs.readFile(manifestPath, "utf8");
  return { manifestPath, manifest: JSON.parse(raw) };
}

async function listJobs() {
  let entries;
  try {
    entries = await fs.readdir(STAGED_DIR, { withFileTypes: true });
  } catch (err) {
    if (err.code === "ENOENT") return [];
    throw err;
  }

  const jobs = [];
  for (const entry of entries) {
    if (!entry.isDirectory() || !isSafePathComponent(entry.name)) continue;
    try {
      const { manifest } = await readManifest(entry.name);
      jobs.push(manifest);
    } catch (err) {
      if (err.code === "ENOENT") continue;
      // A corrupt/unreadable manifest shouldn't break the whole listing —
      // skip it and keep browsing the rest.
      continue;
    }
  }
  jobs.sort((a, b) => String(a.job_id).localeCompare(String(b.job_id)));
  return jobs;
}

// Resolves manifest.output_file against the job's own staged directory and
// refuses anything that would escape it, mirroring the check in
// pipeline/format/validate.py's _locate_output_file.
function resolveOutputFile(jobId, manifest) {
  const jobDir = path.join(STAGED_DIR, jobId);
  const outputFile = manifest.output_file;
  if (typeof outputFile !== "string" || !outputFile) return null;
  const resolved = path.resolve(jobDir, outputFile);
  const jobDirWithSep = jobDir.endsWith(path.sep) ? jobDir : jobDir + path.sep;
  if (!resolved.startsWith(jobDirWithSep)) return null;
  return resolved;
}

async function readRunState(runId) {
  const statePath = path.join(JOBS_DIR, runId, "state.json");
  const raw = await fs.readFile(statePath, "utf8");
  return JSON.parse(raw);
}

async function listRuns() {
  let entries;
  try {
    entries = await fs.readdir(JOBS_DIR, { withFileTypes: true });
  } catch (err) {
    if (err.code === "ENOENT") return [];
    throw err;
  }

  const runs = [];
  for (const entry of entries) {
    if (!entry.isDirectory() || !isSafePathComponent(entry.name)) continue;
    try {
      runs.push(await readRunState(entry.name));
    } catch (err) {
      if (err.code === "ENOENT") continue;
      // A corrupt/unreadable state.json shouldn't break the whole listing —
      // skip it and keep browsing the rest.
      continue;
    }
  }
  // run_id is a timestamp prefix (see _generate_run_id in run_batch.py), so
  // sorting it descending surfaces the most recently started run first.
  runs.sort((a, b) => String(b.run_id).localeCompare(String(a.run_id)));
  return runs;
}

async function listCreators() {
  let raw;
  try {
    raw = await fs.readFile(PERMISSIONS_PATH, "utf8");
  } catch (err) {
    if (err.code === "ENOENT") return [];
    throw err;
  }
  const data = yaml.load(raw);
  return data && Array.isArray(data.creators) ? data.creators : [];
}

async function listCandidateBatches(creatorId) {
  let creatorIds;
  if (creatorId) {
    if (!isSafePathComponent(creatorId)) return [];
    creatorIds = [creatorId];
  } else {
    let entries;
    try {
      entries = await fs.readdir(CANDIDATES_DIR, { withFileTypes: true });
    } catch (err) {
      if (err.code === "ENOENT") return [];
      throw err;
    }
    creatorIds = entries
      .filter((entry) => entry.isDirectory() && isSafePathComponent(entry.name))
      .map((entry) => entry.name);
  }

  const batches = [];
  for (const cid of creatorIds) {
    let files;
    try {
      files = await fs.readdir(path.join(CANDIDATES_DIR, cid), { withFileTypes: true });
    } catch (err) {
      continue;
    }
    for (const file of files) {
      if (!file.isFile() || !file.name.endsWith(".json")) continue;
      const batchId = file.name.slice(0, -".json".length);
      if (!isSafePathComponent(batchId)) continue;
      try {
        const raw = await fs.readFile(path.join(CANDIDATES_DIR, cid, file.name), "utf8");
        batches.push(JSON.parse(raw));
      } catch (err) {
        // A corrupt/unreadable batch shouldn't break the whole listing.
        continue;
      }
    }
  }
  // batch_id is a timestamp prefix (see _generate_batch_id in
  // find_candidates.py), so sorting it descending surfaces the newest
  // discovery batch first.
  batches.sort((a, b) => String(b.batch_id).localeCompare(String(a.batch_id)));
  return batches;
}

// Shells out to pipeline.control.api — the one place any pipeline stage
// actually runs — via argv (never a shell string), so nothing here can be
// used to inject arbitrary commands regardless of what a caller sends as
// action args; those args are validated by the pipeline functions
// themselves on the Python side, the same as when the CLI calls them.
function runControlAction(action, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(PYTHON_BIN, ["-m", "pipeline.control.api", action, JSON.stringify(args)], {
      cwd: REPO_ROOT,
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("error", reject);
    child.on("close", (code) => {
      try {
        resolve(JSON.parse(stdout.trim()));
      } catch (err) {
        // Unparseable stdout means pipeline.control.api crashed outside its
        // own known-error handling (a real bug, not an expected pipeline
        // error) — the traceback goes to this server's own log, where a
        // developer can act on it, never straight into the browser response.
        if (stderr.trim()) {
          console.error(`pipeline.control.api action '${action}' crashed (exit ${code}):\n${stderr}`);
        }
        reject(new Error(`'${action}' failed unexpectedly (exit ${code}) — see server log for details`));
      }
    });
  });
}

async function serveStaticFile(req, res, pathname) {
  const relative = pathname === "/" ? "index.html" : pathname.slice(1);
  const resolved = path.resolve(PUBLIC_DIR, relative);
  const publicDirWithSep = PUBLIC_DIR.endsWith(path.sep) ? PUBLIC_DIR : PUBLIC_DIR + path.sep;
  if (!resolved.startsWith(publicDirWithSep)) {
    sendJson(res, 403, { error: "forbidden" });
    return;
  }
  try {
    const data = await fs.readFile(resolved);
    const ext = path.extname(resolved);
    res.writeHead(200, {
      "Content-Type": STATIC_CONTENT_TYPES[ext] || "application/octet-stream",
    });
    res.end(data);
  } catch (err) {
    sendJson(res, 404, { error: "not found" });
  }
}

async function handleListJobs(req, res) {
  const jobs = await listJobs();
  sendJson(res, 200, { jobs });
}

async function handleGetJob(req, res, jobId) {
  if (!isSafePathComponent(jobId)) {
    sendJson(res, 400, { error: "invalid job id" });
    return;
  }
  try {
    const { manifest } = await readManifest(jobId);
    sendJson(res, 200, { manifest });
  } catch (err) {
    sendJson(res, 404, { error: "job not found" });
  }
}

async function handleListRuns(req, res) {
  const runs = await listRuns();
  sendJson(res, 200, { runs });
}

async function handleGetRun(req, res, runId) {
  if (!isSafePathComponent(runId)) {
    sendJson(res, 400, { error: "invalid run id" });
    return;
  }
  try {
    const state = await readRunState(runId);
    sendJson(res, 200, { state });
  } catch (err) {
    sendJson(res, 404, { error: "run not found" });
  }
}

async function handleListCreators(req, res) {
  const creators = await listCreators();
  sendJson(res, 200, { creators });
}

async function handleListCandidates(req, res, creatorId) {
  if (creatorId && !isSafePathComponent(creatorId)) {
    sendJson(res, 400, { error: "invalid creator id" });
    return;
  }
  const batches = await listCandidateBatches(creatorId);
  sendJson(res, 200, { batches });
}

async function handleAction(req, res, action) {
  if (!CONTROL_ACTIONS.has(action)) {
    sendJson(res, 404, { error: "unknown action" });
    return;
  }
  let args;
  try {
    args = await readJsonBody(req);
  } catch (err) {
    sendJson(res, 400, { error: "invalid JSON body" });
    return;
  }
  let outcome;
  try {
    outcome = await runControlAction(action, args);
  } catch (err) {
    sendJson(res, 500, { error: String(err && err.message ? err.message : err) });
    return;
  }
  if (outcome.ok) {
    sendJson(res, 200, { result: outcome.result });
  } else {
    // A known pipeline error (bad input, permission check failed, TTS/
    // fetch/ffmpeg failure) — not a server bug, so 422 rather than 500.
    sendJson(res, 422, { error: outcome.error });
  }
}

async function handleGetMedia(req, res, jobId) {
  if (!isSafePathComponent(jobId)) {
    sendJson(res, 400, { error: "invalid job id" });
    return;
  }
  let manifest;
  try {
    ({ manifest } = await readManifest(jobId));
  } catch (err) {
    sendJson(res, 404, { error: "job not found" });
    return;
  }
  const outputPath = resolveOutputFile(jobId, manifest);
  if (!outputPath) {
    sendJson(res, 404, { error: "no output file recorded for this job" });
    return;
  }
  let stat;
  try {
    stat = await fs.stat(outputPath);
  } catch (err) {
    sendJson(res, 404, { error: "staged output not found" });
    return;
  }
  const ext = path.extname(outputPath);
  res.writeHead(200, {
    "Content-Type": VIDEO_CONTENT_TYPES[ext] || "application/octet-stream",
    "Content-Length": stat.size,
  });
  createReadStream(outputPath).pipe(res);
}

async function readJsonBody(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  if (chunks.length === 0) return {};
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

async function handleReview(req, res, jobId) {
  if (!isSafePathComponent(jobId)) {
    sendJson(res, 400, { error: "invalid job id" });
    return;
  }

  let body;
  try {
    body = await readJsonBody(req);
  } catch (err) {
    sendJson(res, 400, { error: "invalid JSON body" });
    return;
  }
  if (typeof body.approved !== "boolean") {
    sendJson(res, 400, { error: "'approved' must be a boolean" });
    return;
  }

  let manifestPath, manifest;
  try {
    ({ manifestPath, manifest } = await readManifest(jobId));
  } catch (err) {
    sendJson(res, 404, { error: "job not found" });
    return;
  }

  // This is a note for the human reviewer only — it never triggers any
  // publish/post action, and this app never writes anywhere but this file.
  manifest.review = {
    reviewed: true,
    approved: body.approved,
    reviewed_at: new Date().toISOString(),
  };
  await fs.writeFile(manifestPath, JSON.stringify(manifest, null, 2) + "\n", "utf8");
  sendJson(res, 200, { manifest });
}

const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, `http://${req.headers.host}`);
    const pathname = url.pathname;

    if (req.method === "GET" && pathname === "/api/jobs") {
      await handleListJobs(req, res);
      return;
    }

    const jobMatch = pathname.match(/^\/api\/jobs\/([^/]+)$/);
    if (req.method === "GET" && jobMatch) {
      await handleGetJob(req, res, decodeURIComponent(jobMatch[1]));
      return;
    }

    const reviewMatch = pathname.match(/^\/api\/jobs\/([^/]+)\/review$/);
    if (req.method === "POST" && reviewMatch) {
      await handleReview(req, res, decodeURIComponent(reviewMatch[1]));
      return;
    }

    if (req.method === "GET" && pathname === "/api/runs") {
      await handleListRuns(req, res);
      return;
    }

    const runMatch = pathname.match(/^\/api\/runs\/([^/]+)$/);
    if (req.method === "GET" && runMatch) {
      await handleGetRun(req, res, decodeURIComponent(runMatch[1]));
      return;
    }

    if (req.method === "GET" && pathname === "/api/creators") {
      await handleListCreators(req, res);
      return;
    }

    if (req.method === "GET" && pathname === "/api/candidates") {
      await handleListCandidates(req, res, null);
      return;
    }

    const candidatesMatch = pathname.match(/^\/api\/candidates\/([^/]+)$/);
    if (req.method === "GET" && candidatesMatch) {
      await handleListCandidates(req, res, decodeURIComponent(candidatesMatch[1]));
      return;
    }

    const actionMatch = pathname.match(/^\/api\/actions\/([^/]+)$/);
    if (req.method === "POST" && actionMatch) {
      await handleAction(req, res, decodeURIComponent(actionMatch[1]));
      return;
    }

    const mediaMatch = pathname.match(/^\/media\/([^/]+)$/);
    if (req.method === "GET" && mediaMatch) {
      await handleGetMedia(req, res, decodeURIComponent(mediaMatch[1]));
      return;
    }

    if (req.method === "GET") {
      await serveStaticFile(req, res, pathname);
      return;
    }

    sendJson(res, 404, { error: "not found" });
  } catch (err) {
    sendJson(res, 500, { error: String(err && err.message ? err.message : err) });
  }
});

server.listen(PORT, () => {
  console.log(`content-pipeline review dashboard listening on http://localhost:${PORT}`);
});
