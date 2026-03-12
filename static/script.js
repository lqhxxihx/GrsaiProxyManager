/* ─────────────────────────────────────────────────────────
   Nano Banana AI Drawing UI  –  script.js
   ───────────────────────────────────────────────────────── */

const BASE_URL = 'http://127.0.0.1:1515';
const LS_KEY   = 'nb_draw_results';

// ── DOM refs ──────────────────────────────────────────────
const modelSelect    = document.getElementById('modelSelect');
const promptInput    = document.getElementById('promptInput');
const refImages      = document.getElementById('refImages');
const thumbsRow      = document.getElementById('thumbsRow');
const uploadLabel    = document.getElementById('uploadLabel');
const aspectRatio    = document.getElementById('aspectRatio');
const imageSize      = document.getElementById('imageSize');
const batchCount     = document.getElementById('batchCount');
const saveDuration   = document.getElementById('saveDuration');
const autoRetry      = document.getElementById('autoRetry');
const generateBtn    = document.getElementById('generateBtn');
const resultsGrid    = document.getElementById('resultsGrid');
const emptyState     = document.getElementById('emptyState');
const clearAllBtn    = document.getElementById('clearAllBtn');
const creditsBadge   = document.getElementById('creditsBadge');
const lightbox       = document.getElementById('lightbox');
const lightboxImg    = document.getElementById('lightboxImg');
const lightboxClose  = document.getElementById('lightboxClose');
const apiBaseUrlInput = document.getElementById('apiBaseUrl');
const apiKeyInput     = document.getElementById('apiKey');

// 从 localStorage 恢复代理地址和 Key
if (localStorage.getItem('nb_base_url')) apiBaseUrlInput.value = localStorage.getItem('nb_base_url');
if (localStorage.getItem('nb_api_key')) apiKeyInput.value = localStorage.getItem('nb_api_key');

// 保存到 localStorage
apiBaseUrlInput.addEventListener('change', function() { localStorage.setItem('nb_base_url', this.value.trim()); fetchCredits(); });
apiKeyInput.addEventListener('change', function() { localStorage.setItem('nb_api_key', this.value.trim()); fetchCredits(); });

function getBaseUrl() { return (apiBaseUrlInput.value.trim() || BASE_URL).replace(/\/$/, ''); }
function getApiKey() { return apiKeyInput.value.trim(); }

// ── State ─────────────────────────────────────────────────
let base64Refs = [];   // array of data-URL strings from file input

// ── Helpers ───────────────────────────────────────────────
function getExpiryMs() {
  const v = saveDuration.value;
  if (v === 'permanent') return -1;
  if (v === '2h') return 2 * 3600 * 1000;
  return 1 * 3600 * 1000;  // default 1h
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload  = e => resolve(e.target.result);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── localStorage persistence ──────────────────────────────
function loadSaved() {
  let items;
  try { items = JSON.parse(localStorage.getItem(LS_KEY) || '[]'); }
  catch { items = []; }
  const now = Date.now();
  const valid = items.filter(it => it.expiry === -1 || it.expiry > now);
  if (valid.length !== items.length) {
    localStorage.setItem(LS_KEY, JSON.stringify(valid));
  }
  return valid;
}

function saveItem(item) {
  const items = loadSaved();
  items.unshift(item);
  // keep max 200 items
  if (items.length > 200) items.length = 200;
  try { localStorage.setItem(LS_KEY, JSON.stringify(items)); } catch(e) {}
}

function removeItem(id) {
  const items = loadSaved().filter(it => it.id !== id);
  try { localStorage.setItem(LS_KEY, JSON.stringify(items)); } catch(e) {}
}

function clearAllItems() {
  localStorage.removeItem(LS_KEY);
}

// ── Empty state toggle ────────────────────────────────────
function refreshEmpty() {
  const cards = resultsGrid.querySelectorAll('.card');
  emptyState.style.display = cards.length === 0 ? '' : 'none';
}

// ── Lightbox ──────────────────────────────────────────────
lightboxClose.addEventListener('click', () => lightbox.classList.remove('active'));
lightbox.addEventListener('click', e => { if (e.target === lightbox) lightbox.classList.remove('active'); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') lightbox.classList.remove('active'); });

function openLightbox(url) {
  lightboxImg.src = url;
  lightbox.classList.add('active');
}

// ── Download ──────────────────────────────────────────────
async function downloadImage(url, prompt) {
  try {
    const resp = await fetch(url);
    const blob = await resp.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    const safeName = (prompt || 'image').slice(0, 40).replace(/[^a-zA-Z0-9\u4e00-\u9fa5_-]/g, '_');
    a.download = safeName + '_' + Date.now() + '.png';
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
  } catch(e) {
    alert('下载失败: ' + e.message);
  }
}

// ── Card builder ──────────────────────────────────────────
function makeCard(savedItem) {
  // savedItem: { id, url, prompt, model, timestamp, expiry }
  const card = document.createElement('div');
  card.className = 'card';
  card.dataset.itemId = savedItem.id;

  // image wrap
  const wrap = document.createElement('div');
  wrap.className = 'card-img-wrap';

  const img = document.createElement('img');
  img.src = savedItem.url;
  img.alt = savedItem.prompt || '';
  img.loading = 'lazy';
  img.addEventListener('click', () => openLightbox(savedItem.url));
  wrap.appendChild(img);

  // meta
  const meta = document.createElement('div');
  meta.className = 'card-meta';
  meta.textContent = savedItem.prompt || '(no prompt)';

  // toolbar
  const toolbar = document.createElement('div');
  toolbar.className = 'card-toolbar';

  const dlBtn = document.createElement('button');
  dlBtn.className = 'btn-dl';
  dlBtn.textContent = '⬇';
  dlBtn.title = '下载';
  dlBtn.addEventListener('click', () => downloadImage(savedItem.url, savedItem.prompt));

  const delBtn = document.createElement('button');
  delBtn.className = 'btn-del';
  delBtn.textContent = '✕';
  delBtn.title = '删除';
  delBtn.addEventListener('click', () => {
    removeItem(savedItem.id);
    card.remove();
    refreshEmpty();
  });

  const regenBtn = document.createElement('button');
  regenBtn.className = 'btn-regen';
  regenBtn.textContent = '⟳';
  regenBtn.title = '重新生成';
  regenBtn.addEventListener('click', () => {
    spawnGenerationCard({
      model: savedItem.model,
      prompt: savedItem.prompt,
      aspectRatio: savedItem.aspectRatio || 'auto',
      imageSize: savedItem.imageSize || '4K',
      urls: savedItem.urls || undefined,
    });
  });

  toolbar.append(dlBtn, delBtn, regenBtn);
  card.append(wrap, meta, toolbar);
  return card;
}

// ── Pending card (loading/error) ──────────────────────────
function spawnGenerationCard(reqData) {
  const card = document.createElement('div');
  card.className = 'card';

  const wrap = document.createElement('div');
  wrap.className = 'card-img-wrap';

  // overlay (loading state)
  const overlay = document.createElement('div');
  overlay.className = 'card-overlay';

  const spinner = document.createElement('div');
  spinner.className = 'spinner';

  const progressWrap = document.createElement('div');
  progressWrap.className = 'progress-wrap';
  const progressBar = document.createElement('div');
  progressBar.className = 'progress-bar';
  progressWrap.appendChild(progressBar);

  const progressLabel = document.createElement('div');
  progressLabel.className = 'progress-label';
  progressLabel.textContent = '准备中...';

  overlay.append(spinner, progressWrap, progressLabel);
  wrap.appendChild(overlay);

  // placeholder image (hidden until loaded)
  const img = document.createElement('img');
  img.style.display = 'none';
  wrap.appendChild(img);

  const meta = document.createElement('div');
  meta.className = 'card-meta';
  meta.textContent = reqData.prompt || '(no prompt)';

  const toolbar = document.createElement('div');
  toolbar.className = 'card-toolbar';

  const dlBtn    = document.createElement('button');
  dlBtn.className = 'btn-dl';    dlBtn.textContent = '⬇'; dlBtn.disabled = true;
  const delBtn   = document.createElement('button');
  delBtn.className = 'btn-del';  delBtn.textContent = '✕';
  const regenBtn = document.createElement('button');
  regenBtn.className = 'btn-regen'; regenBtn.textContent = '⟳'; regenBtn.disabled = true;

  delBtn.addEventListener('click', () => { card.remove(); refreshEmpty(); });

  toolbar.append(dlBtn, delBtn, regenBtn);
  card.append(wrap, meta, toolbar);

  // insert at top
  resultsGrid.insertBefore(card, resultsGrid.firstChild);
  refreshEmpty();

  // ── fetch ──
  runGeneration(reqData, {
    onProgress(pct, label) {
      progressBar.style.width = pct + '%';
      progressLabel.textContent = label;
    },
    onSuccess(url) {
      // hide overlay, show image
      overlay.classList.add('hidden');
      img.src = url;
      img.style.display = '';
      img.addEventListener('click', () => openLightbox(url));

      // enable buttons
      dlBtn.disabled = false;
      regenBtn.disabled = false;
      dlBtn.addEventListener('click', () => downloadImage(url, reqData.prompt));
      regenBtn.addEventListener('click', () => spawnGenerationCard(reqData));

      // persist
      const id = 'nb_' + Date.now() + '_' + Math.random().toString(36).slice(2, 7);
      card.dataset.itemId = id;
      const expiry = getExpiryMs();
      const expTs  = expiry === -1 ? -1 : Date.now() + expiry;
      saveItem({
        id, url,
        prompt: reqData.prompt,
        model: reqData.model,
        aspectRatio: reqData.aspectRatio,
        imageSize: reqData.imageSize,
        urls: reqData.urls,
        timestamp: Date.now(),
        expiry: expTs,
      });
    },
    onError(msg) {
      // replace overlay with error state
      overlay.innerHTML = '';
      const errIcon = document.createElement('div');
      errIcon.className = 'error-icon'; errIcon.textContent = '✕';
      const errMsg = document.createElement('div');
      errMsg.className = 'error-msg'; errMsg.textContent = msg;
      const retryBtn = document.createElement('button');
      retryBtn.className = 'error-retry-btn'; retryBtn.textContent = '重试';
      retryBtn.addEventListener('click', () => {
        // reset overlay
        overlay.innerHTML = '';
        overlay.append(spinner.cloneNode(), progressWrap.cloneNode(true), progressLabel.cloneNode(true));
        overlay.classList.remove('hidden');
        const pb2 = overlay.querySelector('.progress-bar');
        const pl2 = overlay.querySelector('.progress-label');
        runGeneration(reqData, {
          onProgress(pct, label) { pb2.style.width = pct + '%'; pl2.textContent = label; },
          onSuccess(url) {
            overlay.classList.add('hidden');
            img.src = url; img.style.display = '';
            img.onclick = () => openLightbox(url);
            dlBtn.disabled = false; regenBtn.disabled = false;
            dlBtn.onclick   = () => downloadImage(url, reqData.prompt);
            regenBtn.onclick = () => spawnGenerationCard(reqData);
            const id = 'nb_' + Date.now() + '_' + Math.random().toString(36).slice(2, 7);
            card.dataset.itemId = id;
            const expiry = getExpiryMs();
            saveItem({ id, url, prompt: reqData.prompt, model: reqData.model,
              aspectRatio: reqData.aspectRatio, imageSize: reqData.imageSize,
              urls: reqData.urls, timestamp: Date.now(),
              expiry: expiry === -1 ? -1 : Date.now() + expiry });
          },
          onError(msg2) {
            pl2.textContent = '失败: ' + msg2;
            if (autoRetry.checked) {
              setTimeout(() => retryBtn.click(), 3000);
            }
          },
        });
      });
      overlay.append(errIcon, errMsg, retryBtn);
      if (autoRetry.checked) {
        setTimeout(() => retryBtn.click(), 3000);
      }
    },
  });
}

// ── Core generation fetch ─────────────────────────────────
async function runGeneration(reqData, { onProgress, onSuccess, onError }) {
  try {
    onProgress(5, '请求中...');
    const resp = await fetch(getBaseUrl() + '/v1/draw/nano-banana', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + (getApiKey() || 'any') },
      body: JSON.stringify({
        model:       reqData.model,
        prompt:      reqData.prompt,
        aspectRatio: reqData.aspectRatio,
        imageSize:   reqData.imageSize,
        urls:        reqData.urls,
      }),
    });

    if (!resp.ok) {
      const txt = await resp.text();
      throw new Error('HTTP ' + resp.status + ': ' + txt.slice(0, 120));
    }

    const ct = resp.headers.get('content-type') || '';

    if (ct.includes('application/json')) {
      // non-streaming
      const data = await resp.json();
      handleResult(data, onProgress, onSuccess, onError);
      return;
    }

    // SSE streaming
    onProgress(10, '生成中...');
    const reader  = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let lastResult = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();  // keep incomplete line

      for (const line of lines) {
        const t = line.trim();
        if (!t.startsWith('data:')) continue;
        const payload = t.slice(5).trim();
        if (payload === '[DONE]') continue;
        try {
          const parsed = JSON.parse(payload);
          if (typeof parsed.progress === 'number') {
            onProgress(parsed.progress, '生成中... ' + parsed.progress + '%');
          }
          if (parsed.status === 'succeeded' || parsed.status === 'failed' || parsed.error) {
            lastResult = parsed;
          }
        } catch { /* ignore malformed lines */ }
      }
    }

    if (lastResult) {
      handleResult(lastResult, onProgress, onSuccess, onError);
    } else {
      throw new Error('未收到有效结果');
    }

  } catch (err) {
    onError(err.message || String(err));
  }
}

function handleResult(data, onProgress, onSuccess, onError) {
  if (data.status === 'succeeded' && data.results && data.results.length > 0) {
    onProgress(100, '完成');
    onSuccess(data.results[0].url);
  } else if (data.status === 'failed') {
    const reasonMap = {
      output_moderation: '内容违规（输出）',
      input_moderation:  '内容违规（输入）',
      error:             '服务错误',
    };
    const r = reasonMap[data.failure_reason] || data.failure_reason || '未知错误';
    onError(r + (data.error ? ' – ' + data.error : ''));
  } else if (data.error) {
    onError(String(data.error));
  } else {
    onError('未知状态: ' + (data.status || JSON.stringify(data).slice(0, 80)));
  }
}

// ── Reference image upload ────────────────────────────────
refImages.addEventListener('change', async () => {
  const files = Array.from(refImages.files);
  thumbsRow.innerHTML = '';
  base64Refs = [];
  if (!files.length) { uploadLabel.textContent = '点击或拖拽上传参考图'; return; }

  generateBtn.disabled = true;
  uploadLabel.textContent = '处理中...';

  for (const f of files) {
    const b64 = await fileToBase64(f);
    base64Refs.push(b64);
    const img = document.createElement('img');
    img.src = b64; img.className = 'ref-thumb';
    thumbsRow.appendChild(img);
  }

  uploadLabel.textContent = files.length + ' 张图片已选择';
  generateBtn.disabled = false;
});

// ── Generate button ───────────────────────────────────────
generateBtn.addEventListener('click', async () => {
  const prompt = promptInput.value.trim();
  if (!prompt) { alert('请输入提示词'); return; }

  const apiKey = getApiKey();
  if (!apiKey) { alert('请输入 API Key'); return; }

  // 验证 API Key
  try {
    const verifyResp = await fetch('/admin/verify-key', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: apiKey })
    });
    if (!verifyResp.ok) {
      alert('API Key 错误，请检查后重试');
      return;
    }
  } catch(e) {
    alert('验证失败: ' + e.message);
    return;
  }

  const count = Math.max(1, Math.min(10, parseInt(batchCount.value) || 1));
  const reqData = {
    model:       modelSelect.value,
    prompt,
    aspectRatio: aspectRatio.value,
    imageSize:   imageSize.value,
    urls:        base64Refs.length > 0 ? base64Refs : undefined,
  };

  for (let i = 0; i < count; i++) spawnGenerationCard(reqData);
});

// ── Clear all ─────────────────────────────────────────────
clearAllBtn.addEventListener('click', () => {
  if (!confirm('确认清空所有生成结果？')) return;

  resultsGrid.querySelectorAll('.card').forEach(c => c.remove());
  clearAllItems();
  refreshEmpty();
});

// ── Credits polling ───────────────────────────────────────
async function fetchCredits() {
  const apiKey = getApiKey();
  if (!apiKey) { creditsBadge.style.display = 'none'; return; }
  try {
    const resp = await fetch('/admin/credits-summary', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: apiKey })
    });
    if (!resp.ok) {
      creditsBadge.style.display = '';
      creditsBadge.textContent = 'Key 错误';
      creditsBadge.style.color = '#f87171';
      return;
    }
    creditsBadge.style.color = '';
    const data = await resp.json();
    creditsBadge.style.display = '';
    creditsBadge.textContent = '积分: ' + (data.total_credits || 0).toLocaleString() + ' (' + (data.active_keys || 0) + ' Keys)';
  } catch { /* ignore */ }
}

fetchCredits();
setInterval(fetchCredits, 30000);

// ── Restore saved results on load ─────────────────────────
(function restoreFromStorage() {
  const items = loadSaved();
  // items are newest-first already (we unshift on save)
  // render in reverse so newest ends up at top after insertBefore
  for (let i = items.length - 1; i >= 0; i--) {
    const card = makeCard(items[i]);
    resultsGrid.insertBefore(card, resultsGrid.firstChild);
  }
  refreshEmpty();
})();
