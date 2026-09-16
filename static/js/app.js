// Configuration
const API_BASE = '/api';
const PROGRESS_POLL_INTERVAL = 1000; // 1 second
const SUBTASK_STEPS = {
 'loading': 10,
 'extracting_audio': 25,
 'transcribing': 65,
 'summarizing': 100
};

const SUBTASK_NAMES = {
 'loading': 'loading',
 'extracting_audio': 'audio',
 'transcribing': 'transcribe',
 'summarizing': 'summarize'
};

let currentJobId = null;
let progressPoller = null;

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
 const fileInput = document.getElementById('videoFile');
 summarizeBtn.disabled = fileInput.files.length === 0;
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
 const fileInput = document.getElementById('videoFile');
 if (!fileInput.files.length) {
 showError('Please select a video file');
 return;
 }

 const source = fileInput.files[0];

 // Hide input, show progress
 document.getElementById('inputSection').style.display = 'none';
 document.getElementById('resultsSection').style.display = 'none';
 document.getElementById('errorSection').style.display = 'none';
 document.getElementById('progressSection').style.display = 'block';

 resetProgressUI();

 try {
 const formData = new FormData();
 formData.append('file', source);
 formData.append('model', 'llama3.2:3b');
 formData.append('compression', 'medium');

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
 startProgressPolling();
 } catch (error) {
 showError(error.message);
 }
}

function resetProgressUI() {
 document.getElementById('progressFill').style.width = '0%';
 document.getElementById('progressText').textContent = '0%';

 Object.keys(SUBTASK_NAMES).forEach(step => {
 const subtaskId = `subtask-${SUBTASK_NAMES[step]}`;
 const subtask = document.getElementById(subtaskId);
 if (subtask) {
 subtask.classList.remove('completed', 'active');
 const checkbox = subtask.querySelector('.subtask-checkbox');
 if (checkbox) checkbox.textContent = '';
 }
 });
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
 updateProgressUI(data);

 if (data.status === 'completed' || data.status === 'error') {
 clearInterval(progressPoller);
 if (data.status === 'error') {
 showError(data.error || 'An error occurred during processing');
 } else {
 showResults();
 }
 }
 } catch (error) {
 console.error('[PROGRESS ERROR]', error);
 }
 }, PROGRESS_POLL_INTERVAL);
}

function updateProgressUI(data) {
 const { progress, current_step } = data;

 const percent = Math.round(progress * 100);
 document.getElementById('progressFill').style.width = percent + '%';
 document.getElementById('progressText').textContent = percent + '%';

 updateSubtaskUI(current_step, progress);
}

function updateSubtaskUI(currentStep, progress) {
 for (const [step] of Object.entries(SUBTASK_STEPS)) {
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

async function showResults() {
 clearInterval(progressPoller);

 document.getElementById('progressSection').style.display = 'none';
 document.getElementById('resultsSection').style.display = 'block';

 try {
 const response = await fetch(`${API_BASE}/results/${currentJobId}`);
 if (!response.ok) throw new Error('Failed to fetch results');

 const results = await response.json();
 const summaryContainer = document.getElementById('summaryContent');
 const summaryText = results.summary || 'No summary available';

 marked.setOptions({ breaks: true, gfm: true });
 summaryContainer.innerHTML = marked.parse(summaryText);
 summaryContainer.classList.add('markdown-body');
 } catch (error) {
 document.getElementById('summaryContent').innerHTML =
 '<p style="color: red;">Error loading summary. Please try again.</p>';
 }
}

function showError(message) {
 document.getElementById('inputSection').style.display = 'none';
 document.getElementById('progressSection').style.display = 'none';
 document.getElementById('resultsSection').style.display = 'none';
 document.getElementById('errorSection').style.display = 'block';
 document.getElementById('errorMessage').textContent = message;
}

function resetForm() {
 currentJobId = null;
 if (progressPoller) clearInterval(progressPoller);

 document.getElementById('videoFile').value = '';
 document.getElementById('fileInfo').style.display = 'none';
 document.getElementById('inputSection').style.display = 'block';
 document.getElementById('progressSection').style.display = 'none';
 document.getElementById('resultsSection').style.display = 'none';
 document.getElementById('errorSection').style.display = 'none';

 document.getElementById('videoFile').focus();
 window.scrollTo(0, 0);
 validateInputs();
}

function goToDashboard() {
 resetForm();
}

function downloadSummary() {
 if (currentJobId) {
 window.location.href = `/download/summary/${currentJobId}`;
 }
}

window.addEventListener('load', () => {
 initializeDragDrop();
 document.getElementById('videoFile').addEventListener('change', validateInputs);
 validateInputs();
});
