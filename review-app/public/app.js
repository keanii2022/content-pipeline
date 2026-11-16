// Frontend for the local review dashboard / control panel. Talks only to
// this app's own /api/* and /media endpoints — nothing here calls out to
// any platform or posting API. The control panel (Step 13) can trigger
// fetch/voiceover/assemble/check-format and candidate-selection's job
// creation; it never runs discovery or script drafting, which still need
// a live Claude Code session. The Clip mode tab lists long videos taken
// in for clip mode, previews their recorded highlights and can cut them
// into staged jobs; taking a video in, transcribing it and finding the
// highlights still happen from the command line and chat.

const jobListEl = document.getElementById("job-list");
const runListEl = document.getElementById("run-list");
const controlListEl = document.getElementById("control-list");
const creatorsListEl = document.getElementById("creators-list");
const batchesListEl = document.getElementById("batches-list");
const detailEl = document.getElementById("detail");
const sidebarTitleEl = document.getElementById("sidebar-title");
const tabJobsEl = document.getElementById("tab-jobs");
const tabRunsEl = document.getElementById("tab-runs");
const tabControlEl = document.getElementById("tab-control");
const tabClipEl = document.getElementById("tab-clip");
const clipListEl = document.getElementById("clip-list");

let jobs = [];
let activeJobId = null;
let runs = [];
let activeRunId = null;
let activeTab = "jobs";
let creators = [];
let batches = [];
let activeBatchKey = null; // `${creator_id}::${batch_id}`
let currentJob = loadCurrentJob(); // { creator_id, batch_id, candidate_index, job_id, clip_id }
let clipSources = [];
let activeSourceId = null;
let previewEnd = null; // where the highlight being previewed stops, in seconds

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[ch]));
}

function jobStatusLabel(manifest) {
  if (manifest.review?.reviewed) {
    return manifest.review.approved ? "approved" : "rejected";
  }
  if (manifest.format_compliance) {
    return manifest.format_compliance.passed ? "format: pass" : "format: fail";
  }
  return "not reviewed";
}

async function fetchJson(url, options) {
  const res = await fetch(url, options);
  const body = await res.json();
  if (!res.ok) {
    throw new Error(body.error || `request to ${url} failed (${res.status})`);
  }
  return body;
}

async function loadJobs() {
  const data = await fetchJson("/api/jobs");
  jobs = data.jobs;
  renderJobList();
}

function renderJobList() {
  jobListEl.innerHTML = "";
  if (jobs.length === 0) {
    const li = document.createElement("li");
    li.textContent = "No staged jobs found.";
    li.style.cursor = "default";
    jobListEl.appendChild(li);
    return;
  }
  for (const manifest of jobs) {
    const li = document.createElement("li");
    li.className = manifest.job_id === activeJobId ? "active" : "";
    li.innerHTML = `
      <span class="job-id">${escapeHtml(manifest.job_id)}</span>
      <span class="job-status">${escapeHtml(jobStatusLabel(manifest))}</span>
      ${
        isClipJob(manifest)
          ? `<span class="job-status">${escapeHtml(manifest.highlight?.hook_title)}</span>
             <span class="job-status">clip ${rightsBadge(manifest.source?.rights_basis)}</span>`
          : ""
      }
    `;
    li.addEventListener("click", () => selectJob(manifest.job_id));
    jobListEl.appendChild(li);
  }
}

function switchTab(tab) {
  activeTab = tab;
  tabJobsEl.classList.toggle("active", tab === "jobs");
  tabRunsEl.classList.toggle("active", tab === "runs");
  tabControlEl.classList.toggle("active", tab === "control");
  tabClipEl.classList.toggle("active", tab === "clip");
  jobListEl.hidden = tab !== "jobs";
  runListEl.hidden = tab !== "runs";
  controlListEl.hidden = tab !== "control";
  clipListEl.hidden = tab !== "clip";
  sidebarTitleEl.textContent = {
    jobs: "Staged jobs",
    runs: "Batch runs",
    control: "Control panel",
    clip: "Clip mode sources",
  }[tab];

  if (tab === "clip") {
    // Always re-read: sources move on from the command line, not from here.
    loadClipSources()
      .then(() => {
        if (activeTab !== "clip") return;
        if (activeSourceId) return selectSource(activeSourceId);
        detailEl.innerHTML = `<p class="empty">Select a source from the list to see its highlights.</p>`;
      })
      .catch((err) => {
        if (activeTab !== "clip") return;
        detailEl.innerHTML = `<p class="empty">Failed to load clip-mode sources: ${escapeHtml(err.message)}</p>`;
      });
    return;
  }

  if (tab === "jobs") {
    if (activeJobId) {
      selectJob(activeJobId);
    } else {
      detailEl.innerHTML = `<p class="empty">Select a job from the list to review it.</p>`;
    }
    return;
  }

  if (tab === "control") {
    if (creators.length === 0 && batches.length === 0) {
      loadControlPanel().catch((err) => {
        detailEl.innerHTML = `<p class="empty">Failed to load control panel: ${escapeHtml(err.message)}</p>`;
      });
    } else {
      renderCreatorsList();
      renderBatchesList();
      renderControlDetail();
    }
    return;
  }

  const showRunDetail = () => {
    if (activeRunId) {
      selectRun(activeRunId);
    } else {
      detailEl.innerHTML = `<p class="empty">Select a run from the list to see its creators' progress.</p>`;
    }
  };
  if (runs.length === 0) {
    loadRuns().then(showRunDetail).catch((err) => {
      detailEl.innerHTML = `<p class="empty">Failed to load runs: ${escapeHtml(err.message)}</p>`;
    });
  } else {
    renderRunList();
    showRunDetail();
  }
}

// Ports run_batch.py's _describe_entry: derives a human-readable stage +
// next action straight from an entry's recorded fields (never from a
// separately stored status string) so this can't drift from what
// `run_batch.py status <run_id>` reports for the same run.
function describeEntry(entry) {
  const creatorId = entry.creator_id;
  if (entry.format_passed !== null) {
    const result = entry.format_passed ? "PASSED" : "FAILED";
    return `[${creatorId}] format-checked against '${entry.format_profile}': ${result} (done)`;
  }
  if (entry.assembled) {
    return `[${creatorId}] assembled — next: check-format <run_id> ${creatorId} <profile>`;
  }
  if (entry.job_id && entry.clip_id && entry.voiceover_generated) {
    return `[${creatorId}] fetched + voiceover ready — next: assemble <run_id> ${creatorId}`;
  }
  if (entry.job_id) {
    const missing = [];
    if (!entry.clip_id) missing.push(`fetch <run_id> ${creatorId}`);
    if (!entry.voiceover_generated) missing.push(`voiceover <run_id> ${creatorId}`);
    return `[${creatorId}] script job '${entry.job_id}' created — next: ${missing.join(", ")}`;
  }
  if (entry.batch_id !== null) {
    return (
      `[${creatorId}] candidates recorded in batch '${entry.batch_id}' — ` +
      `next: select-candidate <run_id> ${creatorId} <candidate_index>`
    );
  }
  return `[${creatorId}] pending discovery — next: discover <run_id> ${creatorId}`;
}

async function loadRuns() {
  const data = await fetchJson("/api/runs");
  runs = data.runs;
  renderRunList();
}

function renderRunList() {
  runListEl.innerHTML = "";
  if (runs.length === 0) {
    const li = document.createElement("li");
    li.textContent = "No batch runs found.";
    li.style.cursor = "default";
    runListEl.appendChild(li);
    return;
  }
  for (const state of runs) {
    const li = document.createElement("li");
    li.className = state.run_id === activeRunId ? "active" : "";
    li.innerHTML = `
      <span class="job-id">${escapeHtml(state.run_id)}</span>
      <span class="job-status">${state.entries.length} creator(s)</span>
    `;
    li.addEventListener("click", () => selectRun(state.run_id));
    runListEl.appendChild(li);
  }
}

async function selectRun(runId) {
  activeRunId = runId;
  renderRunList();
  const data = await fetchJson(`/api/runs/${encodeURIComponent(runId)}`);
  renderRunDetail(data.state);
}

function renderRunDetail(state) {
  const entries = state.entries
    .map((entry) => `<li>${escapeHtml(describeEntry(entry))}</li>`)
    .join("");

  detailEl.innerHTML = `
    <h2>${escapeHtml(state.run_id)}</h2>
    <p class="review-note">Queue source: ${escapeHtml(state.queue_source)} — started ${escapeHtml(state.created_at)}</p>

    <div class="section">
      <h3>Creators</h3>
      <ul class="run-entries">${entries}</ul>
    </div>
  `;
}

async function selectJob(jobId) {
  activeJobId = jobId;
  renderJobList();
  const data = await fetchJson(`/api/jobs/${encodeURIComponent(jobId)}`);
  renderDetail(data.manifest);
}

function renderFormatCompliance(manifest) {
  const fc = manifest.format_compliance;
  if (!fc) {
    return `<div class="section"><h3>Format compliance</h3><span class="badge unknown">not yet validated</span></div>`;
  }
  const badgeClass = fc.passed ? "pass" : "fail";
  const badgeText = fc.passed ? "pass" : "fail";
  const reasons = (fc.reasons || [])
    .map((r) => `<li>${escapeHtml(r)}</li>`)
    .join("");
  return `
    <div class="section">
      <h3>Format compliance — ${escapeHtml(fc.profile)}</h3>
      <span class="badge ${badgeClass}">${badgeText}</span>
      ${reasons ? `<ul class="reasons">${reasons}</ul>` : ""}
    </div>
  `;
}

function renderReviewNote(manifest) {
  const review = manifest.review;
  if (!review?.reviewed) return "";
  return `<p class="review-note">Marked ${review.approved ? "approved" : "rejected"} at ${escapeHtml(review.reviewed_at)}</p>`;
}

// The title box deliberately starts empty: every upload needs a title a
// person typed for this video (see CLAUDE.md). Pre-filling it from the
// script once let the wrong job go out under its old title untouched.
function renderPublishSection(manifest, status) {
  const fc = manifest.format_compliance;
  const eligible = Boolean(manifest.permission_ledger_reference) && fc && fc.passed === true;
  const statusHtml = status
    ? `<p class="review-note ${status.error ? "action-error" : "action-ok"}">${escapeHtml(status.text)}</p>`
    : "";
  return `
    <div class="section">
      <h3>Publish to YouTube</h3>
      ${
        eligible
          ? ""
          : isClipJob(manifest)
            ? `<p class="review-note action-error">Clip-mode jobs can't be published from here yet.</p>`
            : `<p class="review-note action-error">Not eligible yet — needs a recorded permission ledger reference and a passed format-compliance check.</p>`
      }
      <input id="yt-title" type="text" placeholder="Title" value="" ${eligible ? "" : "disabled"} />
      <textarea id="yt-description" placeholder="Description (optional)" ${eligible ? "" : "disabled"}></textarea>
      <div class="actions">
        <select id="yt-privacy" ${eligible ? "" : "disabled"}>
          <option value="private">private</option>
          <option value="unlisted">unlisted</option>
          <option value="public">public</option>
        </select>
        <button id="yt-publish" ${eligible ? "" : "disabled"}>Publish</button>
      </div>
      ${statusHtml}
    </div>
  `;
}

function renderDetail(manifest, status) {
  const sourceClip = manifest.source_clip || {};
  const ledgerRef = manifest.permission_ledger_reference || {};

  detailEl.innerHTML = `
    <h2>${escapeHtml(manifest.job_id)}</h2>
    <video controls src="/media/${encodeURIComponent(manifest.job_id)}"></video>

    ${renderFormatCompliance(manifest)}

    ${
      isClipJob(manifest)
        ? renderClipJobSections(manifest)
        : `
    <div class="section">
      <h3>Source clip</h3>
      <pre>${escapeHtml(JSON.stringify(sourceClip, null, 2))}</pre>
    </div>

    <div class="section">
      <h3>Permission ledger reference</h3>
      <pre>${escapeHtml(JSON.stringify(ledgerRef, null, 2))}</pre>
    </div>

    <div class="section">
      <h3>Approved script</h3>
      <pre>${escapeHtml(manifest.approved_script)}</pre>
    </div>`
    }

    <div class="actions review-actions">
      <button class="approve" data-approved="true">Approve</button>
      <button class="reject" data-approved="false">Reject</button>
    </div>
    ${renderReviewNote(manifest)}

    ${renderPublishSection(manifest, status)}
  `;

  // Scoped to .review-actions specifically, not the broader .actions class
  // — that class is reused for layout on several other button rows in this
  // panel (e.g. the Publish section below), and matching all of them here
  // previously meant clicking Publish silently also fired a reject.
  for (const button of detailEl.querySelectorAll(".review-actions button")) {
    button.addEventListener("click", () => submitReview(manifest.job_id, button.dataset.approved === "true"));
  }
  const publishBtn = document.getElementById("yt-publish");
  if (publishBtn) publishBtn.addEventListener("click", () => publishToYouTube(manifest.job_id));
}

async function publishToYouTube(jobId) {
  const title = document.getElementById("yt-title").value;
  const description = document.getElementById("yt-description").value;
  const privacy_status = document.getElementById("yt-privacy").value;
  try {
    const result = await runAction("publish-youtube", { job_id: jobId, title, description, privacy_status });
    const data = await fetchJson(`/api/jobs/${encodeURIComponent(jobId)}`);
    renderDetail(data.manifest, { text: `Published: ${result.url}` });
  } catch (err) {
    const data = await fetchJson(`/api/jobs/${encodeURIComponent(jobId)}`);
    renderDetail(data.manifest, { text: err.message, error: true });
  }
}

async function submitReview(jobId, approved) {
  const data = await fetchJson(`/api/jobs/${encodeURIComponent(jobId)}/review`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approved }),
  });
  await loadJobs();
  renderDetail(data.manifest);
}

// --- Control panel (Step 13) ---
// Talks to /api/creators, /api/candidates, and /api/actions/:action.
// The action endpoints wrap fetch/voiceover/assemble/check-format and
// select-candidate's job-creation step — deterministic pipeline code with
// no Claude Code agent involved. Discovery and script drafting are NOT
// here: those still happen in chat, per PLAN.md Step 13's scope.

const CURRENT_JOB_KEY = "content-pipeline-current-job";

function loadCurrentJob() {
  try {
    const raw = localStorage.getItem(CURRENT_JOB_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch (err) {
    return null;
  }
}

function saveCurrentJob(job) {
  currentJob = job;
  try {
    if (job) {
      localStorage.setItem(CURRENT_JOB_KEY, JSON.stringify(job));
    } else {
      localStorage.removeItem(CURRENT_JOB_KEY);
    }
  } catch (err) {
    // Best-effort convenience persistence only — ignore storage failures
    // (private browsing, quota, etc.) and keep going with in-memory state.
  }
}

async function loadControlPanel() {
  const [creatorsData, batchesData] = await Promise.all([
    fetchJson("/api/creators"),
    fetchJson("/api/candidates"),
  ]);
  creators = creatorsData.creators;
  batches = batchesData.batches;
  renderCreatorsList();
  renderBatchesList();
  renderControlDetail();
}

function renderCreatorsList() {
  creatorsListEl.innerHTML = "";
  if (creators.length === 0) {
    const li = document.createElement("li");
    li.textContent = "No permitted creators yet.";
    li.style.cursor = "default";
    creatorsListEl.appendChild(li);
    return;
  }
  for (const creator of creators) {
    const li = document.createElement("li");
    li.style.cursor = "default";
    li.innerHTML = `
      <span class="job-id">${escapeHtml(creator.display_name)}</span>
      <span class="job-status">${escapeHtml(creator.platform)} · ${escapeHtml(creator.creator_id)}</span>
    `;
    creatorsListEl.appendChild(li);
  }
}

function renderBatchesList() {
  batchesListEl.innerHTML = "";
  if (batches.length === 0) {
    const li = document.createElement("li");
    li.textContent = "No candidate batches recorded yet.";
    li.style.cursor = "default";
    batchesListEl.appendChild(li);
    return;
  }
  for (const batch of batches) {
    const key = `${batch.creator_id}::${batch.batch_id}`;
    const li = document.createElement("li");
    li.className = key === activeBatchKey ? "active" : "";
    li.innerHTML = `
      <span class="job-id">${escapeHtml(batch.creator_id)}</span>
      <span class="job-status">${escapeHtml(batch.batch_id)} · ${batch.candidates.length} candidate(s)</span>
    `;
    li.addEventListener("click", () => {
      activeBatchKey = key;
      renderBatchesList();
      renderControlDetail();
    });
    batchesListEl.appendChild(li);
  }
}

async function runAction(action, args) {
  const res = await fetch(`/api/actions/${encodeURIComponent(action)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(args),
  });
  const body = await res.json();
  if (!res.ok) {
    throw new Error(body.error || `action '${action}' failed (${res.status})`);
  }
  return body.result;
}

function renderCurrentJobPanel(status) {
  if (!currentJob) {
    return `<div class="section"><h3>Current job</h3><p class="empty">Pick a candidate below to start one.</p></div>`;
  }
  const statusHtml = status
    ? `<p class="review-note ${status.error ? "action-error" : "action-ok"}">${escapeHtml(status.text)}</p>`
    : "";
  const canFetch = Boolean(currentJob.job_id);
  const canVoiceover = Boolean(currentJob.job_id);
  const canAssemble = Boolean(currentJob.job_id && currentJob.clip_id);
  return `
    <div class="section">
      <h3>Current job</h3>
      <pre>${escapeHtml(JSON.stringify(currentJob, null, 2))}</pre>
      <div class="actions">
        <button id="ca-fetch" ${canFetch ? "" : "disabled"}>Fetch clip</button>
        <button id="ca-voiceover" ${canVoiceover ? "" : "disabled"}>Generate voiceover</button>
        <button id="ca-assemble" ${canAssemble ? "" : "disabled"}>Assemble</button>
      </div>
      <div class="actions">
        <select id="ca-profile">
          <option value="tiktok">tiktok</option>
          <option value="reels">reels</option>
          <option value="shorts">shorts</option>
        </select>
        <button id="ca-checkformat" ${canAssemble ? "" : "disabled"}>Check format</button>
        <button id="ca-clear" class="reject">Clear current job</button>
      </div>
      <div class="actions">
        <button id="ca-autofinish" ${canFetch ? "" : "disabled"}>
          Auto-finish (fetch -&gt; voiceover -&gt; assemble -&gt; check-format)
        </button>
      </div>
      <p class="review-note">
        Auto-finish assumes work/&lt;job_id&gt;/script.md already exists (script-writer
        has run) and has been screened by the content-reviewer agent — it does not
        re-check tone or framing itself. There is no separate approval click after
        that: the result lands in Staged Jobs below for you to skim.
      </p>
      ${statusHtml}
    </div>
  `;
}

function renderBatchBrowser() {
  if (!activeBatchKey) {
    return `<p class="empty">Select a candidate batch from the sidebar to browse and pick a clip.</p>`;
  }
  const batch = batches.find((b) => `${b.creator_id}::${b.batch_id}` === activeBatchKey);
  if (!batch) return `<p class="empty">Batch not found.</p>`;

  const items = batch.candidates
    .map((candidate, index) => {
      const isCurrent =
        currentJob &&
        currentJob.creator_id === batch.creator_id &&
        currentJob.batch_id === batch.batch_id &&
        currentJob.candidate_index === index;
      return `
        <div class="section">
          <h3>[${index}] ${escapeHtml(candidate.start_timestamp)}–${escapeHtml(candidate.end_timestamp)}</h3>
          <p>${escapeHtml(candidate.url)}</p>
          <p class="review-note">${escapeHtml(candidate.rationale)}</p>
          <div class="actions">
            <button class="ca-select" data-index="${index}" ${isCurrent ? "disabled" : ""}>
              ${isCurrent ? "Selected" : "Select this candidate"}
            </button>
          </div>
        </div>
      `;
    })
    .join("");

  return `
    <h2>${escapeHtml(batch.creator_id)} — ${escapeHtml(batch.batch_id)}</h2>
    ${items}
  `;
}

function renderControlDetail(status) {
  detailEl.innerHTML = renderCurrentJobPanel(status) + renderBatchBrowser();
  wireControlActions();
}

function wireControlActions() {
  for (const button of detailEl.querySelectorAll(".ca-select")) {
    button.addEventListener("click", () => selectCandidate(Number(button.dataset.index)));
  }
  const fetchBtn = document.getElementById("ca-fetch");
  if (fetchBtn) fetchBtn.addEventListener("click", doFetch);
  const voiceoverBtn = document.getElementById("ca-voiceover");
  if (voiceoverBtn) voiceoverBtn.addEventListener("click", doVoiceover);
  const assembleBtn = document.getElementById("ca-assemble");
  if (assembleBtn) assembleBtn.addEventListener("click", doAssemble);
  const checkFormatBtn = document.getElementById("ca-checkformat");
  if (checkFormatBtn) checkFormatBtn.addEventListener("click", doCheckFormat);
  const clearBtn = document.getElementById("ca-clear");
  if (clearBtn) clearBtn.addEventListener("click", () => {
    saveCurrentJob(null);
    renderControlDetail();
  });
  const autoFinishBtn = document.getElementById("ca-autofinish");
  if (autoFinishBtn) autoFinishBtn.addEventListener("click", doAutoFinish);
}

async function selectCandidate(candidateIndex) {
  const batch = batches.find((b) => `${b.creator_id}::${b.batch_id}` === activeBatchKey);
  if (!batch) return;
  try {
    const result = await runAction("select-candidate", {
      creator_id: batch.creator_id,
      batch_id: batch.batch_id,
      candidate_index: candidateIndex,
    });
    saveCurrentJob({
      creator_id: batch.creator_id,
      batch_id: batch.batch_id,
      candidate_index: candidateIndex,
      job_id: result.job_id,
      clip_id: null,
    });
    renderControlDetail({
      text: `Job '${result.job_id}' created. Hand this prompt to Claude to run the script-writer agent:\n\n${result.prompt}`,
    });
  } catch (err) {
    renderControlDetail({ text: err.message, error: true });
  }
}

async function doFetch() {
  try {
    const result = await runAction("fetch", {
      creator_id: currentJob.creator_id,
      batch_id: currentJob.batch_id,
      candidate_index: currentJob.candidate_index,
    });
    saveCurrentJob({ ...currentJob, clip_id: result.clip_id });
    renderControlDetail({ text: `Fetched clip to ${result.out_dir}` });
  } catch (err) {
    renderControlDetail({ text: err.message, error: true });
  }
}

async function doVoiceover() {
  try {
    const result = await runAction("voiceover", { job_id: currentJob.job_id });
    renderControlDetail({ text: `Voiceover generated at ${result.voiceover_path}` });
  } catch (err) {
    renderControlDetail({ text: err.message, error: true });
  }
}

async function doAssemble() {
  try {
    const result = await runAction("assemble", {
      job_id: currentJob.job_id,
      creator_id: currentJob.creator_id,
      clip_id: currentJob.clip_id,
    });
    renderControlDetail({ text: `Assembled at ${result.output_path}` });
    await loadJobs(); // the new staged job now shows up in the Staged Jobs tab
  } catch (err) {
    renderControlDetail({ text: err.message, error: true });
  }
}

async function doAutoFinish() {
  const profile = document.getElementById("ca-profile").value;
  try {
    const result = await runAction("auto-finish", {
      job_id: currentJob.job_id,
      creator_id: currentJob.creator_id,
      batch_id: currentJob.batch_id,
      candidate_index: currentJob.candidate_index,
      format_profile: profile,
    });
    saveCurrentJob({ ...currentJob, clip_id: result.clip_id });
    renderControlDetail({
      text: `Auto-finished '${result.job_id}' — format (${profile}): ${
        result.format_passed ? "PASSED" : "FAILED"
      }${result.format_reasons && result.format_reasons.length ? " — " + result.format_reasons.join("; ") : ""}. Output at ${result.output_path}`,
      error: !result.format_passed,
    });
    await loadJobs(); // the new staged job now shows up in the Staged Jobs tab
  } catch (err) {
    renderControlDetail({ text: err.message, error: true });
  }
}

async function doCheckFormat() {
  const profile = document.getElementById("ca-profile").value;
  try {
    const result = await runAction("check-format", { job_id: currentJob.job_id, profile });
    renderControlDetail({
      text: `Format check (${profile}): ${result.passed ? "PASSED" : "FAILED"}${
        result.reasons && result.reasons.length ? " — " + result.reasons.join("; ") : ""
      }`,
      error: !result.passed,
    });
  } catch (err) {
    renderControlDetail({ text: err.message, error: true });
  }
}

// --- Clip mode ---
// Talks to /api/clip/sources, /media/clip-source/:id and the `clip-cut`
// action. This view only reads what the command-line stages recorded and
// cuts recorded highlights into staged jobs. It never takes a video in,
// transcribes, picks highlights or publishes.

const RIGHTS_BASES = ["allowlist", "own", "unverified"];

function isClipJob(manifest) {
  return manifest.mode === "clip";
}

// A basis that is missing or not one of the three known values is shown
// as a warning, never as cleared: the badge must not look better than
// what was actually recorded.
function rightsBadge(rightsBasis) {
  const known = RIGHTS_BASES.includes(rightsBasis);
  const cleared = known && rightsBasis !== "unverified";
  const label = known ? `rights: ${rightsBasis}` : "rights: not recorded";
  return `<span class="badge ${cleared ? "rights-ok" : "rights-warn"}">${escapeHtml(label)}</span>`;
}

function rightsNote(rightsBasis) {
  if (rightsBasis === "unverified") {
    return `<p class="review-note rights-warning">Nobody has checked that this source may be reused. That call is yours before anything is posted.</p>`;
  }
  if (!RIGHTS_BASES.includes(rightsBasis)) {
    return `<p class="review-note rights-warning">No usable rights_basis is recorded for this.</p>`;
  }
  return "";
}

function clock(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds)) return "?";
  const whole = Math.floor(seconds);
  return `${String(Math.floor(whole / 60)).padStart(2, "0")}:${String(whole % 60).padStart(2, "0")}`;
}

function factRows(facts) {
  return facts
    .filter(([, value]) => value !== null && value !== undefined && value !== "")
    .map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd>`)
    .join("");
}

function formatBadge(compliance) {
  if (!compliance) return `<span class="badge unknown">format not checked</span>`;
  return compliance.passed === true
    ? `<span class="badge pass">format pass</span>`
    : `<span class="badge fail">format fail</span>`;
}

// What a staged clip-mode job shows instead of the commentary-mode
// "source clip / ledger reference / approved script" sections.
function renderClipJobSections(manifest) {
  const highlight = manifest.highlight || {};
  const source = manifest.source || {};
  const ledgerRef = source.permission_ledger_reference;
  return `
    <div class="section">
      <h3>Clip</h3>
      <p class="clip-title">${escapeHtml(highlight.hook_title)}</p>
      <p class="review-note">Highlight #${escapeHtml(highlight.rank)} · score ${escapeHtml(highlight.score)} · ${clock(highlight.start)}–${clock(highlight.end)} of the source · original audio</p>
      <p class="review-note">${escapeHtml(highlight.rationale)}</p>
    </div>

    <div class="section">
      <h3>Source</h3>
      <p>${rightsBadge(source.rights_basis)}</p>
      ${rightsNote(source.rights_basis)}
      <dl class="facts">
        ${factRows([
          ["Title", source.title],
          ["From", source.source_url || source.original_filename],
          ["Uploader", source.uploader],
          ["Channel", source.channel_url || source.uploader_url],
          ["License", source.license],
          ["Source id", manifest.source_id],
        ])}
      </dl>
      ${
        ledgerRef
          ? `<h3 class="subhead">Permission ledger reference</h3>
             <pre>${escapeHtml(JSON.stringify(ledgerRef, null, 2))}</pre>`
          : ""
      }
    </div>
  `;
}

async function loadClipSources() {
  const data = await fetchJson("/api/clip/sources");
  clipSources = data.sources;
  renderClipList();
}

function sourceStageLabel(source) {
  if (source.error) return "can't be read";
  if (!source.transcribed) return "needs transcribing";
  if (!source.highlights_recorded) return "needs highlights";
  return `${source.clips_cut} of ${source.highlights.length} highlights cut`;
}

function renderClipList() {
  clipListEl.innerHTML = "";
  if (clipSources.length === 0) {
    const li = document.createElement("li");
    li.textContent = "No clip-mode sources yet.";
    li.style.cursor = "default";
    clipListEl.appendChild(li);
    return;
  }
  for (const source of clipSources) {
    const li = document.createElement("li");
    li.className = source.source_id === activeSourceId ? "active" : "";
    li.innerHTML = `
      <span class="job-id">${escapeHtml(source.title || source.source_id)}</span>
      <span class="job-status">${escapeHtml(sourceStageLabel(source))}</span>
      ${source.error ? "" : `<span class="job-status">${rightsBadge(source.rights_basis)}</span>`}
    `;
    li.addEventListener("click", () => selectSource(source.source_id));
    clipListEl.appendChild(li);
  }
}

async function selectSource(sourceId, status) {
  activeSourceId = sourceId;
  renderClipList();
  try {
    const data = await fetchJson(`/api/clip/sources/${encodeURIComponent(sourceId)}`);
    if (activeTab !== "clip" || activeSourceId !== sourceId) return;
    renderSourceDetail(data, status);
  } catch (err) {
    if (activeTab !== "clip" || activeSourceId !== sourceId) return;
    detailEl.innerHTML = `
      <h2>${escapeHtml(sourceId)}</h2>
      <p class="review-note action-error">This source can't be used as it is: ${escapeHtml(err.message)}</p>
    `;
  }
}

function renderNextStep(source) {
  if (source.next === "transcribe" || source.next === "find-highlights") {
    return `
      <p class="review-note">The next step runs from a terminal, not from here:</p>
      <pre class="command">.venv/bin/python -m pipeline.orchestrate.run_clip ${escapeHtml(source.next)} ${escapeHtml(source.source_id)}</pre>
    `;
  }
  if (source.next === "cut") {
    return `<p class="review-note">Next: cut the highlights below.</p>`;
  }
  return `<p class="review-note">Every highlight is cut. The clips are under Staged Jobs.</p>`;
}

function renderHighlightRow(highlight) {
  const length = Math.round(highlight.duration_seconds);
  return `
    <tr>
      <td><input type="checkbox" class="clip-pick" data-rank="${escapeHtml(highlight.rank)}" aria-label="Select highlight #${escapeHtml(highlight.rank)} to cut" ${highlight.job_id ? "disabled" : ""} /></td>
      <td>#${escapeHtml(highlight.rank)}</td>
      <td>${escapeHtml(highlight.score)}</td>
      <td class="nowrap">${clock(highlight.start)}–${clock(highlight.end)}<br /><span class="muted">${escapeHtml(length)}s</span></td>
      <td>
        <strong>${escapeHtml(highlight.hook_title)}</strong>
        <div class="muted">${escapeHtml(highlight.rationale)}</div>
      </td>
      <td class="nowrap">
        ${
          highlight.job_id
            ? `<button type="button" class="link clip-open-job" data-job-id="${escapeHtml(highlight.job_id)}">open clip</button><br />${formatBadge(highlight.format_compliance)}`
            : `<span class="badge unknown">not cut</span>`
        }
      </td>
      <td><button type="button" class="clip-preview-btn" data-start="${escapeHtml(highlight.start)}" data-end="${escapeHtml(highlight.end)}">Preview</button></td>
    </tr>
  `;
}

function renderSourceDetail(data, status) {
  const source = data.source;
  const highlights = source.highlights || [];
  const remaining = highlights.filter((highlight) => !highlight.job_id).length;
  const minutes =
    typeof source.duration_seconds === "number" ? `${(source.duration_seconds / 60).toFixed(1)} min` : null;
  const stage = (done, label) => `<li class="${done ? "done" : ""}">${escapeHtml(label)}</li>`;
  const statusHtml = status
    ? `<p id="clip-status" class="review-note ${status.error ? "action-error" : "action-ok"}">${escapeHtml(status.text)}</p>`
    : `<p id="clip-status" class="review-note"></p>`;
  const profileOptions = (data.profiles || [])
    .map(
      (profile) =>
        `<option value="${escapeHtml(profile)}" ${profile === data.default_profile ? "selected" : ""}>${escapeHtml(profile)}</option>`
    )
    .join("");

  previewEnd = null;
  detailEl.innerHTML = `
    <h2>${escapeHtml(source.title || source.source_id)}</h2>
    <p class="review-note">${escapeHtml(source.source_id)}</p>

    <div class="section">
      <h3>Source</h3>
      <p>${rightsBadge(source.rights_basis)}</p>
      ${rightsNote(source.rights_basis)}
      <dl class="facts">
        ${factRows([
          ["From", source.origin],
          ["Uploader", source.source.uploader],
          ["Channel", source.source.channel_url || source.source.uploader_url],
          ["License", source.source.license],
          ["Length", minutes],
          ["Taken in", source.fetched_at],
        ])}
      </dl>
    </div>

    <div class="section">
      <h3>Progress</h3>
      <ul class="stages">
        ${stage(true, "Taken in")}
        ${stage(source.transcribed, source.transcribed ? `Transcribed (${source.language}, ${source.segments} segments)` : "Transcribed")}
        ${stage(source.highlights_recorded, source.highlights_recorded ? `Highlights recorded (${highlights.length})` : "Highlights recorded")}
        ${stage(highlights.length > 0 && remaining === 0, source.highlights_recorded ? `Clips cut (${source.clips_cut} of ${highlights.length})` : "Clips cut")}
      </ul>
      ${renderNextStep(source)}
      ${
        source.other_staged_clips
          ? `<p class="review-note">${escapeHtml(source.other_staged_clips)} more staged clip(s) from this source came from earlier highlight picks.</p>`
          : ""
      }
    </div>

    ${
      highlights.length === 0
        ? ""
        : `
    <div class="section">
      <h3>Highlights</h3>
      <video id="clip-preview" controls preload="metadata" src="/media/clip-source/${encodeURIComponent(source.source_id)}"></video>
      <table class="highlights">
        <thead>
          <tr><th></th><th>#</th><th>Score</th><th>When</th><th>Hook</th><th>Clip</th><th></th></tr>
        </thead>
        <tbody>${highlights.map(renderHighlightRow).join("")}</tbody>
      </table>
      <div class="actions">
        <select id="clip-profile" title="Format check to run on each clip">${profileOptions}</select>
        <button type="button" id="clip-cut-selected" disabled>Cut selected</button>
        <button type="button" id="clip-cut-all" ${remaining ? "" : "disabled"}>Cut all not cut yet (${remaining})</button>
      </div>
      <p class="review-note">Cutting makes a 9:16 clip with the original audio and puts it under Staged Jobs. Nothing is published.</p>
      ${statusHtml}
    </div>`
    }
  `;
  wireSourceDetail(source.source_id);
}

function wireSourceDetail(sourceId) {
  const video = document.getElementById("clip-preview");
  if (!video) return;
  const cutSelectedBtn = document.getElementById("clip-cut-selected");
  const picks = [...detailEl.querySelectorAll(".clip-pick")];
  const pickedRanks = () => picks.filter((box) => box.checked).map((box) => Number(box.dataset.rank));

  // Stop at the end of the highlight being previewed, so a preview shows
  // exactly what the cut would contain.
  video.addEventListener("timeupdate", () => {
    if (previewEnd !== null && video.currentTime >= previewEnd) {
      video.pause();
      previewEnd = null;
    }
  });
  for (const button of detailEl.querySelectorAll(".clip-preview-btn")) {
    button.addEventListener("click", () => {
      previewEnd = Number(button.dataset.end);
      video.currentTime = Number(button.dataset.start);
      video.play().catch(() => {});
    });
  }
  for (const box of picks) {
    box.addEventListener("change", () => {
      cutSelectedBtn.disabled = pickedRanks().length === 0;
    });
  }
  for (const button of detailEl.querySelectorAll(".clip-open-job")) {
    button.addEventListener("click", async () => {
      activeJobId = button.dataset.jobId;
      await loadJobs().catch(() => {});
      switchTab("jobs");
    });
  }
  cutSelectedBtn.addEventListener("click", () => cutHighlights(sourceId, pickedRanks()));
  document.getElementById("clip-cut-all").addEventListener("click", () => cutHighlights(sourceId, null));
}

async function cutHighlights(sourceId, indices) {
  const args = { source_id: sourceId, profile: document.getElementById("clip-profile").value };
  if (indices) args.indices = indices;
  for (const control of detailEl.querySelectorAll("#clip-cut-selected, #clip-cut-all, .clip-pick")) {
    control.disabled = true;
  }
  const statusEl = document.getElementById("clip-status");
  statusEl.className = "review-note";
  statusEl.textContent = "Cutting… this takes a little while for several clips.";

  let status;
  try {
    const result = await runAction("clip-cut", args);
    const failed = result.staged.filter((clip) => clip.format_compliance?.passed !== true).length;
    status = {
      text:
        `Cut ${result.staged.length} clip(s) — they're under Staged Jobs. Nothing is published.` +
        (failed ? ` ${failed} did not pass the ${result.profile} format check.` : ""),
      error: failed > 0,
    };
  } catch (err) {
    status = { text: err.message, error: true };
  }
  await Promise.all([loadJobs(), loadClipSources()]).catch(() => {});
  if (activeTab === "clip" && activeSourceId === sourceId) {
    await selectSource(sourceId, status);
  }
}

tabJobsEl.addEventListener("click", () => switchTab("jobs"));
tabRunsEl.addEventListener("click", () => switchTab("runs"));
tabControlEl.addEventListener("click", () => switchTab("control"));
tabClipEl.addEventListener("click", () => switchTab("clip"));

loadJobs().catch((err) => {
  detailEl.innerHTML = `<p class="empty">Failed to load jobs: ${escapeHtml(err.message)}</p>`;
});
