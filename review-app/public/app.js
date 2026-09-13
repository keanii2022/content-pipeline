// Frontend for the local review dashboard. Talks only to this app's own
// /api/jobs and /media endpoints, which read/annotate staged/ manifests —
// nothing here calls out to any platform or posting API.

const jobListEl = document.getElementById("job-list");
const detailEl = document.getElementById("detail");

let jobs = [];
let activeJobId = null;

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

loadJobs().catch((err) => {
  detailEl.innerHTML = `<p class="empty">Failed to load jobs: ${escapeHtml(err.message)}</p>`;
});
