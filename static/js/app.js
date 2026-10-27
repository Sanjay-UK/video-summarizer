// Configuration
const API_BASE = '/api';
const PROGRESS_POLL_INTERVAL = 1000; // 1 second
// Pipeline order of the steps the backend reports. The checklist follows this order, not percentages, so it always
// agrees with the stage label shown in the status line.
const SUBTASK_ORDER = [
 'downloading',
 'extracting_audio',
 'transcribing',
 'summarizing',
 'translating',
 'generating_audio',
 'generating_subtitles',
 'highlight_topics',
 'highlight_visuals',
 'highlight_assembly',
 'highlight_complete'
];

const SUBTASK_NAMES = {
 'downloading': 'download',
 'extracting_audio': 'audio',
 'transcribing': 'transcribe',
 'summarizing': 'summarize',
 'translating': 'summarize',
 'generating_audio': 'audio-gen',
 'generating_subtitles': 'subtitles',
 'highlight_topics': 'highlight',
 'highlight_visuals': 'highlight',
 'highlight_assembly': 'highlight',
 'highlight_complete': 'highlight'
};

// Furthest SUBTASK_ORDER index shown on the checklist for the current job. Only moves forward.
let subtaskHighWater = -1;

let highlightRequested = false;

function toggleHighlightLangPanel() {
 /* Language panel stays visible for summary + optional highlight video. */
}

function getSelectedOutputLanguages() {
 const boxes = document.querySelectorAll('input[name="highlight_languages"]:checked');
 let langs = Array.from(boxes).map((b) => b.value);
 if (langs.length === 0) {
 langs = ['en'];
 }
 if (!langs.includes('en')) {
 langs.unshift('en');
 }
 return langs;
}

function getSelectedHighlightLanguages() {
 return getSelectedOutputLanguages();
}

function shouldGenerateHighlightVideo() {
 const checkbox = document.getElementById('highlightVideo');
 const langs = getSelectedOutputLanguages();
 return checkbox?.checked === true || langs.some((code) => code !== 'en');
}

let cachedResults = null;
let selectedResultsLang = 'en';

const SUPPORTED_RESULTS_LANGS = new Set(['en', 'hi', 'kn', 'te', 'ta']);

function sanitizeSummaryMarkdown(text) {
 if (!text) {
 return '';
 }
 let t = text;
 t = t.replace(/\\u([0-9a-fA-F]{4})/g, (_, hex) => {
 const cp = parseInt(hex, 16);
 return Number.isFinite(cp) ? String.fromCharCode(cp) : _;
 });
 t = t.replace(/\\n/g, '\n').replace(/\\t/g, '\t');
 t = t.replace(/(\S+)(?:\s+\1){5,}/gu, '$1 $1');
 return t;
}

function renderMarkdownInto(el, text) {
 if (!el) return;
 marked.setOptions({ breaks: true, gfm: true });
 el.innerHTML = marked.parse(sanitizeSummaryMarkdown(text || ''));
 el.classList.add('markdown-body');
}

function highlightStreamUrl(jobId, langCode) {
 return `/stream/highlight/${jobId}/${langCode}`;
}

// A fresh one-hour S3 link from the backend, requested every time it is needed. Nothing is cached here.
// Jobs that were not stored in S3 (local mode, older jobs) fall back to the local file routes.
async function fetchHighlightUrl(jobId, langCode, attachment = false) {
 const query = attachment ? '?attachment=1' : '';
 const res = await fetch(`/api/highlight-url/${jobId}/${langCode}${query}`, { cache: 'no-store' });
 const body = await res.json().catch(() => ({}));
 if (res.ok) return body.url;
 if (res.status === 404 && body.code === 'not_in_s3') {
 return attachment ? `/download/highlight/${jobId}/${langCode}` : highlightStreamUrl(jobId, langCode);
 }
 throw new Error(body.error || `Highlight link failed (HTTP ${res.status})`);
}

function showHighlightPlayerError(message) {
 const el = document.getElementById('highlightPlayerError');
 if (!el) return;
 el.textContent = message;
 el.style.display = message ? 'block' : 'none';
}

// Point a media element at a fresh link. If playback fails, request one fresh link and retry once per attempt.
// A second failure is shown to the user, so the page never loops.
function bindHighlightSource(mediaEl, reloadEl, jobId, langCode) {
 const token = {};
 mediaEl._highlightToken = token;
 let retried = false;
 const apply = () => fetchHighlightUrl(jobId, langCode)
 .then((url) => {
 if (mediaEl._highlightToken !== token) return;
 showHighlightPlayerError('');
 mediaEl.src = url;
 reloadEl.load();
 })
 .catch((err) => {
 if (mediaEl._highlightToken === token) showHighlightPlayerError(err.message);
 });
 mediaEl.onerror = () => {
 if (retried) {
 showHighlightPlayerError('This highlight could not be played. Reopen the job to request a new link.');
 return;
 }
 retried = true;
 apply();
 };
 // One automatic refresh per playback attempt. Loading data must not reset it, or a link that loads and then
 // fails again would be refreshed forever. A new attempt starts only when the user opens a job or switches language.
 apply();
}

function unbindHighlightSource(mediaEl) {
 mediaEl._highlightToken = null;
 mediaEl.onerror = null;
}

function mergedOutputLanguages(results) {
 const byCode = new Map();
 (results.output_languages || []).forEach((l) => {
 if (l && l.code) {
 byCode.set(l.code, { code: l.code, name: l.name || l.code });
 }
 });
 (results.summary_translation_status || []).forEach((row) => {
 if (row && row.code && !byCode.has(row.code)) {
 byCode.set(row.code, { code: row.code, name: row.name || row.code });
 }
 });
 const tr = results.summary_translations || {};
 Object.keys(tr).forEach((code) => {
 if (!byCode.has(code)) {
 const row = (results.summary_translation_status || []).find((r) => r.code === code);
 byCode.set(code, { code, name: row?.name || code });
 }
 });
 if (!byCode.has('en')) {
 byCode.set('en', { code: 'en', name: 'English' });
 }
 return Array.from(byCode.values());
}

function populateResultsLanguageSelect(results) {
 const select = document.getElementById('resultsLangSelect');
 const langs = mergedOutputLanguages(results);
 if (!select) {
 return;
 }
 select.innerHTML = langs
 .map((l) => `<option value="${escapeHtml(l.code)}">${escapeHtml(l.name)}</option>`)
 .join('');
 selectedResultsLang = langs[0]?.code || 'en';
 select.value = selectedResultsLang;
}

function onResultsLanguageChange() {
 const select = document.getElementById('resultsLangSelect');
 selectedResultsLang = select?.value || 'en';
 applyResultsLanguageView();
}

function summaryTextForLang(results, code) {
 if (!SUPPORTED_RESULTS_LANGS.has(code)) {
 code = 'en';
 }
 if (code === 'en') {
 return results.summary || '';
 }
 const tr = results.summary_translations || {};
 if (tr[code]) {
 return tr[code];
 }
 const row = (results.summary_translation_status || []).find((r) => r.code === code);
 if (row && row.status === 'failed') {
 return `*Summary translation for ${row.name} failed:* ${row.message || ''}`;
 }
 return results.summary || '';
}

function applyResultsLanguageView() {
 if (!cachedResults || !currentJobId) {
 return;
 }
 const code = selectedResultsLang;
 const results = cachedResults;

 renderMarkdownInto(
 document.getElementById('summaryContentDisplay'),
 summaryTextForLang(results, code)
 );

 const audioPlayer = document.getElementById('audioPlayer');
 const audioSource = document.getElementById('audioSource');
 if (audioSource && audioPlayer) {
 if (code === 'en') {
 unbindHighlightSource(audioSource);
 audioSource.type = 'audio/mpeg';
 audioSource.src = `/download/audio/${currentJobId}`;
 } else {
 audioSource.type = 'video/mp4';
 bindHighlightSource(audioSource, audioPlayer, currentJobId, code);
 }
 audioPlayer.load();
 }

 const playerWrap = document.getElementById('highlightPlayerWrap');
 const player = document.getElementById('highlightVideoPlayer');
 const dlBtn = document.getElementById('highlightDownloadBtn');
 const row = (results.highlight_language_status || []).find((r) => r.code === code);
 const hasHighlight = row?.status === 'success' && row.download_available;
 if (playerWrap && player) {
 if (hasHighlight) {
 playerWrap.style.display = 'block';
 bindHighlightSource(player, player, currentJobId, code);
 if (dlBtn) dlBtn.style.display = 'inline-block';
 } else if (results.highlight_video) {
 playerWrap.style.display = 'block';
 unbindHighlightSource(player);
 player.removeAttribute('src');
 if (dlBtn) dlBtn.style.display = 'none';
 } else {
 playerWrap.style.display = 'none';
 unbindHighlightSource(player);
 if (dlBtn) dlBtn.style.display = 'none';
 }
 }
}

function initResultsPage(jobId, results) {
 cachedResults = {
 ...results,
 output_languages: mergedOutputLanguages(results),
 };
 currentJobId = jobId;
 populateResultsLanguageSelect(cachedResults);
 renderHighlightDownloads(jobId, cachedResults);
 const listenCard = document.getElementById('listenResultCard');
 if (listenCard) {
 const anyHighlight = (cachedResults.highlight_language_status || []).some(
 (r) => r.status === 'success' && r.download_available
 );
 listenCard.style.display =
 cachedResults.has_audio || anyHighlight ? 'block' : 'none';
 }
 applyResultsLanguageView();
}

// What one requested highlight language shows. Not generated is neutral, never a failure; only attempted and
// failed languages are shown as Failed, with the reason. Pure function, so it can be tested without a browser.
function highlightRowView(row) {
 if (row.status === 'success' && row.download_available) {
 return { ok: true, symbol: '✓', detail: '', background: 'rgba(46,125,50,0.12)' };
 }
 if (row.status === 'not_generated') {
 return { ok: false, symbol: '○', detail: 'Not generated', background: 'rgba(100,100,100,0.08)' };
 }
 return { ok: false, symbol: '✗', detail: row.message || 'Failed', background: 'rgba(198,40,40,0.1)' };
}

function renderHighlightDownloads(jobId, results) {
 const card = document.getElementById('highlightResultCard');
 const list = document.getElementById('highlightDownloadList');
 const statusList = document.getElementById('highlightLangStatusList');
 const retryBtn = document.getElementById('highlightRetryBtn');
 const playerWrap = document.getElementById('highlightPlayerWrap');
 const player = document.getElementById('highlightVideoPlayer');
 if (!card || !list) {
 return;
 }
 const statusRows = results.highlight_language_status || [];
 const langs = results.highlight_languages || [];
 const hasHighlight =
 statusRows.length > 0 || (results.highlight_video && langs.length > 0);
 if (!hasHighlight) {
 card.style.display = 'none';
 list.innerHTML = '';
 if (statusList) statusList.innerHTML = '';
 if (retryBtn) retryBtn.style.display = 'none';
 if (playerWrap) playerWrap.style.display = 'none';
 if (player) player.removeAttribute('src');
 return;
 }
 card.style.display = 'block';
 if (statusList && statusRows.length > 0) {
 statusList.innerHTML = statusRows
 .map((row) => {
 const view = highlightRowView(row);
 const ok = view.ok;
 const label = `${view.symbol} ${escapeHtml(row.name)} (${escapeHtml(row.code)})`;
 const detail = ok
 ? [
 row.translation_seconds != null ? `translate ${row.translation_seconds}s` : '',
 row.tts_seconds != null ? `TTS ${row.tts_seconds}s` : '',
 row.render_seconds != null ? `render ${row.render_seconds}s` : '',
 row.total_seconds != null ? `total ${row.total_seconds}s` : '',
 ]
 .filter(Boolean)
 .join(' · ')
 : escapeHtml(view.detail);
 return `<div class="highlight-lang-row" style="margin: 8px 0; padding: 8px 10px; border-radius: 6px; background: ${view.background};"><strong>${label}</strong><div style="font-size: 0.9em; margin-top: 4px;">${detail}</div></div>`;
 })
 .join('');
 } else if (statusList) {
 statusList.innerHTML = '';
 }
 list.innerHTML = '';

 const anyFailed = statusRows.some((r) => r.status === 'failed');
 if (retryBtn) {
 retryBtn.style.display = anyFailed && jobId ? 'inline-block' : 'none';
 }
}

async function retryFailedHighlightLanguages() {
 if (!currentJobId) {
 return;
 }
 const retryBtn = document.getElementById('highlightRetryBtn');
 if (retryBtn) {
 retryBtn.disabled = true;
 retryBtn.textContent = 'Retrying…';
 }
 try {
 const resp = await fetch(`/api/highlight/retry/${currentJobId}`, {
 method: 'POST',
 headers: { 'Content-Type': 'application/json' },
 body: JSON.stringify({}),
 });
 const data = await resp.json();
 if (!resp.ok) {
 throw new Error(data.error || 'Retry failed');
 }
 cachedResults = {
 ...cachedResults,
 highlight_video: data.highlight_video,
 highlight_languages: data.highlight_languages || [],
 highlight_language_status: data.highlight_language_status || [],
 };
 initResultsPage(currentJobId, cachedResults);
 } catch (e) {
 alert(e.message || String(e));
 } finally {
 if (retryBtn) {
 retryBtn.disabled = false;
 retryBtn.textContent = 'Retry failed languages';
 }
 }
}

let currentJobId = null;
let progressPoller = null;
let currentTab = 'url';

function switchTab(tab) {
 currentTab = tab;

 // Update tab buttons
 document.querySelectorAll('.tab-btn').forEach(btn => {
 btn.classList.remove('active');
 });
 event.target.classList.add('active');

 // Update tab content
 document.querySelectorAll('.tab-content').forEach(content => {
 content.classList.remove('active');
 });
 document.getElementById(tab + '-tab').classList.add('active');

 // Focus on input
 if (tab === 'url') {
 document.getElementById('youtubeUrl').focus();
 } else {
 document.getElementById('videoFile').focus();
 }

 validateInputs();
}

function updateFileInfo() {
 const fileInput = document.getElementById('videoFile');
 const fileInfo = document.getElementById('fileInfo');
 const fileName = document.getElementById('fileName');

 if (fileInput.files.length > 0) {
 const file = fileInput.files[0];
 const sizeMB = (file.size / (1024 * 1024)).toFixed(1);
 fileName.textContent = `${file.name} (${sizeMB}MB)`;
 fileInfo.style.display = 'block';
 } else {
 fileInfo.style.display = 'none';
 }
 validateInputs();
}

function validateInputs() {
 const summarizeBtn = document.getElementById('summarizeBtn');
 const urlInput = document.getElementById('youtubeUrl');
 const fileInput = document.getElementById('videoFile');

 let isValid = false;

 if (currentTab === 'url') {
 isValid = urlInput.value.trim().length > 0;
 } else {
 isValid = fileInput.files.length > 0;
 }

 summarizeBtn.disabled = !isValid;
}

// Drag and drop support
function initializeDragDrop() {
 const fileInput = document.getElementById('videoFile');
 const fileLabel = document.querySelector('.file-label');

 if (!fileLabel) return;

 fileLabel.addEventListener('dragover', (e) => {
 e.preventDefault();
 fileLabel.style.background = 'rgba(102, 126, 234, 0.1)';
 fileLabel.style.borderColor = '#667eea';
 });

 fileLabel.addEventListener('dragleave', () => {
 fileLabel.style.background = '';
 fileLabel.style.borderColor = '#e0e0e0';
 });

 fileLabel.addEventListener('drop', (e) => {
 e.preventDefault();
 fileLabel.style.background = '';
 fileLabel.style.borderColor = '#e0e0e0';

 const files = e.dataTransfer.files;
 if (files.length > 0) {
 fileInput.files = files;
 updateFileInfo();
 }
 });
}

/** Fixed summarizer model. Changed on the GPU selection page, not on the homepage. */
const SUMMARIZATION_MODEL = 'llama3.2:3b';

async function startSummarization() {
 const model = SUMMARIZATION_MODEL;
 const compression = document.querySelector('input[name="compression"]:checked').value;
 highlightRequested = shouldGenerateHighlightVideo();
 let source = null;

 if (currentTab === 'url') {
 source = document.getElementById('youtubeUrl').value.trim();
 if (!source) {
 showError('Please enter a YouTube URL');
 return;
 }
 } else {
 const fileInput = document.getElementById('videoFile');
 if (!fileInput.files.length) {
 showError('Please select a video file');
 return;
 }
 source = fileInput.files[0];
 }

 console.log('[APP] Starting summarization:', { source: currentTab === 'url' ? source : source.name, model, compression });

 // Hide input, show progress
 document.getElementById('inputSection').style.display = 'none';
 document.getElementById('resultsSection').style.display = 'none';
 document.getElementById('errorSection').style.display = 'none';
 document.getElementById('progressSection').style.display = 'block';

 // Reset progress UI (job ID is set after /summarize returns, then kept via syncProgressJobIdDisplay)
 resetProgressUI();
 const progressJobEl = document.getElementById('progressJobId');
 if (progressJobEl) {
 progressJobEl.textContent = '';
 }

 try {
 const formData = new FormData();
 formData.append('model', model);
 formData.append('compression', compression);
 getSelectedOutputLanguages().forEach((lang) => {
 formData.append('highlight_languages', lang);
 });
 if (highlightRequested) {
 formData.append('highlight_video', '1');
 }

 const highlightSubtask = document.getElementById('subtask-highlight');
 if (highlightSubtask) {
 highlightSubtask.style.display = highlightRequested ? 'flex' : 'none';
 }

 if (currentTab === 'url') {
 formData.append('url', source);
 } else {
 formData.append('file', source);
 }

 const response = await fetch(`${API_BASE}/summarize`, {
 method: 'POST',
 body: formData
 });

 if (!response.ok) {
 const error = await response.json();
 throw new Error(error.error || 'Failed to start summarization');
 }

 const data = await response.json();
 currentJobId = data.job_id;
 console.log('[APP] Job started:', currentJobId);
 syncProgressJobIdDisplay();

 // Start polling for progress
 startProgressPolling();
 } catch (error) {
 console.error('[APP] Error:', error);
 showError(error.message);
 }
}

function syncProgressJobIdDisplay() {
 const progressJobEl = document.getElementById('progressJobId');
 if (progressJobEl && currentJobId) {
 progressJobEl.textContent = `Job ID: ${currentJobId}`;
 }
}

function resetProgressUI() {
 document.getElementById('progressFill').style.width = '0%';
 document.getElementById('progressText').textContent = '0%';

 // Reset all subtasks
 subtaskHighWater = -1;
 Object.keys(SUBTASK_NAMES).forEach(step => {
 const subtaskId = `subtask-${SUBTASK_NAMES[step]}`;
 const subtask = document.getElementById(subtaskId);
 if (subtask) {
 subtask.classList.remove('completed', 'active');
 const checkbox = subtask.querySelector('.subtask-checkbox');
 if (checkbox) checkbox.textContent = '';
 }
 });

 // Clear logs
 document.getElementById('logContent').innerHTML = '<div class="log-entry">Initializing...</div>';
}

let displayedPercent = 0;

// Each poll tick carries the job, the polling generation, and a request sequence number. A response is applied only
// if it is still for the job being shown, belongs to the current generation, and is newer than the last applied one.
// The message to show for a failed job: the job's own sanitized error when the payload carries one.
function failureMessageFor(data) {
 const message = data && typeof data.error === 'string' ? data.error.trim() : '';
 return message || 'An error occurred during processing';
}

let progressGeneration = 0;
let pollSequence = 0;
let appliedSequence = 0;

function shouldApplyPoll(current, request) {
 if (request.jobId !== current.jobId) return false;             // the user switched jobs
 if (request.generation !== current.generation) return false;   // from a previous polling run
 if (request.responseJobId != null && request.responseJobId !== request.jobId) return false;
 return request.sequence > current.appliedSequence;             // older than an update already applied
}

function startProgressPolling() {
 if (progressPoller) clearInterval(progressPoller);
 displayedPercent = 0;
 const generation = ++progressGeneration;
 const jobId = currentJobId;
 pollSequence = 0;
 appliedSequence = 0;

 progressPoller = setInterval(async () => {
 if (!currentJobId || generation !== progressGeneration) {
 clearInterval(progressPoller);
 return;
 }
 const sequence = ++pollSequence;

 try {
 const response = await fetch(`${API_BASE}/progress/${jobId}`);
 if (!response.ok) throw new Error('Failed to fetch progress');

 const data = await response.json();
 const accept = shouldApplyPoll(
 { jobId: currentJobId, generation: progressGeneration, appliedSequence },
 { jobId, generation, sequence, responseJobId: data.job_id }
 );
 if (!accept) return;
 appliedSequence = sequence;
 console.log('[PROGRESS]', data);

 updateProgressUI(data);

 // If complete or error, stop polling
 if (data.status === 'completed' || data.status === 'error') {
 clearInterval(progressPoller);
 if (data.status === 'error') {
 showError(failureMessageFor(data));
 } else {
 showResults(data);
 }
 }
 } catch (error) {
 console.error('[PROGRESS ERROR]', error);
 }
 }, PROGRESS_POLL_INTERVAL);
}

// What the progress bar should show for one status payload. It shows confirmed backend progress only:
// it never moves backward within a job, never shows 100 before the job is completed, and shows no number for a
// stage whose progress cannot be measured (indeterminate). Pure function, so it can be tested without a browser.
const STEP_LABELS = {
 downloading: 'Downloading video',
 extracting_audio: 'Extracting audio',
 transcribing: 'Transcribing',
 summarizing: 'Summarizing',
 translating: 'Translating summary',
 generating_audio: 'Generating narration',
 generating_subtitles: 'Generating subtitles',
 highlight_assembly: 'Rendering highlight video',
 highlight_complete: 'Finishing highlight video',
};

function stageLabel(step) {
 return STEP_LABELS[step] || 'Working';
}

function progressViewFor(data, previousPercent) {
 const previous = previousPercent || 0;
 if (data.status === 'completed') {
 return { percent: 100, indeterminate: false, label: 'Completed' };
 }
 if (data.status === 'error') {
 return { percent: previous, indeterminate: false, label: 'Failed' };
 }
 if (data.indeterminate) {
 return { percent: previous, indeterminate: true, label: stageLabel(data.current_step) };
 }
 const confirmed = Math.min(99, Math.max(0, Math.round((Number(data.progress) || 0) * 100)));
 return { percent: Math.max(previous, confirmed), indeterminate: false, label: null };
}

function updateProgressUI(data) {
 const { progress, current_step, status, log_lines } = data;

 syncProgressJobIdDisplay();

 // Update overall progress
 const view = progressViewFor(data, displayedPercent);
 displayedPercent = view.percent;
 const fill = document.getElementById('progressFill');
 if (view.indeterminate) {
 fill.classList.add('indeterminate');
 fill.style.width = '40%';
 const elapsed = Number(data.stage_elapsed_sec) || 0;
  document.getElementById('progressText').textContent =
 `${stageLabel(current_step)} · ${elapsed}s`;
 } else {
 fill.classList.remove('indeterminate');
 fill.style.width = view.percent + '%';
 document.getElementById('progressText').textContent = view.percent + '%';
 }

 // Update the checklist from the reported step, so it matches the stage label
 updateSubtaskUI(current_step, status === 'completed');

 // Update logs
 if (log_lines && log_lines.length > 0) {
 const logContent = document.getElementById('logContent');
 logContent.innerHTML = log_lines
 .map(line => `<div class="log-entry">${escapeHtml(line)}</div>`)
 .join('');
 logContent.scrollTop = logContent.scrollHeight;
 }
}

// State of each reported step: completed before the current one, active on it, pending after it. Pure, so it can be
// tested. An unknown step leaves every step pending; finished marks every step completed.
function subtaskStates(currentStep, finished) {
 const current = SUBTASK_ORDER.indexOf(currentStep);
 const states = {};
 SUBTASK_ORDER.forEach((step, index) => {
 if (finished || (current >= 0 && index < current)) states[step] = 'completed';
 else if (index === current) states[step] = 'active';
 else states[step] = 'pending';
 });
 return states;
}

// Checklist items group several steps (for example, both summarize steps). An item is active while any of its steps
// is active, completed once all of its steps are completed, and pending otherwise.
function subtaskItemStates(states) {
 const items = {};
 for (const name of new Set(Object.values(SUBTASK_NAMES))) {
 const own = Object.keys(states).filter(step => SUBTASK_NAMES[step] === name).map(step => states[step]);
 if (own.includes('active')) items[name] = 'active';
 else if (own.length > 0 && own.every(state => state === 'completed')) items[name] = 'completed';
 else items[name] = 'pending';
 }
 return items;
}

function updateSubtaskUI(currentStep, finished) {
 // A late or out-of-order report must never move the checklist back, so keep the furthest step seen for this job.
 subtaskHighWater = Math.max(subtaskHighWater, SUBTASK_ORDER.indexOf(currentStep));
 const furthest = SUBTASK_ORDER[subtaskHighWater];
 const items = subtaskItemStates(subtaskStates(furthest, finished));
 for (const [name, state] of Object.entries(items)) {
 const subtask = document.getElementById(`subtask-${name}`);
 if (!subtask) continue;
 subtask.classList.toggle('active', state === 'active');
 subtask.classList.toggle('completed', state === 'completed');
 subtask.querySelector('.subtask-checkbox').textContent = '';
 // The highlight item is hidden until a highlight step is reached, so show it as soon as one is.
 if (name === 'highlight' && state !== 'pending') subtask.style.display = 'flex';
 }
}

// One results request, checked before its JSON is trusted. A failed job answers with its own error (422 or 500).
async function fetchResultsOrError(jobId) {
 let response;
 try {
 response = await fetch(`${API_BASE}/results/${jobId}`, { cache: 'no-store' });
 } catch (err) {
 return { ok: false, error: 'Could not reach the server to load this job.' };
 }
 const body = await response.json().catch(() => null);
 if (response.ok) {
 if (body && typeof body === 'object') return { ok: true, results: body };
 return { ok: false, error: 'The job result could not be read.' };
 }
 if (body && body.error) return { ok: false, error: body.error, code: body.code || null, status: response.status };
 if (response.status === 404) return { ok: false, error: 'This job was not found.', status: 404 };
 return { ok: false, error: `Could not load this job (HTTP ${response.status}).`, status: response.status };
}

async function showResults(data) {
 clearInterval(progressPoller);

 document.getElementById('progressSection').style.display = 'none';
 document.getElementById('resultsSection').style.display = 'block';
 document.getElementById('jobIdDisplay').textContent = `Video ID: ${currentJobId}`;
 history.pushState({ jobId: currentJobId }, '', `/job/${currentJobId}`);

 try {
 // Fetch final results
 const outcome = await fetchResultsOrError(currentJobId);
 if (!outcome.ok) {
 showError(outcome.error);
 return;
 }
 const results = outcome.results;
 console.log('[RESULTS]', results);

 initResultsPage(currentJobId, results);

 // Calculate stats
 const origLength = (results.transcript_length || 0);
 const summLength = (results.summary_length || 0);
 const compression = origLength > 0 ? Math.round((1 - summLength / origLength) * 100) : 0;

 document.getElementById('originalLength').textContent = `${Math.round(origLength / 100) / 10}k chars`;
 document.getElementById('summaryLength').textContent = `${Math.round(summLength / 100) / 10}k chars`;
 document.getElementById('compression').textContent = `${compression}%`;

 } catch (error) {
 console.error('[RESULTS ERROR]', error);
 }
}

function showError(message) {
 document.getElementById('inputSection').style.display = 'none';
 document.getElementById('progressSection').style.display = 'none';
 document.getElementById('resultsSection').style.display = 'none';
 document.getElementById('errorSection').style.display = 'block';
 document.getElementById('errorMessage').textContent = message;
}

function resetForm(updateUrl = true) {
 currentJobId = null;
 if (progressPoller) clearInterval(progressPoller);

 document.getElementById('youtubeUrl').value = '';
 document.getElementById('videoFile').value = '';
 document.getElementById('fileInfo').style.display = 'none';
 document.getElementById('inputSection').style.display = 'block';
 document.getElementById('progressSection').style.display = 'none';
 document.getElementById('resultsSection').style.display = 'none';
 document.getElementById('errorSection').style.display = 'none';

 if (updateUrl) {
 history.pushState({}, '', '/');
 }

 document.getElementById('youtubeUrl').focus();
 window.scrollTo(0, 0);
}

function goToDashboard() {
 resetForm();
}

function downloadSummary() {
 if (!currentJobId) {
 return;
 }
 const lang = selectedResultsLang || 'en';
 window.location.href =
 lang === 'en'
 ? `/download/summary/${currentJobId}`
 : `/download/summary/${currentJobId}/${lang}`;
}

function downloadListenAudio() {
 if (!currentJobId) {
 return;
 }
 if (selectedResultsLang === 'en') {
 window.location.href = `/download/audio/${currentJobId}`;
 return;
 }
 downloadHighlightForResultsLang();
}

function downloadHighlightForResultsLang() {
 if (!currentJobId || !selectedResultsLang) {
 return;
 }
 downloadHighlightLang(selectedResultsLang);
}

function downloadHighlightLang(langCode) {
 if (!currentJobId || !langCode) return;
 fetchHighlightUrl(currentJobId, langCode, true)
 .then((url) => { window.location.href = url; })
 .catch((err) => showHighlightPlayerError(err.message));
}

function escapeHtml(text) {
 const div = document.createElement('div');
 div.textContent = text;
 return div.innerHTML;
}

function loadHistory() {
 fetch(`${API_BASE}/history`)
 .then(r => r.json())
 .then(data => {
 const historyList = document.getElementById('historyList');
 const jobs = data.jobs || [];

 if (jobs.length === 0) {
 historyList.innerHTML = '<div class="history-empty">No videos processed yet</div>';
 return;
 }

 historyList.innerHTML = jobs.map((job) => {
 const hl = (job.highlights || []).map((h) => h.code.toUpperCase()).join(', ');
 const sumLangs = (job.summary_languages || ['en'])
 .map((c) => String(c).toUpperCase())
 .join(', ');
 const extras = [
 job.has_summary !== false ? `Summary: ${sumLangs}` : '',
 job.has_highlight_video && hl ? `Highlights: ${hl}` : '',
 job.has_audio ? 'Audio' : '',
 ].filter(Boolean);
 return `
 <div class="history-item" onclick="loadJobResults('${escapeHtml(job.job_id)}')">
 <div class="history-item-title">
 <span>${escapeHtml(truncate(job.source || job.job_id, 40))}</span>
 <span class="history-status ${escapeHtml(job.status)}">${escapeHtml(String(job.status || '').toUpperCase())}</span>
 </div>
 <div class="history-item-meta">
 <span class="history-item-id">ID: ${escapeHtml(job.job_id)}</span>
 <span> ${formatDate(job.created_at)}</span>
 ${job.completed_at ? `<span> ${formatDuration(job.created_at, job.completed_at)}</span>` : ''}
 </div>
 ${extras.length ? `<div class="history-item-meta" style="margin-top: 6px; font-size: 12px; color: #666;">${extras.map((e) => escapeHtml(e)).join(' · ')}</div>` : ''}
 </div>
 `;
 }).join('');
 })
 .catch(e => console.error('[HISTORY ERROR]', e));
}

function truncate(str, len) {
 return str.length > len ? str.substring(0, len) + '...' : str;
}

function formatDate(isoString) {
 const date = new Date(isoString);
 return date.toLocaleDateString() + ' ' + date.toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'});
}

function formatDuration(startIso, endIso) {
 const start = new Date(startIso);
 const end = new Date(endIso);
 const seconds = Math.round((end - start) / 1000);
 if (seconds < 60) return `${seconds}s`;
 if (seconds < 3600) return `${Math.round(seconds/60)}m`;
 return `${Math.round(seconds/3600)}h`;
}

function loadJobResults(jobId, updateUrl = true) {
 currentJobId = jobId;
 console.log('[LOAD JOB] Loading results for job:', jobId);

 document.getElementById('inputSection').style.display = 'none';
 document.getElementById('progressSection').style.display = 'none';
 document.getElementById('resultsSection').style.display = 'block';
 document.getElementById('jobIdDisplay').textContent = `Video ID: ${jobId}`;

 if (updateUrl) {
 history.pushState({ jobId }, '', `/job/${jobId}`);
 }

 // Scroll to results
 window.scrollTo(0, 0);

  fetchResultsOrError(jobId)
  .then(outcome => {
  if (!outcome.ok) {
  showError(outcome.error);
  return;
  }
  const results = outcome.results;
  console.log('[LOAD JOB] Results received:', results);

  initResultsPage(jobId, results);

  // Update stats
  const origLength = (results.transcript_length || 0);
  const summLength = (results.summary_length || 0);
  const compression = origLength > 0 ? Math.round((1 - summLength / origLength) * 100) : 0;

  document.getElementById('originalLength').textContent = `${Math.round(origLength / 100) / 10}k chars`;
  document.getElementById('summaryLength').textContent = `${Math.round(summLength / 100) / 10}k chars`;
  document.getElementById('compression').textContent = `${compression}%`;

  console.log('[LOAD JOB] Summary loaded successfully');
  })
  .catch(e => {
  console.error('[LOAD RESULTS ERROR]', e);
  const el = document.getElementById('summaryContentDisplay');
  if (el) {
  el.innerHTML = '<p style="color: red;">Error loading summary. Please try again.</p>';
  }
  });
}

function getJobIdFromPath() {
 const match = window.location.pathname.match(/^\/job\/([a-zA-Z0-9\-]+)$/);
 return match ? match[1] : null;
}

// Handle browser back/forward navigation
window.addEventListener('popstate', () => {
 const jobId = getJobIdFromPath();
 if (jobId) {
 loadJobResults(jobId, false);
 } else {
 resetForm(false);
 }
});

// Initialize on load
window.addEventListener('load', () => {
 initializeDragDrop();
 loadHistory();

 // If the page was loaded directly on a /job/<id> URL (shared link, refresh, or back button),
 // open that job's results immediately instead of the input form.
 const jobId = getJobIdFromPath();
 if (jobId) {
 loadJobResults(jobId, false);
 } else {
 document.getElementById('youtubeUrl').focus();
 }

 // Add input validation listeners
 document.getElementById('youtubeUrl').addEventListener('input', validateInputs);
 document.getElementById('videoFile').addEventListener('change', validateInputs);

 // Set initial button state
 validateInputs();
 toggleHighlightLangPanel();

 // Refresh history every 5 seconds
 setInterval(loadHistory, 5000);
});
