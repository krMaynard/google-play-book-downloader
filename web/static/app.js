/**
 * Play Books Downloader — frontend application.
 */

const API = '';
let settings = { output_dir: '', pdf_available: false };
let currentBook = null;
let pollTimer = null;

// --- helpers ---------------------------------------------------------------
function el(id) { return document.getElementById(id); }

function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
}

function formatETA(seconds) {
    if (seconds == null || seconds < 0) return '';
    if (seconds < 60) return `${seconds}s`;
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    if (mins < 60) return secs > 0 ? `${mins}m ${secs}s` : `${mins}m`;
    const hours = Math.floor(mins / 60);
    return `${hours}h ${mins % 60}m`;
}

// --- session / auth --------------------------------------------------------
async function checkAuth() {
    try {
        const res = await fetch(`${API}/api/status`);
        const data = await res.json();
        const wrap = el('auth-status');
        const dot = wrap.querySelector('.status-dot');
        const text = wrap.querySelector('.status-text');
        const btn = el('session-btn');

        if (data.valid) {
            text.textContent = 'Session set';
            dot.className = 'status-dot w-2 h-2 rounded-full bg-play-green';
            wrap.className = 'flex items-center gap-2 text-sm text-play-green';
            btn.textContent = 'Update Session';
        } else {
            text.textContent = 'No session';
            dot.className = 'status-dot w-2 h-2 rounded-full bg-play-yellow';
            wrap.className = 'flex items-center gap-2 text-sm text-amber-600';
            btn.textContent = 'Set Session';
        }
    } catch (err) {
        console.error('Auth check failed:', err);
    }
}

async function loadSettings() {
    try {
        const res = await fetch(`${API}/api/settings`);
        settings = await res.json();
    } catch (err) {
        console.error('Failed to load settings:', err);
    }
}

function showSessionModal() {
    el('session-modal').classList.remove('hidden');
    el('curl-input').value = '';
    el('curl-error').classList.add('hidden');
    document.body.style.overflow = 'hidden';
    el('curl-input').focus();
}

function hideSessionModal() {
    el('session-modal').classList.add('hidden');
    document.body.style.overflow = '';
}

async function saveSession() {
    const curl = el('curl-input').value.trim();
    const errorEl = el('curl-error');

    if (!curl) {
        errorEl.textContent = 'Please paste your cURL command.';
        errorEl.classList.remove('hidden');
        return;
    }

    try {
        const res = await fetch(`${API}/api/curl`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ curl })
        });
        const data = await res.json();
        if (data.error) {
            errorEl.textContent = data.error;
            errorEl.classList.remove('hidden');
            return;
        }
        hideSessionModal();
        checkAuth();
    } catch (err) {
        errorEl.textContent = 'Failed to save session.';
        errorEl.classList.remove('hidden');
    }
}

// --- fetch book ------------------------------------------------------------
async function fetchBook(e) {
    if (e) e.preventDefault();
    const input = el('book-input').value.trim();
    const errorEl = el('fetch-error');
    const btn = el('fetch-btn');

    errorEl.classList.add('hidden');
    if (!input) {
        errorEl.textContent = 'Enter a book ID or reader link.';
        errorEl.classList.remove('hidden');
        return;
    }

    btn.disabled = true;
    btn.querySelector('.fetch-label').classList.add('hidden');
    btn.querySelector('.fetch-spinner').classList.remove('hidden');

    try {
        const res = await fetch(`${API}/api/book/${encodeURIComponent(input)}`);
        const data = await res.json();

        if (data.error) {
            errorEl.textContent = data.error;
            errorEl.classList.remove('hidden');
            el('book-card').classList.add('hidden');
            return;
        }

        currentBook = data;
        renderBookCard(data);
        el('hint').classList.add('hidden');
    } catch (err) {
        errorEl.textContent = 'Failed to fetch book. Check your session and try again.';
        errorEl.classList.remove('hidden');
    } finally {
        btn.disabled = false;
        btn.querySelector('.fetch-label').classList.remove('hidden');
        btn.querySelector('.fetch-spinner').classList.add('hidden');
    }
}

function metaChip(label, value) {
    if (!value && value !== 0) return '';
    return `<span class="inline-flex items-center gap-1 px-2.5 py-1 bg-surface-100 rounded-full text-xs font-medium text-zinc-600">${escapeHtml(label)}: ${escapeHtml(value)}</span>`;
}

function renderBookCard(book) {
    const card = el('book-card');
    const pdfDisabled = !settings.pdf_available;

    const previewBanner = book.is_full ? '' : `
        <div class="flex items-start gap-2 px-4 py-3 bg-amber-50 border border-amber-200 rounded-lg text-sm text-amber-800 mb-4">
            <svg class="w-5 h-5 flex-shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
            <span>This book reports preview mode <b>“${escapeHtml(book.preview || 'unknown')}”</b> (expected “full”). You may not own it on this account, or your session may be expired.</span>
        </div>`;

    const missingNote = book.missing_pages > 0
        ? `<span class="text-play-red">${book.missing_pages} pages have no download link</span>`
        : '';

    card.innerHTML = `
    <article class="bg-white rounded-2xl border border-zinc-200 shadow-card overflow-hidden animate-slide-up">
        <div class="p-6 flex gap-5">
            <img src="${escapeHtml(book.cover_url)}" alt="Cover"
                 class="w-24 sm:w-28 rounded-lg shadow-card object-cover bg-surface-100 flex-shrink-0"
                 onerror="this.style.visibility='hidden'">
            <div class="flex-1 min-w-0">
                <h2 class="text-xl font-semibold text-zinc-900 leading-snug">${escapeHtml(book.title || book.id)}</h2>
                ${book.authors ? `<p class="text-zinc-500 mt-1">${escapeHtml(book.authors)}</p>` : ''}
                <div class="flex flex-wrap gap-2 mt-3">
                    ${metaChip('Pages', book.num_pages)}
                    ${metaChip('Publisher', book.publisher)}
                    ${metaChip('Published', book.pub_date)}
                    ${metaChip('Language', book.language)}
                </div>
                <p class="mt-3 text-xs font-mono text-zinc-400">${escapeHtml(book.id)}</p>
            </div>
        </div>

        <div class="px-6 pb-6">
            ${previewBanner}

            <div class="flex flex-col sm:flex-row sm:items-center gap-4 mb-4">
                <label class="flex items-center gap-2.5 text-sm text-zinc-700 ${pdfDisabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}"
                       title="${pdfDisabled ? 'Install img2pdf and pikepdf to enable PDF building' : 'Build a PDF with metadata and table of contents after download'}">
                    <input type="checkbox" id="build-pdf" class="w-4 h-4 rounded" ${pdfDisabled ? 'disabled' : ''}>
                    <span>Build PDF after download ${pdfDisabled ? '<span class="text-xs text-zinc-400">(needs img2pdf + pikepdf)</span>' : ''}</span>
                </label>
                ${missingNote ? `<span class="text-xs">${missingNote}</span>` : ''}
            </div>

            <div class="flex items-center gap-3">
                <button id="download-btn" class="flex-1 sm:flex-none px-6 py-3 bg-play-blue hover:bg-play-blue-dark text-white rounded-xl font-medium transition-colors duration-150 flex items-center justify-center gap-2">
                    <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/></svg>
                    Download pages
                </button>
                <button id="cancel-btn" class="hidden px-6 py-3 text-zinc-600 border border-zinc-200 hover:bg-zinc-50 rounded-xl font-medium transition-colors duration-150">Cancel</button>
            </div>

            <!-- Progress -->
            <div id="progress-section" class="hidden mt-5">
                <div class="flex items-center justify-between text-sm mb-2">
                    <span id="progress-status" class="text-zinc-600">Starting…</span>
                    <span id="progress-percent" class="font-medium text-play-blue tabular-nums">0%</span>
                </div>
                <div class="h-2.5 bg-surface-200 rounded-full overflow-hidden">
                    <div class="progress-fill h-full rounded-full" style="width: 0%"></div>
                </div>
            </div>

            <!-- Result -->
            <div id="result-section" class="hidden mt-5"></div>
        </div>
    </article>`;

    card.classList.remove('hidden');

    el('download-btn').addEventListener('click', startDownload);
    el('cancel-btn').addEventListener('click', cancelDownload);
}

// --- download + progress ---------------------------------------------------
async function startDownload() {
    if (!currentBook) return;

    const buildPdf = !!(el('build-pdf') && el('build-pdf').checked);

    el('download-btn').classList.add('hidden');
    el('cancel-btn').classList.remove('hidden');
    el('cancel-btn').disabled = false;
    el('progress-section').classList.remove('hidden');
    el('result-section').classList.add('hidden');
    el('progress-status').textContent = 'Starting…';
    el('progress-percent').textContent = '0%';
    el('book-card').querySelector('.progress-fill').style.width = '0%';

    try {
        const res = await fetch(`${API}/api/download`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ book_id: currentBook.id, build_pdf: buildPdf })
        });
        const data = await res.json();
        if (data.error) {
            failDownload(data.error);
            return;
        }
        pollProgress();
    } catch (err) {
        failDownload('Failed to start download.');
    }
}

async function cancelDownload() {
    el('cancel-btn').disabled = true;
    el('cancel-btn').textContent = 'Cancelling…';
    try {
        await fetch(`${API}/api/cancel`, { method: 'POST' });
    } catch (err) {
        console.error('Cancel failed:', err);
    }
}

function restoreDownloadButton() {
    el('download-btn').classList.remove('hidden');
    const cancel = el('cancel-btn');
    cancel.classList.add('hidden');
    cancel.disabled = false;
    cancel.textContent = 'Cancel';
}

function failDownload(message) {
    restoreDownloadButton();
    el('progress-status').textContent = `Error: ${message}`;
    el('progress-status').classList.add('text-play-red');
}

async function pollProgress() {
    try {
        const res = await fetch(`${API}/api/progress`);
        const data = await res.json();

        const statusEl = el('progress-status');
        const percentEl = el('progress-percent');
        const fill = el('book-card').querySelector('.progress-fill');

        if (typeof data.percentage === 'number') {
            fill.style.width = `${data.percentage}%`;
            percentEl.textContent = `${data.percentage}%`;
        }

        const details = [];
        if (data.message) details.push(data.message);
        if (data.eta_seconds) details.push(`~${formatETA(data.eta_seconds)} left`);
        if (data.failed_pages) details.push(`${data.failed_pages} failed`);

        if (data.status === 'completed') {
            statusEl.classList.remove('text-play-red');
            statusEl.textContent = 'Done';
            restoreDownloadButton();
            renderResult(data);
            return;
        }
        if (data.status === 'error') {
            failDownload(data.error || 'Unknown error');
            return;
        }
        if (data.status === 'cancelled') {
            restoreDownloadButton();
            statusEl.textContent = 'Cancelled';
            el('progress-section').classList.add('hidden');
            return;
        }

        if (data.status === 'building_pdf') {
            statusEl.textContent = data.message || 'Building PDF…';
        } else {
            statusEl.textContent = details.join(' · ') || 'Working…';
        }
        pollTimer = setTimeout(pollProgress, 600);
    } catch (err) {
        pollTimer = setTimeout(pollProgress, 1200);
    }
}

function renderResult(data) {
    el('progress-section').classList.add('hidden');
    const section = el('result-section');

    const rows = [];
    if (data.book_dir) rows.push(fileRow('Pages folder', data.book_dir));
    if (data.pdf) rows.push(fileRow('PDF', data.pdf));

    const failedNote = data.failed_pages
        ? `<p class="text-sm text-amber-600 mt-3">${data.failed_pages} of ${data.total_pages} pages could not be downloaded.</p>`
        : '';

    section.innerHTML = `
        <div class="flex items-center gap-2 text-play-green font-medium mb-3">
            <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>
            Downloaded ${escapeHtml(data.downloaded_pages ?? '')} of ${escapeHtml(data.total_pages ?? '')} pages
        </div>
        <div class="space-y-2">${rows.join('')}</div>
        ${failedNote}
    `;
    section.classList.remove('hidden');

    section.querySelectorAll('[data-reveal]').forEach(btn => {
        btn.addEventListener('click', () => revealFile(btn.getAttribute('data-reveal')));
    });
}

function fileRow(label, path) {
    return `
        <div class="flex items-center gap-3 px-4 py-3 bg-surface-50 rounded-lg text-sm">
            <span class="font-medium text-zinc-700 min-w-[90px]">${escapeHtml(label)}</span>
            <span class="flex-1 font-mono text-xs text-zinc-500 truncate" title="${escapeHtml(path)}">${escapeHtml(path)}</span>
            <button data-reveal="${escapeHtml(path)}" class="px-2.5 py-1 text-xs font-medium text-play-blue hover:bg-play-blue-light rounded transition-colors">Reveal</button>
        </div>`;
}

async function revealFile(path) {
    try {
        const res = await fetch(`${API}/api/reveal`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path })
        });
        const data = await res.json();
        if (data.error) console.error('Reveal failed:', data.error);
    } catch (err) {
        console.error('Reveal request failed:', err);
    }
}

// --- wiring ----------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
    checkAuth();
    loadSettings();

    el('fetch-form').addEventListener('submit', fetchBook);
    el('session-btn').addEventListener('click', showSessionModal);
    el('cancel-session-btn').addEventListener('click', hideSessionModal);
    el('save-session-btn').addEventListener('click', saveSession);
    document.querySelector('#session-modal .modal-backdrop').addEventListener('click', hideSessionModal);

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && !el('session-modal').classList.contains('hidden')) {
            hideSessionModal();
        }
    });
});
