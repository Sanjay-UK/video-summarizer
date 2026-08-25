// Configuration
const API_BASE = '/api';
const PROGRESS_POLL_INTERVAL = 1000; // 1 second
const SUBTASK_STEPS = {
 'downloading': 10,
 'extracting_audio': 25,
 'transcribing': 65,
 'summarizing': 85,
 'generating_audio': 92,
 'generating_subtitles': 100
};

const SUBTASK_NAMES = {
 'downloading': 'download',
 'extracting_audio': 'audio',
 'transcribing': 'transcribe',
 'summarizing': 'summarize',
 'generating_audio': 'audio-gen',
 'generating_subtitles': 'subtitles'
};

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

async function startSummarization() {
 const model = document.querySelector('input[name="model"]:checked').value;
 const compression = document.querySelector('input[name="compression"]:checked').value;
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

 // Reset progress UI
 resetProgressUI();

 try {
 const formData = new FormData();
 formData.append('model', model);
 formData.append('compression', compression);

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

 // Start polling for progress
 startProgressPolling();
 } catch (error) {
 console.error('[APP] Error:', error);
 showError(error.message);
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

 // Display summary with markdown rendering
 const summaryContainer = document.getElementById('summaryContent');
 const summaryText = results.summary || 'No summary available';

 // Configure marked for safe rendering
 marked.setOptions({
 breaks: true,
 gfm: true,
 });

 // Render markdown to HTML
 const htmlContent = marked.parse(summaryText);
 summaryContainer.innerHTML = htmlContent;
 summaryContainer.classList.add('markdown-body');

 console.log('[SUMMARY] Rendered markdown:', summaryText.substring(0, 100) + '...');

 // Set audio player source and reload
 const audioPlayer = document.getElementById('audioPlayer');
 const audioSource = document.getElementById('audioSource');
 if (audioSource && audioPlayer) {
 const audioUrl = `/download/audio/${currentJobId}`;
 audioSource.src = audioUrl;
 audioPlayer.load(); // Critical: reload the audio element
 console.log('[RESULTS] Audio player loaded:', audioUrl);
 } else {
 console.warn('[RESULTS] Audio player elements not found');
 }

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
 if (currentJobId) {
 window.location.href = `/download/summary/${currentJobId}`;
 }
}

function downloadAudio() {
 if (currentJobId) {
 window.location.href = `/download/audio/${currentJobId}`;
 }
}

function downloadSubtitles() {
 if (currentJobId) {
 window.location.href = `/download/subtitles/${currentJobId}`;
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

 historyList.innerHTML = jobs.map((job, idx) => `
 <div class="history-item" onclick="loadJobResults('${job.job_id}')">
 <div class="history-item-title">
 <span>${truncate(job.source, 40)}</span>
 <span class="history-status ${job.status}">${job.status.toUpperCase()}</span>
 </div>
 <div class="history-item-meta">
 <span class="history-item-id">ID: ${job.job_id}</span>
 <span> ${formatDate(job.created_at)}</span>
 ${job.completed_at ? `<span> ${formatDuration(job.created_at, job.completed_at)}</span>` : ''}
 </div>
 </div>
 `).join('');
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

 // Display summary with markdown rendering
 const summaryContainer = document.getElementById('summaryContent');
 const summaryText = results.summary || 'No summary available';

 // Configure marked for safe rendering
 marked.setOptions({
 breaks: true,
 gfm: true,
 });

 // Render markdown to HTML
 const htmlContent = marked.parse(summaryText);
 summaryContainer.innerHTML = htmlContent;
 summaryContainer.classList.add('markdown-body');

 // Update audio player
 const audioPlayer = document.getElementById('audioPlayer');
 const audioSource = document.getElementById('audioSource');
 if (audioSource && audioPlayer) {
 const audioUrl = `/download/audio/${jobId}`;
 audioSource.src = audioUrl;
 audioPlayer.load(); // Reload the audio element
 console.log('[LOAD JOB] Audio source set to:', audioUrl);

 // Add error handling
 audioPlayer.onerror = function() {
 console.warn('[AUDIO] Failed to load audio for job:', jobId);
 };

 audioPlayer.oncanplay = function() {
 console.log('[AUDIO] Audio loaded successfully');
 };
 } else {
 console.warn('[LOAD JOB] Audio player elements not found');
 }

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
 document.getElementById('summaryContent').innerHTML = '<p style="color: red;">Error loading summary. Please try again.</p>';
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

 // Refresh history every 5 seconds
 setInterval(loadHistory, 5000);
});
