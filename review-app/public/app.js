// Frontend for the local review dashboard. Talks only to this app's own
// /api/jobs and /media endpoints, which read/annotate staged/ manifests —
// nothing here calls out to any platform or posting API.

const jobListEl = document.getElementById("job-list");
const runListEl = document.getElementById("run-list");
const detailEl = document.getElementById("detail");
const sidebarTitleEl = document.getElementById("sidebar-title");
const tabJobsEl = document.getElementById("tab-jobs");
const tabRunsEl = document.getElementById("tab-runs");

let jobs = [];
let activeJobId = null;
let runs = [];
let activeRunId = null;
let activeTab = "jobs";

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
  jobListEl.hidden = tab !== "jobs";
  runListEl.hidden = tab !== "runs";
  sidebarTitleEl.textContent = tab === "jobs" ? "Staged jobs" : "Batch runs";

  if (tab === "jobs") {
    if (activeJobId) {
      selectJob(activeJobId);
    } else {
      detailEl.innerHTML = `<p class="empty">Select a job from the list to review it.</p>`;
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

function renderDetail(manifest) {
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
  `;

  for (const button of detailEl.querySelectorAll(".actions button")) {
    button.addEventListener("click", () => submitReview(manifest.job_id, button.dataset.approved === "true"));
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

tabJobsEl.addEventListener("click", () => switchTab("jobs"));
tabRunsEl.addEventListener("click", () => switchTab("runs"));

loadJobs().catch((err) => {
  detailEl.innerHTML = `<p class="empty">Failed to load jobs: ${escapeHtml(err.message)}</p>`;
});
