'use strict';

const chat          = document.getElementById('chat');
const form          = document.getElementById('composer');
const input         = document.getElementById('input');
const sendBtn       = document.getElementById('send-btn');
const modelSel      = document.getElementById('model-select');
const convList      = document.getElementById('conv-list');
const newChatBtn    = document.getElementById('new-chat-btn');
const sidebarToggle = document.getElementById('sidebar-toggle');
const sidebar       = document.getElementById('sidebar');
const docList           = document.getElementById('doc-list');
const docUpload         = document.getElementById('doc-upload');
const chatDocList       = document.getElementById('chat-doc-list');
const chatDocUpload     = document.getElementById('chat-doc-upload');
const chatDocsDivider   = document.getElementById('chat-docs-divider');
const chatDocsHeader    = document.getElementById('chat-docs-header');
const gpuMeter      = document.getElementById('gpu-meter');
const gpuPct        = document.getElementById('gpu-pct');
const gpuUtilBar    = document.getElementById('gpu-util-bar');
const gpuVram       = document.getElementById('gpu-vram');

let currentConvId = null;
let abortController = null;

// ── Sidebar toggle ────────────────────────────────────────────
sidebarToggle.addEventListener('click', () => {
  sidebar.classList.toggle('collapsed');
});

// ── Auto-resize textarea ─────────────────────────────────────
input.addEventListener('input', () => {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 200) + 'px';
});

input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    form.requestSubmit();
  }
});

// ── DOM helpers ──────────────────────────────────────────────
function scrollToBottom() {
  // Only auto-scroll if the user is already near the bottom (within 80px).
  // If they've scrolled up to read, leave them there.
  const distanceFromBottom = chat.scrollHeight - chat.scrollTop - chat.clientHeight;
  if (distanceFromBottom < 80) {
    chat.scrollTop = chat.scrollHeight;
  }
}

// ── Think section ─────────────────────────────────────────────
function buildThinkBlock(collapsed = false) {
  const block = document.createElement('div');
  block.className = 'think-block' + (collapsed ? ' collapsed' : '');

  const header = document.createElement('div');
  header.className = 'think-header';
  header.innerHTML = '<span class="think-chevron">▾</span><span class="think-label">Thinking…</span>';
  header.addEventListener('click', () => block.classList.toggle('collapsed'));

  const content = document.createElement('div');
  content.className = 'think-content';

  block.appendChild(header);
  block.appendChild(content);
  return { block, content, label: header.querySelector('.think-label') };
}

// Parse <think>...</think> from a completed message string
function parseThink(text) {
  const m = text.match(/^<think>([\s\S]*?)<\/think>\s*/);
  if (m) return { think: m[1].trim(), response: text.slice(m[0].length).trimStart() };
  return { think: null, response: text };
}

function addMeta(inner, modelTag, sources) {
  if (modelTag) {
    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.textContent = modelTag;
    inner.appendChild(meta);
  }
  if (sources && (sources.doc_chunks > 0 || sources.urls.length > 0 || (sources.web_searches && sources.web_searches.length > 0))) {
    const badge = document.createElement('div');
    badge.className = 'context-badge';
    const parts = [];
    if (sources.doc_chunks > 0)
      parts.push(`📄 ${sources.doc_chunks} doc chunk${sources.doc_chunks > 1 ? 's' : ''}`);
    if (sources.urls.length > 0)
      parts.push(`🔗 ${sources.urls.join(', ')}`);
    if (sources.web_searches && sources.web_searches.length > 0)
      parts.push(`🔍 ${sources.web_searches.join(' · ')}`);
    badge.textContent = parts.join(' · ');
    inner.appendChild(badge);
  }
}

function createMessage(role, content, { isError = false, modelTag = '', sources = null } = {}) {
  const wrap = document.createElement('div');
  wrap.className = `message ${role}`;

  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  avatar.textContent = role === 'user' ? 'You' : 'AI';

  const inner = document.createElement('div');

  const bubble = document.createElement('div');
  bubble.className = 'bubble' + (isError ? ' error' : '');
  bubble.textContent = content;
  inner.appendChild(bubble);

  addMeta(inner, modelTag, sources);

  wrap.appendChild(avatar);
  wrap.appendChild(inner);
  chat.appendChild(wrap);
  scrollToBottom();
  return bubble;
}

function createStreamingBubble() {
  const wrap = document.createElement('div');
  wrap.className = 'message ai';

  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  avatar.textContent = 'AI';

  const inner = document.createElement('div');
  const bubble = document.createElement('div');
  bubble.className = 'bubble streaming';

  inner.appendChild(bubble);
  wrap.appendChild(avatar);
  wrap.appendChild(inner);
  chat.appendChild(wrap);
  scrollToBottom();
  return { bubble, inner };
}

// ── Streaming think/response handler ─────────────────────────
function makeThinkParser(inner, responseBubble) {
  let thinkObj = null;
  let thinkDone = false;

  function onThink(content) {
    if (!thinkObj) {
      thinkObj = buildThinkBlock(false);
      inner.insertBefore(thinkObj.block, responseBubble);
    }
    thinkObj.content.textContent += content;
    thinkObj.content.scrollTop = thinkObj.content.scrollHeight;
  }

  function onToken(content) {
    if (thinkObj && !thinkDone) {
      thinkDone = true;
      thinkObj.label.textContent = 'Thinking';
      thinkObj.block.classList.add('collapsed');
    }
    responseBubble.textContent += content;
  }

  return { onThink, onToken };
}

function showThinking() {
  const wrap = document.createElement('div');
  wrap.className = 'message ai';
  wrap.id = 'thinking';

  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  avatar.textContent = 'AI';

  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.innerHTML = '<span class="spinner"></span>Thinking…';

  wrap.appendChild(avatar);
  wrap.appendChild(bubble);
  chat.appendChild(wrap);
  scrollToBottom();
}

function removeThinking() {
  const el = document.getElementById('thinking');
  if (el) el.remove();
}

// ── Conversations ────────────────────────────────────────────
function setActiveConvItem(id) {
  document.querySelectorAll('.conv-item').forEach(el => {
    el.classList.toggle('active', el.dataset.convId === id);
  });
}

async function fetchConversations() {
  try {
    const res = await fetch('/api/conversations');
    if (!res.ok) return;
    renderConvList(await res.json());
  } catch (_) {}
}

function renderConvList(convs) {
  convList.innerHTML = '';
  for (const conv of convs) {
    const item = document.createElement('div');
    item.className = 'conv-item' + (conv.id === currentConvId ? ' active' : '');
    item.dataset.convId = conv.id;
    item.setAttribute('role', 'listitem');

    const title = document.createElement('span');
    title.className = 'conv-item-title';
    title.textContent = conv.title || 'Untitled';
    title.title = conv.title || 'Untitled';

    const del = document.createElement('button');
    del.className = 'conv-item-delete';
    del.textContent = '✕';
    del.title = 'Delete conversation';
    del.addEventListener('click', async (e) => {
      e.stopPropagation();
      await deleteConversation(conv.id);
    });

    item.appendChild(title);
    item.appendChild(del);
    item.addEventListener('click', () => loadConversation(conv.id));
    convList.appendChild(item);
  }
}

async function loadConversation(id) {
  try {
    const res = await fetch(`/api/conversations/${id}`);
    if (!res.ok) return;
    const conv = await res.json();
    currentConvId = id;
    chat.innerHTML = '';
    for (const msg of conv.messages || []) {
      const role = msg.role === 'assistant' ? 'ai' : 'user';
      createMessage(role, msg.content, role === 'ai' ? { modelTag: conv.model } : {});
    }
    setActiveConvItem(id);
    setChatDocsVisible(true);
    await fetchChatDocuments();
    input.focus();
  } catch (_) {}
}

async function deleteConversation(id) {
  try {
    const res = await fetch(`/api/conversations/${id}`, { method: 'DELETE' });
    if (res.status === 204 || res.ok) {
      if (currentConvId === id) newChat();
      await fetchConversations();
    }
  } catch (_) {}
}

function newChat() {
  currentConvId = null;
  chat.innerHTML = '';
  chatDocList.innerHTML = '';
  setActiveConvItem(null);
  setChatDocsVisible(false);
  input.focus();
}

newChatBtn.addEventListener('click', newChat);

// ── Documents ────────────────────────────────────────────────
// ── Documents helpers ─────────────────────────────────────────

function _makeDocItem(doc, onDelete) {
  const item = document.createElement('div');
  item.className = 'doc-item';
  item.dataset.docId = doc.id;

  const name = document.createElement('span');
  name.className = 'doc-item-name';
  name.textContent = doc.filename;
  name.title = `${doc.filename} · ${doc.chunk_count} chunks`;

  const del = document.createElement('button');
  del.className = 'doc-item-delete';
  del.textContent = '✕';
  del.title = 'Delete document';
  del.addEventListener('click', onDelete);

  item.appendChild(name);
  item.appendChild(del);
  return item;
}

async function _uploadDoc(file, listEl, conversationId) {
  const placeholder = document.createElement('div');
  placeholder.className = 'doc-item uploading';
  const pname = document.createElement('span');
  pname.className = 'doc-item-name';
  pname.textContent = `⏳ ${file.name}`;
  placeholder.appendChild(pname);
  listEl.prepend(placeholder);

  try {
    const body = new FormData();
    body.append('file', file);
    const url = conversationId
      ? `/api/documents?conversation_id=${conversationId}`
      : '/api/documents';
    const res = await fetch(url, { method: 'POST', body });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(`Upload failed: ${err.detail || res.status}`);
    }
  } catch (err) {
    alert(`Upload error: ${err.message}`);
  } finally {
    placeholder.remove();
    if (conversationId) await fetchChatDocuments();
    else await fetchDocuments();
  }
}

// ── Global documents ──────────────────────────────────────────

async function fetchDocuments() {
  try {
    const res = await fetch('/api/documents');
    if (!res.ok) return;
    renderDocList(await res.json());
  } catch (_) {}
}

function renderDocList(docs) {
  docList.innerHTML = '';
  for (const doc of docs) {
    docList.appendChild(_makeDocItem(doc, async () => {
      await fetch(`/api/documents/${doc.id}`, { method: 'DELETE' });
      await fetchDocuments();
    }));
  }
}

docUpload.addEventListener('change', async () => {
  const file = docUpload.files[0];
  if (!file) return;
  docUpload.value = '';
  await _uploadDoc(file, docList, null);
});

// ── Chat-scoped documents ─────────────────────────────────────

function setChatDocsVisible(visible) {
  chatDocsDivider.hidden = !visible;
  chatDocsHeader.hidden = !visible;
  chatDocList.hidden = !visible;
}

async function fetchChatDocuments() {
  if (!currentConvId) return;
  try {
    const res = await fetch(`/api/documents?conversation_id=${currentConvId}`);
    if (!res.ok) return;
    renderChatDocList(await res.json());
  } catch (_) {}
}

function renderChatDocList(docs) {
  chatDocList.innerHTML = '';
  for (const doc of docs) {
    chatDocList.appendChild(_makeDocItem(doc, async () => {
      await fetch(`/api/documents/${doc.id}`, { method: 'DELETE' });
      await fetchChatDocuments();
    }));
  }
}

chatDocUpload.addEventListener('change', async () => {
  const file = chatDocUpload.files[0];
  if (!file || !currentConvId) return;
  chatDocUpload.value = '';
  await _uploadDoc(file, chatDocList, currentConvId);
});

// ── Send ─────────────────────────────────────────────────────
form.addEventListener('submit', async (e) => {
  e.preventDefault();

  if (abortController) {
    abortController.abort();
    return;
  }

  const message = input.value.trim();
  if (!message) return;

  const model = modelSel.value;
  input.value = '';
  input.style.height = 'auto';
  sendBtn.textContent = '■ Stop';
  sendBtn.classList.add('stop-mode');

  createMessage('user', message);
  showThinking();

  try {
    abortController = new AbortController();
    const body = { message, model };
    if (currentConvId) body.conversation_id = currentConvId;

    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: abortController.signal,
    });

    removeThinking();

    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try { detail = (await res.json()).detail || detail; } catch (_) {}
      createMessage('ai', `Error: ${detail}`, { isError: true });
      return;
    }

    const { bubble, inner } = createStreamingBubble();
    const { onThink, onToken } = makeThinkParser(inner, bubble);
    let searchBadge = null;
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let sseBuffer = '';
    let doneData = null;

    outer: while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      sseBuffer += decoder.decode(value, { stream: true });
      const lines = sseBuffer.split('\n');
      sseBuffer = lines.pop();

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        let evt;
        try { evt = JSON.parse(line.slice(6)); } catch (_) { continue; }

        if (evt.type === 'search') {
          if (!searchBadge) {
            searchBadge = document.createElement('div');
            searchBadge.className = 'search-badge';
            inner.insertBefore(searchBadge, bubble);
          }
          searchBadge.textContent = `Searching the web: "${evt.query}"…`;
          scrollToBottom();
        } else if (evt.type === 'think') {
          onThink(evt.content);
          scrollToBottom();
        } else if (evt.type === 'token') {
          if (searchBadge) { searchBadge.remove(); searchBadge = null; }
          onToken(evt.content);
          scrollToBottom();
        } else if (evt.type === 'error') {
          bubble.classList.remove('streaming');
          bubble.classList.add('error');
          bubble.textContent = `Error: ${evt.detail}`;
          break outer;
        } else if (evt.type === 'done') {
          doneData = evt;
          break outer;
        }
      }
    }

    bubble.classList.remove('streaming');

    if (doneData) {
      addMeta(inner, doneData.model, doneData.sources);
      if (!currentConvId) {
        currentConvId = doneData.conversation_id;
        await fetchConversations();
        setActiveConvItem(currentConvId);
        setChatDocsVisible(true);
        await fetchChatDocuments();
      }
    }

  } catch (err) {
    removeThinking();
    if (err.name !== 'AbortError') {
      createMessage('ai', `Network error: ${err.message}`, { isError: true });
    }
  } finally {
    abortController = null;
    sendBtn.textContent = 'Send';
    sendBtn.classList.remove('stop-mode');
    input.focus();
  }
});

// ── GPU meter ─────────────────────────────────────────────────
async function pollGpu() {
  try {
    const res = await fetch('/api/gpu');
    if (!res.ok) { gpuMeter.classList.add('unavailable'); return; }
    const d = await res.json();
    if (!d.ok) { gpuMeter.classList.add('unavailable'); return; }

    gpuMeter.classList.remove('unavailable');
    gpuMeter.title = d.name || '';
    gpuPct.textContent = `${d.gpu_util}%`;
    gpuUtilBar.style.width = `${d.gpu_util}%`;
    gpuUtilBar.className = 'gpu-bar' +
      (d.gpu_util > 85 ? ' hot' : d.gpu_util > 55 ? ' warm' : '');
    const used  = (d.mem_used  / 1073741824).toFixed(1);
    const total = (d.mem_total / 1073741824).toFixed(1);
    gpuVram.textContent = `${used}/${total} GB`;
  } catch (_) {
    gpuMeter.classList.add('unavailable');
  }
}

// ── Model list ───────────────────────────────────────────────
async function fetchModels() {
  try {
    const res = await fetch('/api/models');
    if (!res.ok) return;
    const { models } = await res.json();
    const saved = localStorage.getItem('selectedModel');
    modelSel.innerHTML = '';
    for (const name of models) {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      if (name === saved) opt.selected = true;
      modelSel.appendChild(opt);
    }
  } catch (_) {}
}

modelSel.addEventListener('change', () => {
  localStorage.setItem('selectedModel', modelSel.value);
});

// ── Bootstrap ─────────────────────────────────────────────────
fetchModels();
fetchConversations();
fetchDocuments();
pollGpu();
setInterval(pollGpu, 3000);
input.focus();
