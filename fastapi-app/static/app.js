/* ═══════════════════════════════════════════════════════════════════════════
   VAT Validation Agent — app.js
   Single-file, vanilla JS, no dependencies.
   ═══════════════════════════════════════════════════════════════════════════ */

'use strict';

/* ── Global State ─────────────────────────────────────────────────────────── */
window.vatEntries       = [];
window.selectedIsoCodes = new Set();
window.scanResults      = null;
window.scanWasTestMode  = false;
window.currentView      = 'view-home';

/* ── Theme ────────────────────────────────────────────────────────────────── */
(function initTheme() {
  const THEMES   = ['system', 'light', 'dark'];
  const LABELS   = { system: '◑ SYSTEM', light: '☀ LIGHT', dark: '☾ DARK' };
  const html     = document.documentElement;
  const btn      = document.getElementById('theme-toggle');
  let   mqDark   = window.matchMedia('(prefers-color-scheme: dark)');
  let   current  = localStorage.getItem('vat-theme') || 'system';

  function applyTheme(t) {
    current = t;
    localStorage.setItem('vat-theme', t);
    if (t === 'system') {
      html.setAttribute('data-theme', mqDark.matches ? 'dark' : 'light');
    } else {
      html.setAttribute('data-theme', t);
    }
    if (btn) btn.textContent = LABELS[t];
  }

  // Listen for OS-level changes when in system mode
  mqDark.addEventListener('change', () => {
    if (current === 'system') applyTheme('system');
  });

  if (btn) {
    btn.addEventListener('click', () => {
      const idx = THEMES.indexOf(current);
      applyTheme(THEMES[(idx + 1) % THEMES.length]);
    });
  }

  applyTheme(current);
})();

/* ── Navigation ───────────────────────────────────────────────────────────── */
const VIEWS = ['view-home', 'view-status', 'view-results', 'view-export', 'view-logs'];

function showView(id) {
  VIEWS.forEach(v => {
    const el = document.getElementById(v);
    if (el) el.style.display = (v === id) ? 'block' : 'none';
  });
  // Update nav button states
  document.querySelectorAll('#nav button[data-nav]').forEach(btn => {
    const active = btn.dataset.nav === id;
    btn.classList.toggle('btn-inverted', active);
  });
  window.currentView = id;

  // Side-effects on view entry
  if (id === 'view-results') loadResults();
  if (id === 'view-export')  buildDiary();
  if (id === 'view-logs')    loadLogList();
}

document.querySelectorAll('#nav button[data-nav]').forEach(btn => {
  btn.addEventListener('click', () => showView(btn.dataset.nav));
});

/* ── Utilities ────────────────────────────────────────────────────────────── */
function makeError(msg) {
  const d = document.createElement('div');
  d.className = 'error-box';
  d.textContent = msg;
  return d;
}

function clearEl(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
}

function asciiBar(ratio, width = 20) {
  const filled = Math.round(Math.min(1, Math.max(0, ratio)) * width);
  const empty  = width - filled;
  const pct    = Math.round(ratio * 100);
  return '[' + '█'.repeat(filled) + '░'.repeat(empty) + '] ' + pct + '%';
}

function confBar(score, width = 10) {
  const filled = Math.round(Math.min(1, Math.max(0, score)) * width);
  const empty  = width - filled;
  return '[' + '█'.repeat(filled) + '░'.repeat(empty) + '] ' + score.toFixed(2);
}

function scoreEmoji(score) {
  if (score >= 0.9) return '🟢';
  if (score >= 0.7) return '🟡';
  return '🔴';
}

/* ── VBA Parsing & Home View ──────────────────────────────────────────────── */
const TEST_MODE_ISOS = new Set(['DE','FR','EE','ID','MW','MQ','GP','RU','BR','HK','US','AI']);

document.getElementById('btn-fetch-vba').addEventListener('click', fetchVBA);

async function fetchVBA() {
  const btn       = document.getElementById('btn-fetch-vba');
  const statusEl  = document.getElementById('vba-status');
  const errorEl   = document.getElementById('vba-error');
  clearEl(errorEl);
  statusEl.textContent = 'Loading...';
  btn.disabled = true;

  try {
    const res  = await fetch('/api/vba');
    if (!res.ok) throw new Error('HTTP ' + res.status + ': ' + res.statusText);
    const text = await res.text();

    const regex   = /countryData\((\d+),\s*1\)\s*=\s*"([^"]*)"\s*:\s*countryData\(\d+,\s*2\)\s*=\s*"([^"]*)"\s*:\s*countryData\(\d+,\s*3\)\s*=\s*([0-9.]+)/g;
    const entries = [];
    let   m;
    while ((m = regex.exec(text)) !== null) {
      entries.push({
        index:    parseInt(m[1], 10),
        iso_code: m[2],
        name:     m[3],
        vat_rate: parseFloat(m[4])
      });
    }

    if (entries.length === 0) throw new Error('No country data found in VBA file.');

    window.vatEntries = entries;
    statusEl.textContent = '[ LOADED ] ' + entries.length + ' countries';

    buildCountryList();
    document.getElementById('country-section').style.display = 'block';
  } catch (err) {
    statusEl.textContent = '';
    errorEl.appendChild(makeError('Failed to fetch VBA: ' + err.message));
  } finally {
    btn.disabled = false;
  }
}

function buildCountryList() {
  const listEl    = document.getElementById('country-list');
  const filterEl  = document.getElementById('country-filter');
  clearEl(listEl);

  const filterVal = filterEl ? filterEl.value.toLowerCase() : '';

  window.vatEntries.forEach(entry => {
    if (filterVal) {
      const hay = (entry.iso_code + ' ' + entry.name).toLowerCase();
      if (!hay.includes(filterVal)) return;
    }

    const row = document.createElement('label');
    row.className = 'country-item';

    const cb = document.createElement('input');
    cb.type    = 'checkbox';
    cb.value   = entry.iso_code;
    cb.checked = window.selectedIsoCodes.has(entry.iso_code);
    cb.addEventListener('change', () => {
      if (cb.checked) window.selectedIsoCodes.add(entry.iso_code);
      else            window.selectedIsoCodes.delete(entry.iso_code);
      updateSelectionCaption();
    });

    const txt = document.createTextNode(entry.iso_code + ' — ' + entry.name);
    row.appendChild(cb);
    row.appendChild(txt);
    listEl.appendChild(row);
  });

  updateSelectionCaption();
}

function updateSelectionCaption() {
  const cap = document.getElementById('selection-caption');
  const btn = document.getElementById('btn-start-scan');
  const n   = window.selectedIsoCodes.size;
  if (n > 0) {
    const mins = Math.round(n * 1.5);
    cap.textContent = '[ ' + n + ' SELECTED ] — estimated scan time: ~' + mins + ' minutes';
    cap.style.display = 'block';
    btn.disabled = false;
  } else {
    cap.textContent = '';
    cap.style.display = 'none';
    btn.disabled = true;
  }
}

// Filter input
document.getElementById('country-filter').addEventListener('input', buildCountryList);

// Select All
document.getElementById('btn-select-all').addEventListener('click', () => {
  window.vatEntries.forEach(e => window.selectedIsoCodes.add(e.iso_code));
  buildCountryList();
});

// Test Mode
document.getElementById('btn-test-mode').addEventListener('click', () => {
  window.selectedIsoCodes.clear();
  TEST_MODE_ISOS.forEach(iso => window.selectedIsoCodes.add(iso));
  buildCountryList();
});

// Clear
document.getElementById('btn-clear-all').addEventListener('click', () => {
  window.selectedIsoCodes.clear();
  buildCountryList();
});

// Start Scan
document.getElementById('btn-start-scan').addEventListener('click', startScan);

async function startScan() {
  const btn      = document.getElementById('btn-start-scan');
  const errorEl  = document.getElementById('start-error');
  clearEl(errorEl);

  const countries  = Array.from(window.selectedIsoCodes);
  const isTestMode = countries.length <= TEST_MODE_ISOS.size &&
                     countries.every(c => TEST_MODE_ISOS.has(c));

  btn.disabled = true;

  try {
    const res = await fetch('/api/scan/start', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ countries, test_mode: isTestMode })
    });
    if (!res.ok) {
      const body = await res.text();
      throw new Error('HTTP ' + res.status + ': ' + body);
    }

    window.scanWasTestMode = isTestMode;
    showView('view-status');
    startStatusPolling();
  } catch (err) {
    errorEl.appendChild(makeError('Failed to start scan: ' + err.message));
    btn.disabled = false;
  }
}

/* ── Status View & Polling ────────────────────────────────────────────────── */
let statusInterval   = null;
let logListCache     = null;
let logListFetchedAt = 0;
let logFetchInFlight = false;

function startStatusPolling() {
  if (statusInterval) clearInterval(statusInterval);
  pollStatus();
  statusInterval = setInterval(pollStatus, 3000);
}

async function pollStatus() {
  try {
    const res  = await fetch('/api/scan/status');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    renderStatus(data);

    if (!logFetchInFlight) {
      logFetchInFlight = true;
      pollLogTail().finally(() => { logFetchInFlight = false; });
    }

    if (data.status === 'done') {
      clearInterval(statusInterval);
      statusInterval = null;
      setTimeout(() => showView('view-results'), 1000);
    }
  } catch (err) {
    // Silently ignore transient failures; render will show stale data
    console.warn('Status poll error:', err);
  }
}

function renderStatus(data) {
  const badge = document.getElementById('status-badge');
  badge.textContent = (data.status || 'idle').toUpperCase().replace('_', ' ');
  badge.className   = 'badge badge-' + (data.status || 'idle');

  const countryEl = document.getElementById('status-country');
  if (data.current_country) {
    // Try to find name from vatEntries
    const entry = (window.vatEntries || []).find(e => e.iso_code === data.current_country);
    const label = entry ? data.current_country + ' — ' + entry.name : data.current_country;
    countryEl.textContent = 'Processing: ' + label;
  } else {
    countryEl.textContent = '';
  }

  const progText = document.getElementById('status-progress-text');
  const progBar  = document.getElementById('status-progress-bar');
  const completed = data.last_completed_index != null ? data.last_completed_index : 0;
  const total     = data.total || 0;

  if (total > 0) {
    progText.textContent = completed + ' / ' + total + ' countries validated';
    progBar.textContent  = asciiBar(completed / total);
  } else {
    progText.textContent = '';
    progBar.textContent  = '';
  }
}

async function pollLogTail() {
  const now = Date.now();
  // Refresh log list at most every 30 seconds
  if (!logListCache || (now - logListFetchedAt) > 30000) {
    try {
      const res = await fetch('/api/logs');
      if (res.ok) {
        logListCache     = await res.json();
        logListFetchedAt = Date.now();
      }
    } catch (_) { /* ignore */ }
  }

  if (!logListCache || logListCache.length === 0) return;

  // Most recent = last entry (assume sorted ascending by name/timestamp)
  const filename = logListCache[logListCache.length - 1];

  const filenameEl = document.getElementById('log-tail-filename');
  const preEl      = document.getElementById('log-tail-pre');

  if (filenameEl) filenameEl.textContent = '[ ' + filename + ' ]';

  try {
    const res = await fetch('/api/logs/' + encodeURIComponent(filename));
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const text  = await res.text();
    const lines = text.split('\n');
    const tail  = lines.slice(Math.max(0, lines.length - 20)).join('\n');
    if (preEl) preEl.textContent = tail || '(empty log)';
  } catch (err) {
    if (preEl) preEl.textContent = 'Error fetching log: ' + err.message;
  }
}

/* ── Results View ─────────────────────────────────────────────────────────── */
document.getElementById('btn-refresh-results').addEventListener('click', loadResults);

async function loadResults() {
  const errorEl = document.getElementById('results-error');
  clearEl(errorEl);

  try {
    const res = await fetch('/api/scan/results');
    if (!res.ok) throw new Error('HTTP ' + res.status + ': ' + res.statusText);
    const data = await res.json();
    window.scanResults = data;
    renderResults(data);
  } catch (err) {
    errorEl.appendChild(makeError('Failed to load results: ' + err.message));
  }
}

function renderResults(data) {
  const results = data.results || {};
  const entries = Object.entries(results);

  if (entries.length === 0) {
    document.getElementById('results-metrics').style.display = 'none';
    document.getElementById('mismatches-section').style.display = 'none';
    return;
  }

  // Categorise
  const mismatches    = [];
  const confirmedHigh = [];
  const lowConf       = [];

  entries.forEach(([iso, r]) => {
    if (!r.is_match) {
      mismatches.push({ iso, ...r });
    } else if (r.confidence_score >= 0.7) {
      confirmedHigh.push({ iso, ...r });
    } else {
      lowConf.push({ iso, ...r });
    }
  });

  const needsReview = entries.filter(([, r]) =>
    r.needs_human_review && (!r.is_match || r.confidence_score < 0.7)
  ).length;

  // Metrics
  document.getElementById('results-metrics').style.display = '';
  document.getElementById('metric-total').textContent      = entries.length;
  document.getElementById('metric-matches').textContent    = entries.filter(([, r]) => r.is_match).length;
  document.getElementById('metric-mismatches').textContent = mismatches.length;
  document.getElementById('metric-review').textContent     = needsReview;

  // ── Mismatches
  const mmSection = document.getElementById('mismatches-section');
  mmSection.style.display = mismatches.length > 0 ? '' : 'none';

  // Sort by rate_diff descending
  mismatches.sort((a, b) => Math.abs(b.rate_diff || 0) - Math.abs(a.rate_diff || 0));

  const tbody = document.getElementById('mismatches-tbody');
  clearEl(tbody);
  mismatches.forEach(r => {
    const tr = document.createElement('tr');
    tr.dataset.iso = r.iso;
    tr.innerHTML = `
      <td>${r.iso}</td>
      <td>${reVerifiedPrefix(r)}${escHtml(r.country_name || r.country || '—')}</td>
      <td>${r.stored_rate != null ? r.stored_rate : '—'}</td>
      <td>${r.found_rate  != null ? r.found_rate  : '—'}</td>
      <td>${r.rate_diff   != null ? (r.rate_diff > 0 ? '+' : '') + r.rate_diff : '—'}</td>
      <td>${r.confidence_score != null ? r.confidence_score.toFixed(2) : '—'}</td>
      <td class="review-actions"></td>
    `;
    const actionsEl = tr.querySelector('.review-actions');
    renderReviewActions(actionsEl, r.iso, r.review_status);
    tbody.appendChild(tr);
  });

  // ── Approve / Reject All
  document.getElementById('btn-approve-all').onclick = () => bulkReview('approve', mismatches.map(r => r.iso));
  document.getElementById('btn-reject-all').onclick  = () => bulkReview('reject',  mismatches.map(r => r.iso));

  // ── Confirmed High Confidence
  // A matching rate and decent confidence does not mean "nothing to see":
  // a structural tax reform can leave the rate unchanged while still setting
  // needs_human_review. Surface those rather than burying them.
  const flaggedConfirmed = confirmedHigh.filter(r => r.needs_human_review);

  confirmedHigh.sort((a, b) => {
    const aFlag = a.needs_human_review ? 0 : 1;
    const bFlag = b.needs_human_review ? 0 : 1;
    if (aFlag !== bFlag) return aFlag - bFlag;
    return (a.country_name || a.country || '').localeCompare(b.country_name || b.country || '');
  });

  document.getElementById('details-confirmed-summary').textContent =
    '═══ CONFIRMED MATCHES (' + confirmedHigh.length + ') ═══' +
    (flaggedConfirmed.length ? '  ⚠ ' + flaggedConfirmed.length + ' FLAGGED' : '');

  // Expand automatically when something in here needs attention, so the user
  // is not required to open it to find out that they should have.
  const cDetails = document.getElementById('details-confirmed');
  if (flaggedConfirmed.length > 0) {
    cDetails.setAttribute('open', '');
  } else {
    cDetails.removeAttribute('open');
  }

  const cTbody = document.getElementById('confirmed-tbody');
  clearEl(cTbody);
  confirmedHigh.forEach(r => {
    const tr = document.createElement('tr');
    const flagged = !!r.needs_human_review;
    const reason = r.review_reason ? escHtml(r.review_reason) : '';
    const name = escHtml(r.country_name || r.country || '—');

    if (flagged) tr.className = 'flagged-row';
    if (flagged && reason) tr.setAttribute('title', reason);

    tr.innerHTML = `
      <td>${r.iso}</td>
      <td>${flagged ? '⚠ ' : ''}${reVerifiedPrefix(r)}${name}</td>
      <td>${r.stored_rate != null ? r.stored_rate : '—'}</td>
      <td>${escHtml(r.confidence || '—')}</td>
      <td>${r.confidence_score != null ? confBar(r.confidence_score) : '—'}</td>
      <td>${flagged ? (reason || 'Flagged for human review') : ''}</td>
    `;
    cTbody.appendChild(tr);
  });

  // ── Low Confidence
  document.getElementById('details-lowconf-summary').textContent =
    '⚠ LOW CONFIDENCE MATCHES (' + lowConf.length + ') ═══';
  const lTbody = document.getElementById('lowconf-tbody');
  clearEl(lTbody);
  window.lowConfIsoCodes = lowConf.map(r => r.iso);

  lowConf.forEach(r => {
    const tr = document.createElement('tr');
    // A country that has already been re-verified and is STILL low confidence
    // stays here, badged — that is the honest outcome, not a failure to move.
    if (r.re_verified) tr.className = 'flagged-row';
    tr.innerHTML = `
      <td>${r.iso}</td>
      <td>${reVerifiedPrefix(r)}${escHtml(r.country_name || r.country || '—')}</td>
      <td>${r.stored_rate != null ? r.stored_rate : '—'}</td>
      <td>${escHtml(r.confidence || '—')}</td>
      <td>${r.confidence_score != null ? confBar(r.confidence_score) : '—'}</td>
      <td>${escHtml(r.review_reason || '—')}</td>
      <td class="retry-action"></td>
    `;
    renderRetryAction(tr.querySelector('.retry-action'), r.iso);
    lTbody.appendChild(tr);
  });

  const bulkWrap = document.getElementById('lowconf-bulk');
  if (bulkWrap) bulkWrap.style.display = lowConf.length > 0 ? '' : 'none';
}

/* ── Targeted retry ───────────────────────────────────────────────────────── */

// Countries currently listed as low confidence, kept so the bulk button knows
// what to submit without re-deriving it from the DOM.
window.lowConfIsoCodes = window.lowConfIsoCodes || [];

function reVerifiedPrefix(r) {
  return r && r.re_verified ? '↻ RE-VERIFIED  ' : '';
}

async function submitRetry(isoCodes, statusEl) {
  if (!isoCodes || isoCodes.length === 0) return;
  const label = statusEl || document.getElementById('retry-status');
  if (label) label.textContent = 'Queueing…';

  try {
    const resp = await fetch('/api/scan/retry', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ countries: isoCodes }),
    });

    if (resp.status === 409) {
      if (label) label.textContent = 'A scan or retry is already running.';
      return;
    }
    if (!resp.ok) {
      const detail = await resp.text();
      if (label) label.textContent = 'Failed: ' + detail.slice(0, 120);
      return;
    }

    if (label) label.textContent = 'Queued — switching to status…';
    // The Status view already polls validation_progress.json every 3s and
    // navigates back to Results when it reports done, so nothing further is
    // needed here.
    showView('view-status');
    if (typeof startStatusPolling === 'function') startStatusPolling();
  } catch (err) {
    if (label) label.textContent = 'Failed: ' + err.message;
  }
}

// Bulk retry — submits every country currently listed as low confidence.
// Attached at top level like the other listeners in this file; the script is
// loaded with defer, so the DOM is already parsed.
(function wireBulkRetry() {
  const bulkBtn = document.getElementById('btn-retry-all');
  if (!bulkBtn) return;
  bulkBtn.addEventListener('click', () => {
    const isos = window.lowConfIsoCodes || [];
    const label = document.getElementById('retry-status');
    if (isos.length === 0) {
      if (label) label.textContent = 'Nothing to re-run.';
      return;
    }
    bulkBtn.disabled = true;
    submitRetry(isos, label);
  });
})();

function renderRetryAction(container, iso) {
  clearEl(container);
  const btn = document.createElement('button');
  btn.textContent = '[ ↻ RE-RUN TARGETED ]';
  btn.style.fontSize = '0.75rem';
  btn.style.padding = '0.25rem 0.6rem';
  btn.addEventListener('click', () => {
    btn.disabled = true;
    btn.textContent = '[ QUEUEING… ]';
    submitRetry([iso]);
  });
  container.appendChild(btn);
}

function renderReviewActions(container, iso, currentStatus) {
  clearEl(container);
  if (currentStatus === 'approved') {
    const span = document.createElement('span');
    span.className   = 'label';
    span.textContent = 'APPROVED';
    span.style.color = '#2e7d32';
    container.appendChild(span);
    return;
  }
  if (currentStatus === 'rejected') {
    const span = document.createElement('span');
    span.className   = 'label';
    span.textContent = 'REJECTED';
    span.style.color = '#c62828';
    container.appendChild(span);
    return;
  }

  const approveBtn = document.createElement('button');
  approveBtn.textContent = '[ ✓ APPROVE ]';
  approveBtn.style.marginRight = '0.4rem';
  approveBtn.addEventListener('click', () => submitReview(iso, 'approve', container));

  const rejectBtn = document.createElement('button');
  rejectBtn.textContent = '[ ✗ REJECT ]';
  rejectBtn.addEventListener('click', () => submitReview(iso, 'reject', container));

  container.appendChild(approveBtn);
  container.appendChild(rejectBtn);
}

async function submitReview(iso, action, actionsContainer) {
  const errorEl = document.getElementById('mismatches-error');
  try {
    const res = await fetch('/api/scan/review', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ country: iso, action })
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    renderReviewActions(actionsContainer, iso, action === 'approve' ? 'approved' : 'rejected');

    // Update scanResults cache
    if (window.scanResults && window.scanResults.results && window.scanResults.results[iso]) {
      window.scanResults.results[iso].review_status = action === 'approve' ? 'approved' : 'rejected';
    }
  } catch (err) {
    clearEl(errorEl);
    errorEl.appendChild(makeError('Review failed for ' + iso + ': ' + err.message));
  }
}

async function bulkReview(action, isos) {
  for (const iso of isos) {
    try {
      const res = await fetch('/api/scan/review', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ country: iso, action })
      });
      if (res.ok) {
        const row = document.querySelector(`#mismatches-tbody tr[data-iso="${iso}"]`);
        if (row) {
          const actionsEl = row.querySelector('.review-actions');
          if (actionsEl) renderReviewActions(actionsEl, iso, action === 'approve' ? 'approved' : 'rejected');
        }
        if (window.scanResults && window.scanResults.results && window.scanResults.results[iso]) {
          window.scanResults.results[iso].review_status = action === 'approve' ? 'approved' : 'rejected';
        }
      }
    } catch (_) { /* continue with next */ }
  }
}

/* ── Export View ──────────────────────────────────────────────────────────── */
document.getElementById('btn-dl-vba').addEventListener('click', downloadVBA);
document.getElementById('btn-dl-json').addEventListener('click', downloadJSON);

async function downloadVBA() {
  const btn     = document.getElementById('btn-dl-vba');
  const errorEl = document.getElementById('export-error');
  clearEl(errorEl);
  btn.disabled = true;

  try {
    const res = await fetch('/api/scan/export', { method: 'POST' });
    if (!res.ok) throw new Error('HTTP ' + res.status + ': ' + res.statusText);

    const blob     = await res.blob();
    const url      = URL.createObjectURL(blob);
    const filename = getFilenameFromResponse(res) || 'corrected_vba.bas';
    triggerDownload(url, filename);
    URL.revokeObjectURL(url);
  } catch (err) {
    errorEl.appendChild(makeError('Export failed: ' + err.message));
  } finally {
    btn.disabled = false;
  }
}

async function downloadJSON() {
  const btn     = document.getElementById('btn-dl-json');
  const errorEl = document.getElementById('export-error');
  clearEl(errorEl);
  btn.disabled = true;

  try {
    const res = await fetch('/api/scan/results');
    if (!res.ok) throw new Error('HTTP ' + res.status + ': ' + res.statusText);
    const data = await res.json();
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const url  = URL.createObjectURL(blob);
    triggerDownload(url, 'vat-validation-report.json');
    URL.revokeObjectURL(url);
  } catch (err) {
    errorEl.appendChild(makeError('JSON export failed: ' + err.message));
  } finally {
    btn.disabled = false;
  }
}

function getFilenameFromResponse(res) {
  const cd = res.headers.get('Content-Disposition') || '';
  const m  = cd.match(/filename[^;=\n]*=((['"]).*?\2|[^;\n]*)/i);
  return m ? m[1].replace(/['"]/g, '') : null;
}

function triggerDownload(url, filename) {
  const a    = document.createElement('a');
  a.href     = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

/* ── Reasoning Diary ──────────────────────────────────────────────────────── */
function buildDiary() {
  const diarySection = document.getElementById('reasoning-diary');
  const diaryEntries = document.getElementById('diary-entries');

  if (!window.scanWasTestMode) {
    diarySection.style.display = 'none';
    return;
  }
  if (!window.scanResults || !window.scanResults.results) {
    diarySection.style.display = 'none';
    return;
  }

  diarySection.style.display = 'block';
  clearEl(diaryEntries);

  const results = window.scanResults.results;

  Object.entries(results).forEach(([iso, r]) => {
    const score     = r.confidence_score != null ? r.confidence_score : 0;
    const countryName = r.country_name || r.country || iso;
    const emoji     = scoreEmoji(score);

    const details = document.createElement('details');
    details.className = 'mb-1';

    const summary = document.createElement('summary');
    summary.textContent = emoji + ' ' + countryName + ' (' + iso + ') — ' + score.toFixed(2) + ' confidence';
    details.appendChild(summary);

    const body = document.createElement('div');
    body.className = 'details-body';

    // Human review warning
    if (r.needs_human_review) {
      const warn = document.createElement('div');
      warn.className   = 'warn-box mb-05';
      warn.textContent = '⚠ HUMAN REVIEW REQUIRED: ' + (r.review_reason || 'No reason provided.');
      body.appendChild(warn);
    }

    // Confidence bar
    const barDiv = document.createElement('div');
    barDiv.className   = 'mb-05 small';
    barDiv.textContent = 'Confidence: ' + confBar(score);
    body.appendChild(barDiv);

    // Source agreement / temporal notes
    if (r.source_agreement) {
      const sa = document.createElement('p');
      sa.className   = 'small mb-05';
      sa.innerHTML   = '<span class="label">Source Agreement:</span> ' + escHtml(r.source_agreement);
      body.appendChild(sa);
    }
    if (r.temporal_notes) {
      const tn = document.createElement('p');
      tn.className   = 'small mb-05';
      tn.innerHTML   = '<span class="label">Temporal Notes:</span> ' + escHtml(r.temporal_notes);
      body.appendChild(tn);
    }

    // Reasoning
    if (r.reasoning) {
      const rd = document.createElement('p');
      rd.className   = 'small mb-1';
      rd.innerHTML   = '<span class="label">Reasoning:</span> ' + escHtml(r.reasoning);
      body.appendChild(rd);
    }

    // Sources table
    if (r.sources_analyzed && r.sources_analyzed.length > 0) {
      const srcHeader = document.createElement('div');
      srcHeader.className   = 'label mb-05';
      srcHeader.textContent = '[ SOURCES ANALYZED ]';
      body.appendChild(srcHeader);

      const wrap = document.createElement('div');
      wrap.style.overflowX = 'auto';

      const tbl = document.createElement('table');
      tbl.innerHTML = `
        <thead>
          <tr>
            <th>URL</th>
            <th>Type</th>
            <th>Claimed Rate</th>
            <th>Publication Date</th>
            <th>Direct Quote</th>
          </tr>
        </thead>
      `;
      const stbody = document.createElement('tbody');
      r.sources_analyzed.forEach(src => {
        const str = document.createElement('tr');
        str.innerHTML = `
          <td style="word-break:break-all; max-width:220px;">
            ${src.url ? '<a href="' + escHtml(src.url) + '" target="_blank" rel="noopener">' + escHtml(src.url) + '</a>' : '—'}
          </td>
          <td>${escHtml(src.type || '—')}</td>
          <td>${src.claimed_rate != null ? escHtml(String(src.claimed_rate)) : '—'}</td>
          <td>${escHtml(src.publication_date || '—')}</td>
          <td style="font-size:0.78rem; max-width:240px;">${escHtml(src.direct_quote || '—')}</td>
        `;
        stbody.appendChild(str);
      });
      tbl.appendChild(stbody);
      wrap.appendChild(tbl);
      body.appendChild(wrap);
    }

    details.appendChild(body);
    diaryEntries.appendChild(details);
  });
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/* ── Logs View ────────────────────────────────────────────────────────────── */
document.getElementById('btn-refresh-logs').addEventListener('click', loadLogList);

async function loadLogList() {
  const listEl  = document.getElementById('logs-list');
  const errorEl = document.getElementById('logs-error');
  clearEl(listEl);
  clearEl(errorEl);

  try {
    const res = await fetch('/api/logs');
    if (!res.ok) throw new Error('HTTP ' + res.status + ': ' + res.statusText);
    const files = await res.json();

    if (!files || files.length === 0) {
      listEl.textContent = '(no log files found)';
      return;
    }

    // Show most recent first
    const sorted = [...files].reverse();
    sorted.forEach(filename => {
      const btn = document.createElement('button');
      btn.textContent  = filename;
      btn.style.display = 'block';
      btn.style.width   = '100%';
      btn.style.textAlign = 'left';
      btn.style.marginBottom = '0.3rem';
      btn.style.fontSize = '0.82rem';
      btn.addEventListener('click', () => loadLogFile(filename));
      listEl.appendChild(btn);
    });
  } catch (err) {
    errorEl.appendChild(makeError('Failed to load log list: ' + err.message));
  }
}

async function loadLogFile(filename) {
  const errorEl   = document.getElementById('logs-error');
  const contentEl = document.getElementById('logs-content-pre');
  const labelEl   = document.getElementById('logs-viewing-label');
  clearEl(errorEl);

  labelEl.textContent    = '[ VIEWING: ' + filename + ' ]';
  contentEl.style.display = 'block';
  contentEl.textContent   = 'Loading...';

  try {
    const res = await fetch('/api/logs/' + encodeURIComponent(filename));
    if (!res.ok) throw new Error('HTTP ' + res.status + ': ' + res.statusText);
    const text = await res.text();
    contentEl.textContent = text || '(empty)';
    // Scroll to bottom
    contentEl.scrollTop = contentEl.scrollHeight;
  } catch (err) {
    contentEl.style.display = 'none';
    errorEl.appendChild(makeError('Failed to load log: ' + err.message));
  }
}

/* ── Boot ─────────────────────────────────────────────────────────────────── */
showView('view-home');
