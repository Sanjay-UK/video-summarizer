// Configuration
const API_BASE = '/api';
const PROGRESS_POLL_INTERVAL = 1000; // 1 second
const SUBTASK_STEPS = {
 'downloading': 10,
 'extracting_audio': 25,
 'transcribing': 65,
 'summarizing': 85,
 'generating_audio': 92,
 'generating_subtitles': 100,
 'highlight_topics': 94,
 'highlight_visuals': 97,
 'highlight_assembly': 99,
 'highlight_complete': 100
};

const SUBTASK_NAMES = {
 'downloading': 'download',
 'extracting_audio': 'audio',
 'transcribing': 'transcribe',
 'summarizing': 'summarize',
 'generating_audio': 'audio-gen',
 'generating_subtitles': 'subtitles',
 'highlight_topics': 'highlight',
 'highlight_visuals': 'highlight',
 'highlight_assembly': 'highlight',
 'highlight_complete': 'highlight'
};

let highlightRequested = false;

function getPipelineRuntimeValue() {
 const toggle = document.getElementById('pipelineRuntimeToggle');
 return toggle && toggle.checked ? 'runpod' : 'local';
}

function syncNavbarGpuLinkVisibility() {
 const toggle = document.getElementById('pipelineRuntimeToggle');
 const link = document.getElementById('navbarGpuLink');
 if (!toggle || !link) return;
 if (toggle.checked) {
 link.removeAttribute('hidden');
 link.setAttribute('aria-hidden', 'false');
 } else {
 link.setAttribute('hidden', '');
 link.setAttribute('aria-hidden', 'true');
 }
}

async function savePipelineRuntimeFromToggle() {
 const runtime = getPipelineRuntimeValue();
 syncNavbarGpuLinkVisibility();
 try {
 await fetch(`${API_BASE}/pipeline-runtime`, {
 method: 'POST',
 headers: { 'Content-Type': 'application/json' },
 body: JSON.stringify({ pipeline_runtime: runtime }),
 });
 } catch (e) {
 console.warn('[APP] pipeline runtime save failed', e);
 }
}

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
 audioSource.type = 'audio/mpeg';
 audioSource.src = `/download/audio/${currentJobId}`;
 } else {
 audioSource.type = 'video/mp4';
 audioSource.src = highlightStreamUrl(currentJobId, code);
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
 player.src = highlightStreamUrl(currentJobId, code);
 player.load();
 if (dlBtn) dlBtn.style.display = 'inline-block';
 } else if (results.highlight_video) {
 playerWrap.style.display = 'block';
 player.removeAttribute('src');
 if (dlBtn) dlBtn.style.display = 'none';
 } else {
 playerWrap.style.display = 'none';
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
 const ok = row.status === 'success' && row.download_available;
 const label = ok
 ? `✓ ${escapeHtml(row.name)} (${escapeHtml(row.code)})`
 : `✗ ${escapeHtml(row.name)} (${escapeHtml(row.code)})`;
 const translatorLabel =
 row.translator === 'source' || row.code === 'en'
 ? 'English source'
 : row.translator
 ? row.translator
 : '';
 const detail = ok
 ? [
 translatorLabel ? `Translated with: ${escapeHtml(translatorLabel)}` : '',
 row.translation_seconds != null ? `translate ${row.translation_seconds}s` : '',
 row.tts_seconds != null ? `TTS ${row.tts_seconds}s` : '',
 row.render_seconds != null ? `render ${row.render_seconds}s` : '',
 row.total_seconds != null ? `total ${row.total_seconds}s` : '',
 ]
 .filter(Boolean)
 .join(' · ')
 : escapeHtml(row.message || 'Failed');
 return `<div class="highlight-lang-row" style="margin: 8px 0; padding: 8px 10px; border-radius: 6px; background: ${ok ? 'rgba(46,125,50,0.12)' : 'rgba(198,40,40,0.1)'};"><strong>${label}</strong><div style="font-size: 0.9em; margin-top: 4px;">${detail}</div></div>`;
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

/** Fixed summarizer model (Ollama on RunPod/local). Change via GPU page serverless settings, not homepage. */
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
 formData.append('pipeline_runtime', getPipelineRuntimeValue());
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

function startProgressPolling() {
 if (progressPoller) clearInterval(progressPoller);

 progressPoller = setInterval(async () => {
 if (!currentJobId) {
 clearInterval(progressPoller);
 return;
 }

 try {
 const response = await fetch(`${API_BASE}/progress/${currentJobId}`);
 if (!response.ok) throw new Error('Failed to fetch progress');

 const data = await response.json();
 console.log('[PROGRESS]', data);

 updateProgressUI(data);

 // If complete or error, stop polling
 if (data.status === 'completed' || data.status === 'error') {
 clearInterval(progressPoller);
 if (data.status === 'error') {
 showError(data.error || 'An error occurred during processing');
 } else {
 showResults(data);
 }
 }
 } catch (error) {
 console.error('[PROGRESS ERROR]', error);
 }
 }, PROGRESS_POLL_INTERVAL);
}

function updateProgressUI(data) {
 const { progress, current_step, status, log_lines } = data;

 syncProgressJobIdDisplay();

 // Update overall progress
 const percent = Math.round(progress * 100);
 document.getElementById('progressFill').style.width = percent + '%';
 document.getElementById('progressText').textContent = percent + '%';

 // Update subtasks based on current step and progress percentage
 updateSubtaskUI(current_step, progress);

 // Update logs
 if (log_lines && log_lines.length > 0) {
 const logContent = document.getElementById('logContent');
 logContent.innerHTML = log_lines
 .map(line => `<div class="log-entry">${escapeHtml(line)}</div>`)
 .join('');
 logContent.scrollTop = logContent.scrollHeight;
 }
}

function updateSubtaskUI(currentStep, progress) {
 // Mark completed subtasks
 let targetPercent = 0;
 for (const [step, stepPercent] of Object.entries(SUBTASK_STEPS)) {
 const subtaskId = `subtask-${SUBTASK_NAMES[step]}`;
 const subtask = document.getElementById(subtaskId);
 if (!subtask) continue;

 if (step === currentStep) {
 subtask.classList.add('active');
 subtask.classList.remove('completed');
 subtask.querySelector('.subtask-checkbox').textContent = '';
 } else if (SUBTASK_STEPS[step] < (progress * 100)) {
 subtask.classList.add('completed');
 subtask.classList.remove('active');
 subtask.querySelector('.subtask-checkbox').textContent = '';
 } else {
 subtask.classList.remove('completed', 'active');
 subtask.querySelector('.subtask-checkbox').textContent = '';
 }
 }
}

async function showResults(data) {
 clearInterval(progressPoller);

 document.getElementById('progressSection').style.display = 'none';
 document.getElementById('resultsSection').style.display = 'block';
 document.getElementById('jobIdDisplay').textContent = `Video ID: ${currentJobId}`;
 history.pushState({ jobId: currentJobId }, '', `/job/${currentJobId}`);

 try {
 // Fetch final results
 const response = await fetch(`${API_BASE}/results/${currentJobId}`);
 if (!response.ok) throw new Error('Failed to fetch results');

 const results = await response.json();
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
 if (currentJobId && langCode) {
 window.location.href = `/download/highlight/${currentJobId}/${langCode}`;
 }
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

 fetch(`${API_BASE}/results/${jobId}`)
 .then(r => {
 console.log('[LOAD JOB] Response status:', r.status);
 return r.json();
 })
 .then(results => {
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

 const runtimeToggle = document.getElementById('pipelineRuntimeToggle');
 if (runtimeToggle) {
 syncNavbarGpuLinkVisibility();
 runtimeToggle.addEventListener('change', () => {
 savePipelineRuntimeFromToggle();
 });
 }

 // Refresh history every 5 seconds
 setInterval(loadHistory, 5000);
});
