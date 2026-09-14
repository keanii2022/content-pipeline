// Frontend for the local review dashboard / control panel. Talks only to
// this app's own /api/* and /media endpoints — nothing here calls out to
// any platform or posting API. The control panel (Step 13) can trigger
// fetch/voiceover/assemble/check-format and candidate-selection's job
// creation; it never runs discovery or script drafting, which still need
// a live Claude Code session.

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

let jobs = [];
let activeJobId = null;
let runs = [];
let activeRunId = null;
let activeTab = "jobs";
let creators = [];
let batches = [];
let activeBatchKey = null; // `${creator_id}::${batch_id}`
let currentJob = loadCurrentJob(); // { creator_id, batch_id, candidate_index, job_id, clip_id }

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
  jobListEl.hidden = tab !== "jobs";
  runListEl.hidden = tab !== "runs";
  controlListEl.hidden = tab !== "control";
  sidebarTitleEl.textContent =
    tab === "jobs" ? "Staged jobs" : tab === "runs" ? "Batch runs" : "Control panel";

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

// Pulls a default video title out of the script header line
// ("SCRIPT — <title> (<source>, <date>)") so the publish form starts with
// something reasonable instead of blank — always editable before publish.
function guessTitleFromScript(approvedScript) {
  const firstLine = (approvedScript || "").split("\n")[0] || "";
  const match = firstLine.match(/^SCRIPT\s*[—-]\s*(.+?)\s*\(/);
  return match ? match[1].trim() : "";
}

function renderPublishSection(manifest, status) {
  const fc = manifest.format_compliance;
  const eligible = Boolean(manifest.permission_ledger_reference) && fc && fc.passed === true;
  const defaultTitle = guessTitleFromScript(manifest.approved_script);
  const statusHtml = status
    ? `<p class="review-note ${status.error ? "action-error" : "action-ok"}">${escapeHtml(status.text)}</p>`
    : "";
  return `
    <div class="section">
      <h3>Publish to YouTube</h3>
      ${
        eligible
          ? ""
          : `<p class="review-note action-error">Not eligible yet — needs a recorded permission ledger reference and a passed format-compliance check.</p>`
      }
      <input id="yt-title" type="text" placeholder="Title" value="${escapeHtml(defaultTitle)}" ${eligible ? "" : "disabled"} />
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
    </div>

    <div class="actions">
      <button class="approve" data-approved="true">Approve</button>
      <button class="reject" data-approved="false">Reject</button>
    </div>
    ${renderReviewNote(manifest)}

    ${renderPublishSection(manifest, status)}
  `;

  for (const button of detailEl.querySelectorAll(".actions button")) {
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

tabJobsEl.addEventListener("click", () => switchTab("jobs"));
tabRunsEl.addEventListener("click", () => switchTab("runs"));
tabControlEl.addEventListener("click", () => switchTab("control"));

loadJobs().catch((err) => {
  detailEl.innerHTML = `<p class="empty">Failed to load jobs: ${escapeHtml(err.message)}</p>`;
});
