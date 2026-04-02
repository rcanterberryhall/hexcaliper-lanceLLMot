'use strict';

const chat          = document.getElementById('chat');
const form          = document.getElementById('composer');
const input         = document.getElementById('input');
const sendBtn       = document.getElementById('send-btn');
const modelSel      = document.getElementById('model-select');
const analysisSel   = document.getElementById('analysis-select');
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
const gpuMeters     = document.getElementById('gpu-meters');
const modelDot        = document.getElementById('model-dot');
const modelDotLabel   = document.getElementById('model-dot-label');
const loadModelBtn    = document.getElementById('load-model-btn');
const analysisDot     = document.getElementById('analysis-dot');
const analysisDotLabel = document.getElementById('analysis-dot-label');
const loadAnalysisBtn  = document.getElementById('load-analysis-btn');
const refreshModelsBtn = document.getElementById('refresh-models-btn');
const errorBar      = document.getElementById('error-bar');
const errorBarText  = document.getElementById('error-bar-text');
const errorBarDismiss = document.getElementById('error-bar-dismiss');
const systemPromptWrap = document.getElementById('system-prompt-wrap');
const systemPromptInput = document.getElementById('system-prompt');
const systemBtn         = document.getElementById('system-btn');
const statusBar         = document.getElementById('status-bar');
const statusSpinner     = document.getElementById('status-spinner');
const statusMsg         = document.getElementById('status-msg');

let currentConvId = null;
let abortController = null;
let uploadInProgress = false;
let gpuFastInterval = null;

// ── Markdown rendering ─────────────────────────────────────────
marked.use({
  gfm: true,     // GitHub-Flavored Markdown (tables, strikethrough, etc.)
  breaks: false, // Require double newline for <br>; keeps prose tighter
});

/**
 * Parse *text* as Markdown, apply syntax highlighting to all fenced code
 * blocks, and return the resulting HTML string.
 *
 * Uses a parse-then-highlight approach rather than a marked renderer hook
 * to avoid marked's unstable renderer token API across versions. marked
 * produces the HTML structure; hljs highlights each ``<pre><code>`` block
 * in a temporary off-screen element before the string is returned.
 *
 * Only called for AI message bubbles — user messages and error messages
 * are always set via ``textContent`` to avoid any injection risk.
 *
 * @param {string} text - Raw markdown text from the model.
 * @return {string} Rendered HTML string with syntax-highlighted code blocks.
 */
function renderMarkdown(text) {
  const tmp = document.createElement('div');
  tmp.innerHTML = marked.parse(text);
  tmp.querySelectorAll('pre code').forEach(block => hljs.highlightElement(block));
  return tmp.innerHTML;
}

// ── Sidebar toggle ────────────────────────────────────────────
sidebarToggle.addEventListener('click', () => {
  sidebar.classList.toggle('collapsed');
});

// ── System prompt toggle ──────────────────────────────────────
systemBtn.addEventListener('click', () => {
  const hidden = systemPromptWrap.hidden;
  systemPromptWrap.hidden = !hidden;
  systemBtn.classList.toggle('active', !hidden === false);
  if (!systemPromptWrap.hidden) systemPromptInput.focus();
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
/**
 * Scrolls the element to the bottom if the user is near the bottom of the content.
 * This function ensures that automatic scrolling occurs only when the user is
 * within 80 pixels of the bottom, allowing them to read content without interruption
 * if they have scrolled up.
 *
 * @return {void} This function does not return any value.
 */
function scrollToBottom() {
  // Only auto-scroll if the user is already near the bottom (within 80px).
  // If they've scrolled up to read, leave them there.
  const distanceFromBottom = chat.scrollHeight - chat.scrollTop - chat.clientHeight;
  if (distanceFromBottom < 80) {
    chat.scrollTop = chat.scrollHeight;
  }
}

/**
 * Appends a "Copy" button to an AI message container.
 *
 * The button is hidden until the parent ``.message.ai`` is hovered.
 * On click, it copies the rendered text of *bubble* to the clipboard and
 * briefly shows a confirmation tick.
 *
 * @param {HTMLElement} inner - The inner message container to append the button to.
 * @param {HTMLElement} bubble - The bubble whose ``innerText`` is copied.
 * @return {void}
 */
/**
 * Appends an "Escalate →" button to an AI message, allowing the user to send
 * the query to a cloud model via the escalation queue.
 */
function addEscalateBtn(inner, convId, queryText, docIds, hasClientDocs) {
  const btn = document.createElement('button');
  btn.className = 'escalate-btn';
  btn.textContent = 'Escalate →';
  btn.title = hasClientDocs
    ? 'Send to cloud model — requires manual approval (client documents in context)'
    : 'Send to cloud model for a second opinion';
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    btn.textContent = 'Queuing…';
    try {
      const res = await fetch('/api/escalation/queue', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          query_text:      queryText,
          source_doc_ids:  docIds,
          has_client_docs: hasClientDocs,
          conversation_id: convId,
        }),
      });
      if (res.ok) {
        const data = await res.json();
        btn.textContent = data.auto_approved ? '✓ Auto-approved' : '✓ Pending approval';
        setStatus(
          data.auto_approved
            ? 'Escalation queued and auto-approved.'
            : 'Escalation queued — awaiting approval in Library → Escalation.',
          'info',
        );
      } else {
        const err = await res.json().catch(() => ({}));
        btn.disabled = false;
        btn.textContent = 'Escalate →';
        setStatus(err.detail || 'Escalation failed.', 'error');
      }
    } catch {
      btn.disabled = false;
      btn.textContent = 'Escalate →';
      setStatus('Escalation failed — network error.', 'error');
    }
  });
  inner.appendChild(btn);
}

function addCopyBtn(inner, bubble) {
  const btn = document.createElement('button');
  btn.className = 'copy-btn';
  btn.textContent = 'Copy';
  btn.title = 'Copy response';
  btn.addEventListener('click', async () => {
    await navigator.clipboard.writeText(bubble.innerText).catch(() => {});
    btn.textContent = 'Copied ✓';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 1800);
  });
  inner.appendChild(btn);
}

// ── Think section ─────────────────────────────────────────────
/**
 * Creates a "Think Block" UI component, which includes a header, a content section,
 * and supports collapsing functionality.
 *
 * @param {boolean} [collapsed=false] - Determines whether the "Think Block" is initially collapsed.
 * @return {Object} An object containing the following properties:
 * - `block` {HTMLElement}: The root element of the "Think Block".
 * - `content` {HTMLElement}: The content element inside the "Think Block".
 * - `label` {HTMLElement}: The label element in the header for updating or accessing its text.
 */
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
/**
 * Parses the input text to extract content within `<think>` tags and separates it from the remaining text.
 *
 * @param {string} text - The input string potentially containing `<think>` tags and additional content.
 * @return {Object} An object containing two properties:
 *                  - `think`: The content within the `<think>` tags, or `null` if no tags are found.
 *                  - `response`: The remaining text after the `<think>` tags are extracted.
 */
function parseThink(text) {
  const m = text.match(/^<think>([\s\S]*?)<\/think>\s*/);
  if (m) return { think: m[1].trim(), response: text.slice(m[0].length).trimStart() };
  return { think: null, response: text };
}

/**
 * Appends metadata elements (model tag and context sources badge) to a message container.
 *
 * Adds a small model label and/or a context-sources badge below the message bubble
 * when the response was grounded by document chunks, fetched URLs, or web searches.
 *
 * @param {HTMLElement} inner - The inner message container element to append metadata to.
 * @param {string} modelTag - The model name string to display; omitted if falsy.
 * @param {Object|null} sources - Sources object from the API ``done`` event, or ``null``.
 * @param {number} sources.doc_chunks - Number of RAG document chunks used.
 * @param {string[]} sources.urls - List of URLs fetched for inline context.
 * @param {string[]} [sources.web_searches] - List of web-search queries performed.
 * @return {void}
 */
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

/**
 * Creates and appends a static chat message bubble to the chat window.
 *
 * Used for user messages, fully-received AI responses, and error messages.
 * For streaming AI responses use {@link createStreamingBubble} instead.
 *
 * @param {string} role - Either ``'user'`` or ``'ai'``; controls CSS class and avatar label.
 * @param {string} content - The plain-text message body.
 * @param {Object} [options={}] - Optional display overrides.
 * @param {boolean} [options.isError=false] - When true, applies the error style to the bubble.
 * @param {string} [options.modelTag=''] - Model name to display as a metadata label.
 * @param {Object|null} [options.sources=null] - Context sources object passed to {@link addMeta}.
 * @return {HTMLElement} The bubble ``<div>`` element that was appended.
 */
function createMessage(role, content, { isError = false, modelTag = '', sources = null } = {}) {
  const wrap = document.createElement('div');
  wrap.className = `message ${role}`;

  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  avatar.textContent = role === 'user' ? 'You' : 'AI';

  const inner = document.createElement('div');

  const bubble = document.createElement('div');
  bubble.className = 'bubble' + (isError ? ' error' : '');
  // Render markdown for AI responses; use textContent for user/error messages
  // to avoid any HTML injection from user-supplied content.
  if (role === 'ai' && !isError) {
    bubble.innerHTML = renderMarkdown(content);
  } else {
    bubble.textContent = content;
  }
  inner.appendChild(bubble);

  addMeta(inner, modelTag, sources);
  if (role === 'ai' && !isError) addCopyBtn(inner, bubble);

  wrap.appendChild(avatar);
  wrap.appendChild(inner);
  chat.appendChild(wrap);
  scrollToBottom();
  return bubble;
}

/**
 * Creates and appends an AI message container ready to receive streamed tokens.
 *
 * The bubble starts with the ``streaming`` CSS class (shows a blinking cursor)
 * which should be removed once the stream is complete.
 *
 * @return {{bubble: HTMLElement, inner: HTMLElement}} An object with the bubble
 *   element (where token text is appended) and its parent inner container
 *   (where think blocks and metadata are inserted).
 */
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
/**
 * Creates a stateful parser that routes streaming tokens to a think block or response bubble.
 *
 * Manages the lifecycle of an inline ``<think>`` block: the block is created on the first
 * think token, and collapsed automatically once the first response token arrives.
 *
 * @param {HTMLElement} inner - The inner message container that holds both the think block
 *   and the response bubble.
 * @param {HTMLElement} responseBubble - The bubble element where response tokens are appended.
 * @return {{onThink: Function, onToken: Function}} An object with two callbacks:
 *   - ``onThink(content)`` — append a think token to the think block.
 *   - ``onToken(content)`` — append a response token to the bubble (collapses the think block
 *     on first call).
 */
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

/**
 * Inserts a temporary "Thinking…" placeholder message in the chat window.
 *
 * The element is given the id ``"thinking"`` so it can be located and removed
 * by {@link removeThinking} once the response stream begins.
 *
 * @return {void}
 */
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

/**
 * Removes the "Thinking…" placeholder from the chat window, if present.
 *
 * Called as soon as the first bytes of the response stream are received
 * so the placeholder is replaced by the streaming bubble.
 *
 * @return {void}
 */
function removeThinking() {
  const el = document.getElementById('thinking');
  if (el) el.remove();
}

// ── Conversations ────────────────────────────────────────────
/**
 * Activates an inline rename editor on a conversation sidebar item.
 *
 * Replaces the title ``<span>`` with a text ``<input>``, commits on Enter or
 * blur, cancels on Escape.  Calls ``PATCH /api/conversations/{id}`` on commit.
 *
 * @param {HTMLElement} titleEl - The ``.conv-item-title`` span to replace.
 * @param {string} convId - The conversation ID to rename.
 * @return {void}
 */
function startRenameConv(titleEl, convId) {
  const prev = titleEl.textContent;
  const input = document.createElement('input');
  input.className = 'conv-item-rename';
  input.value = prev;
  titleEl.replaceWith(input);
  input.focus();
  input.select();

  async function commit() {
    const newTitle = input.value.trim();
    if (newTitle && newTitle !== prev) {
      await fetch(`/api/conversations/${convId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: newTitle }),
      }).catch(() => {});
      titleEl.textContent = newTitle;
      titleEl.title = newTitle;
    }
    input.replaceWith(titleEl);
  }

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter')  { e.preventDefault(); commit(); }
    if (e.key === 'Escape') { input.replaceWith(titleEl); }
  });
  input.addEventListener('blur', commit);
}

/**
 * Highlights the sidebar item that corresponds to the active conversation.
 *
 * Toggles the ``active`` CSS class on all ``.conv-item`` elements, adding it
 * only to the item whose ``data-conv-id`` matches *id*.  Pass ``null`` to
 * clear all highlights (e.g. when starting a new chat).
 *
 * @param {string|null} id - The conversation ID to mark as active, or ``null``.
 * @return {void}
 */
function setActiveConvItem(id) {
  document.querySelectorAll('.conv-item').forEach(el => {
    el.classList.toggle('active', el.dataset.convId === id);
  });
}

/**
 * Fetches the conversation list from the API and renders it in the sidebar.
 *
 * Silently swallows network and HTTP errors so a transient backend hiccup
 * does not crash the UI.
 *
 * @return {Promise<void>}
 */
async function fetchConversations() {
  try {
    const res = await fetch('/api/conversations');
    if (!res.ok) return;
    renderConvList(await res.json());
  } catch (_) {}
}

/**
 * Renders the conversation list into the sidebar.
 *
 * Clears the existing list and rebuilds it from *convs*.  Each item gets a
 * click handler to load the conversation and a delete button to remove it.
 *
 * @param {Array<{id: string, title: string}>} convs - Conversations returned by the API,
 *   newest-first.
 * @return {void}
 */
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
    title.addEventListener('dblclick', (e) => {
      e.stopPropagation();
      startRenameConv(title, conv.id);
    });

    const exportBtn = document.createElement('button');
    exportBtn.className = 'conv-item-export';
    exportBtn.textContent = '⬇';
    exportBtn.title = 'Export conversation';
    exportBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      openExportMenu(conv.id, exportBtn);
    });

    const del = document.createElement('button');
    del.className = 'conv-item-delete';
    del.textContent = '✕';
    del.title = 'Delete conversation';
    del.addEventListener('click', async (e) => {
      e.stopPropagation();
      await deleteConversation(conv.id);
    });

    item.appendChild(title);
    if (conv.system_prompt_id) {
      const badge = document.createElement('span');
      badge.className = 'conv-sp-badge';
      badge.textContent = 'SP';
      badge.title = 'System prompt assigned';
      item.appendChild(badge);
    }
    item.appendChild(exportBtn);
    item.appendChild(del);
    item.addEventListener('click', () => loadConversation(conv.id));
    convList.appendChild(item);
  }
}

/**
 * Loads a conversation from the API and renders its messages in the chat window.
 *
 * Replaces the current chat view with the conversation's message history,
 * marks the sidebar item as active, and fetches any chat-scoped documents.
 *
 * @param {string} id - The ID of the conversation to load.
 * @return {Promise<void>}
 */
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
    syncSpForConversation(conv);
    input.focus();
  } catch (_) {}
}

/**
 * Deletes a conversation via the API and refreshes the sidebar list.
 *
 * If the deleted conversation is the currently active one, resets to a new
 * blank chat via {@link newChat}.
 *
 * @param {string} id - The ID of the conversation to delete.
 * @return {Promise<void>}
 */
async function deleteConversation(id) {
  try {
    const res = await fetch(`/api/conversations/${id}`, { method: 'DELETE' });
    if (res.status === 204 || res.ok) {
      if (currentConvId === id) newChat();
      await fetchConversations();
    }
  } catch (_) {}
}

/**
 * Resets the UI to a blank new-chat state.
 *
 * Clears the chat window, empties the chat-document list, deselects any
 * active sidebar item, hides the chat-docs section, and focuses the input.
 *
 * @return {void}
 */
function newChat() {
  currentConvId = null;
  chat.innerHTML = '';
  chatDocList.innerHTML = '';
  setActiveConvItem(null);
  setChatDocsVisible(false);
  syncSpForConversation(null);
  input.focus();
}

newChatBtn.addEventListener('click', () => {
  newChat();
  // Clear any workbench project context
  delete chatView.dataset.projectId;
  delete chatView.dataset.projectName;
  input.placeholder = 'Ask anything…';
});

// ── Documents ────────────────────────────────────────────────
// ── Documents helpers ─────────────────────────────────────────

/**
 * Builds a single document list-item element with a filename label and delete button.
 *
 * The item is not yet attached to the DOM; the caller appends it to the appropriate list.
 *
 * @param {{id: string, filename: string, chunk_count: number}} doc - Document metadata
 *   from the API.
 * @param {Function} onDelete - Click handler for the delete button.
 * @return {HTMLElement} The assembled ``.doc-item`` div element.
 */
function _makeDocItem(doc, onDelete) {
  const item = document.createElement('div');
  item.className = 'doc-item';
  item.dataset.docId = doc.id;

  // Top row: filename + delete button
  const main = document.createElement('div');
  main.className = 'doc-item-main';

  const name = document.createElement('span');
  name.className = 'doc-item-name';
  name.textContent = doc.filename;
  name.title = `${doc.filename} · ${doc.chunk_count} chunks`;

  const del = document.createElement('button');
  del.className = 'doc-item-delete';
  del.textContent = '✕';
  del.title = 'Delete document';
  del.addEventListener('click', async () => {
    if (!confirm(`Delete "${doc.filename}"?\n\nThis removes it permanently from the knowledge graph.`)) return;
    await onDelete();
  });

  main.appendChild(name);
  main.appendChild(del);
  item.appendChild(main);

  // Summary line — shown when the API provides one
  if (doc.summary) {
    const summary = document.createElement('div');
    summary.className = 'doc-item-summary';
    summary.textContent = doc.summary;
    item.appendChild(summary);
  }

  return item;
}

/**
 * Uploads a file to the API and refreshes the appropriate document list on completion.
 *
 * Shows an "Uploading…" placeholder in *listEl* during the upload, switches to
 * "Processing…" once the file bytes have been sent (server is still embedding),
 * and uses fast GPU polling while the operation is in progress.
 *
 * @param {File} file - The file object selected by the user.
 * @param {HTMLElement} listEl - The document list element where the placeholder
 *   is inserted (either the global or chat-scoped list).
 * @param {string|null} conversationId - When set, scopes the document to that
 *   conversation; pass ``null`` for a globally-scoped upload.
 * @return {Promise<void>}
 */
async function _uploadDoc(file, listEl, conversationId) {
  const placeholder = document.createElement('div');
  placeholder.className = 'doc-item uploading';
  const pname = document.createElement('span');
  pname.className = 'doc-item-name';
  pname.textContent = `Uploading ${file.name}…`;
  placeholder.appendChild(pname);
  listEl.prepend(placeholder);

  uploadInProgress = true;
  modelDot.className = 'model-dot';
  modelDotLabel.textContent = 'Busy…';
  modelDotLabel.className = 'model-dot-label';
  gpuFastInterval = setInterval(pollGpu, 500);
  setStatus(`Uploading ${file.name}…`, 'busy');

  try {
    const body = new FormData();
    body.append('file', file);
    const url = conversationId
      ? `/api/documents?conversation_id=${conversationId}`
      : '/api/documents';

    await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', url);
      xhr.upload.addEventListener('load', () => {
        pname.textContent = `Processing ${file.name}…`;
        setStatus(`Processing ${file.name}…`, 'busy');
      });
      xhr.addEventListener('load', () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve();
        } else {
          let detail = xhr.status;
          try { detail = JSON.parse(xhr.responseText).detail || detail; } catch (_) {}
          reject(new Error(`Upload failed: ${detail}`));
        }
      });
      xhr.addEventListener('error', () => reject(new Error('Network error during upload')));
      xhr.send(body);
    });
    setStatus(`✓ ${file.name} uploaded`, 'info');
  } catch (err) {
    setStatus(err.message, 'error');
  } finally {
    uploadInProgress = false;
    clearInterval(gpuFastInterval);
    gpuFastInterval = null;
    placeholder.remove();
    if (conversationId) await fetchChatDocuments(conversationId);
    else await fetchDocuments();
    await Promise.all([pollModelStatus(), pollAnalysisModelStatus()]);
  }
}

// ── Global documents ──────────────────────────────────────────

/**
 * Fetches the list of globally-scoped documents from the API and renders them.
 *
 * @return {Promise<void>}
 */
async function fetchDocuments() {
  try {
    const res = await fetch('/api/documents');
    if (!res.ok) return;
    renderDocList(await res.json());
  } catch (_) {}
}

/**
 * Renders the global document list in the sidebar.
 *
 * Clears the existing list and rebuilds it.  Each item's delete button
 * calls the API and refreshes the list on success.
 *
 * @param {Array<{id: string, filename: string, chunk_count: number}>} docs - Documents
 *   returned by the API.
 * @return {void}
 */
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

/**
 * Shows or hides the chat-scoped documents section in the sidebar.
 *
 * Toggles visibility of the divider, section header, and document list together
 * so they always appear or disappear as a unit.
 *
 * @param {boolean} visible - ``true`` to show the section, ``false`` to hide it.
 * @return {void}
 */
function setChatDocsVisible(visible) {
  chatDocsDivider.hidden = !visible;
  chatDocsHeader.hidden = !visible;
  chatDocList.hidden = !visible;
}

/**
 * Fetches documents scoped to a specific conversation and renders them.
 *
 * Also ensures the chat-docs section becomes visible if any documents are
 * returned.  A no-op when *convId* is falsy.
 *
 * @param {string} [convId=currentConvId] - The conversation ID to fetch documents for.
 * @return {Promise<void>}
 */
async function fetchChatDocuments(convId = currentConvId) {
  if (!convId) return;
  try {
    const res = await fetch(`/api/documents?conversation_id=${convId}`);
    if (!res.ok) return;
    const docs = await res.json();
    renderChatDocList(docs);
    if (docs.length > 0) setChatDocsVisible(true);
  } catch (_) {}
}

/**
 * Renders conversation-scoped documents into the chat-docs list.
 *
 * Clears the existing list and rebuilds it.  Each item's delete button
 * removes the document via the API and refreshes the list.
 *
 * @param {Array<{id: string, filename: string, chunk_count: number}>} docs - Documents
 *   scoped to the current conversation.
 * @return {void}
 */
function renderChatDocList(docs) {
  chatDocList.innerHTML = '';
  for (const doc of docs) {
    const convId = currentConvId;
    chatDocList.appendChild(_makeDocItem(doc, async () => {
      await fetch(`/api/documents/${doc.id}`, { method: 'DELETE' });
      await fetchChatDocuments(convId);
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

  clearErrorBar();
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
    const wbProjectId = chatView.dataset.projectId;
    if (wbProjectId) body.project_id = wbProjectId;
    const systemPrompt = systemPromptInput.value.trim();
    if (systemPrompt) body.system = systemPrompt;

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
        } else if (evt.type === 'warning') {
          showErrorBar(evt.detail, 'warning');
        } else if (evt.type === 'error') {
          bubble.classList.remove('streaming');
          bubble.classList.add('error');
          bubble.textContent = `Error: ${evt.detail}`;
          showErrorBar(evt.detail);
          break outer;
        } else if (evt.type === 'done') {
          doneData = evt;
          break outer;
        }
      }
    }

    bubble.classList.remove('streaming');

    if (doneData) {
      // Swap accumulated plain text for rendered markdown now that the stream is complete.
      // During streaming we used textContent (no flicker); render once on done.
      const rawText = bubble.textContent;
      bubble.innerHTML = renderMarkdown(rawText);
      addMeta(inner, doneData.model, doneData.sources);
      addCopyBtn(inner, bubble);
      addEscalateBtn(inner, doneData.conversation_id, message, doneData.doc_ids || [], doneData.has_client_docs || false);
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
      const msg = `Network error: ${err.message}`;
      createMessage('ai', msg, { isError: true });
      showErrorBar(msg);
    }
  } finally {
    abortController = null;
    sendBtn.textContent = 'Send';
    sendBtn.classList.remove('stop-mode');
    input.focus();
  }
});

// ── GPU meters ────────────────────────────────────────────────
function _buildGpuMeter(i, label) {
  const div = document.createElement('div');
  div.className = 'gpu-meter';
  div.id = `gpu-meter-${i}`;
  div.innerHTML = `
    <div class="gpu-meter-row">
      <span class="gpu-meter-label">${label}</span>
      <span class="gpu-pct" id="gpu-pct-${i}">--</span>
    </div>
    <div class="gpu-bar-wrap"><div id="gpu-bar-${i}" class="gpu-bar"></div></div>
    <div class="gpu-meter-row"><span class="gpu-vram" id="gpu-vram-${i}">-- VRAM</span></div>`;
  return div;
}

async function pollGpu() {
  if (!gpuMeters) return;
  try {
    const res = await fetch('/api/gpu');
    if (!res.ok) { Array.from(gpuMeters.children).forEach(m => m.classList.add('unavailable')); return; }
    const d = await res.json();
    const noGpu = !d.ok || !d.gpus || d.gpus.length === 0;
    if (noGpu) {
      if (!gpuMeters.dataset.noGpu) {
        gpuMeters.innerHTML = '';
        gpuMeters.dataset.noGpu = '1';
        const m = _buildGpuMeter(0, 'No GPU');
        m.classList.add('unavailable');
        gpuMeters.appendChild(m);
      }
      return;
    }
    delete gpuMeters.dataset.noGpu;
    if (gpuMeters.children.length !== d.gpus.length) {
      gpuMeters.innerHTML = '';
      const multi = d.gpus.length > 1;
      d.gpus.forEach((_, i) => gpuMeters.appendChild(_buildGpuMeter(i, multi ? `GPU ${i}` : 'GPU')));
    }
    d.gpus.forEach((g, i) => {
      const meter = document.getElementById(`gpu-meter-${i}`);
      if (!meter) return;
      meter.classList.remove('unavailable');
      meter.title = g.name || '';
      document.getElementById(`gpu-pct-${i}`).textContent = `${g.gpu_util}%`;
      const bar = document.getElementById(`gpu-bar-${i}`);
      bar.style.width = `${g.gpu_util}%`;
      bar.className = 'gpu-bar' + (g.gpu_util > 85 ? ' hot' : g.gpu_util > 55 ? ' warm' : '');
      const used  = (g.mem_used  / 1073741824).toFixed(1);
      const total = (g.mem_total / 1073741824).toFixed(1);
      document.getElementById(`gpu-vram-${i}`).textContent = `${used}/${total} GB`;
    });
  } catch (_) {
    Array.from(gpuMeters.children).forEach(m => m.classList.add('unavailable'));
  }
}

// ── System meter (CPU / RAM) ──────────────────────────────────
const sysMeters = document.getElementById('sys-meters');

function _buildSysMeter() {
  const div = document.createElement('div');
  div.className = 'gpu-meter';
  div.id = 'sys-meter-cpu';
  div.innerHTML = `
    <div class="gpu-meter-row">
      <span class="gpu-meter-label">CPU</span>
      <span class="gpu-pct" id="sys-cpu-pct">--</span>
    </div>
    <div class="gpu-bar-wrap"><div id="sys-cpu-bar" class="gpu-bar"></div></div>
    <div class="gpu-meter-row"><span class="gpu-vram" id="sys-ram">-- RAM</span></div>`;
  return div;
}

async function pollSystem() {
  if (!sysMeters) return;
  if (!sysMeters.children.length) sysMeters.appendChild(_buildSysMeter());
  try {
    const res = await fetch('/api/system');
    if (!res.ok) { document.getElementById('sys-meter-cpu')?.classList.add('unavailable'); return; }
    const d = await res.json();
    if (!d.ok) { document.getElementById('sys-meter-cpu')?.classList.add('unavailable'); return; }
    document.getElementById('sys-meter-cpu').classList.remove('unavailable');
    document.getElementById('sys-cpu-pct').textContent = `${d.cpu_util}%`;
    const bar = document.getElementById('sys-cpu-bar');
    bar.style.width = `${d.cpu_util}%`;
    bar.className = 'gpu-bar' + (d.cpu_util > 85 ? ' hot' : d.cpu_util > 55 ? ' warm' : '');
    const used  = (d.mem_used  / 1073741824).toFixed(1);
    const total = (d.mem_total / 1073741824).toFixed(1);
    document.getElementById('sys-ram').textContent = `${used}/${total} GB`;
  } catch (_) { document.getElementById('sys-meter-cpu')?.classList.add('unavailable'); }
}

// ── merLLM status indicator ────────────────────────────────────
async function pollMerllm() {
  const dot   = document.getElementById('merllm-dot');
  const label = document.getElementById('merllm-label');
  if (!dot || !label) return;
  try {
    const res = await fetch('/api/merllm/status');
    if (!res.ok) throw new Error(res.status);
    const d = await res.json();
    const mode = d.mode || 'unknown';
    dot.className = 'merllm-dot ' + mode;
    const queue = d.queue?.total ?? 0;
    label.textContent = 'merLLM' + (queue > 0 ? ` (${queue})` : '');
    label.title = `Mode: ${mode}` + (d.warnings?.length ? '\n⚠ ' + d.warnings.join('\n⚠ ') : '');
    if (d.warnings?.length) {
      dot.style.boxShadow = '0 0 0 2px rgba(210,153,34,.4)';
    } else {
      dot.style.boxShadow = '';
    }
  } catch (_) {
    dot.className = 'merllm-dot unknown';
    label.textContent = 'merLLM';
    label.title = 'merLLM unreachable';
  }
}

// ── Error bar ─────────────────────────────────────────────────
/**
 * Displays a message in the error bar at the top of the chat panel.
 *
 * @param {string} msg - The message text to display.
 * @param {'error'|'warning'|'info'} [level='error'] - Severity level; controls the bar's
 *   colour via the ``data-level`` attribute.
 * @return {void}
 */
function showErrorBar(msg, level = 'error') {
  errorBarText.textContent = msg;
  errorBar.dataset.level = level;
  errorBar.hidden = false;
}


/**
 * Hides the error bar and clears its message text.
 *
 * @return {void}
 */
function clearErrorBar() {
  errorBar.hidden = true;
  errorBarText.textContent = '';
}

errorBarDismiss.addEventListener('click', clearErrorBar);

// ── Status bar ────────────────────────────────────────────────
let _statusClearTimer = null;

/**
 * Updates the bottom status bar with a message and optional severity level.
 * Non-error messages auto-clear after 5 s; errors persist until replaced.
 *
 * @param {string} msg - Message to display.
 * @param {'info'|'warning'|'error'|'busy'} [level='info']
 */
function setStatus(msg, level = 'info') {
  clearTimeout(_statusClearTimer);
  statusMsg.textContent = msg;
  statusBar.dataset.level = level === 'busy' ? 'info' : level;
  statusSpinner.classList.toggle('on', level === 'busy');
  if (level !== 'error' && level !== 'busy') {
    _statusClearTimer = setTimeout(() => {
      statusMsg.textContent = 'Ready';
      delete statusBar.dataset.level;
    }, 5000);
  }
}

// ── Model status dot ──────────────────────────────────────────
/**
 * Polls the model-status endpoint and updates the status dot in the toolbar.
 *
 * Sets the dot to ``loaded`` (green) or ``unloaded`` (grey) based on whether
 * the selected model is currently resident in Ollama.  If Ollama swapped to a
 * different model, an info-level error bar is shown.  A no-op while an upload
 * is in progress to avoid misleading "Not loaded" flickers.
 *
 * @return {Promise<void>}
 */
async function pollModelStatus() {
  const model = modelSel.value;
  if (!model) return;
  if (uploadInProgress) return;
  try {
    const res = await fetch(`/api/model-status?model=${encodeURIComponent(model)}`);
    if (!res.ok) return;
    const { loaded, active } = await res.json();
    modelDot.className = 'model-dot ' + (loaded ? 'loaded' : 'unloaded');
    modelDotLabel.textContent = loaded ? 'Ready' : 'Not loaded';
    modelDotLabel.className = 'model-dot-label ' + (loaded ? 'loaded' : 'unloaded');
    if (!loaded && active && active.length > 0) {
      showErrorBar(`Ollama switched to ${active.join(', ')}`, 'info');
    } else if (loaded) {
      clearErrorBar();
    }
  } catch (_) {
    modelDot.className = 'model-dot';
    modelDotLabel.textContent = 'Unknown';
    modelDotLabel.className = 'model-dot-label';
  }
}

// ── Model list ───────────────────────────────────────────────
/**
 * Fetches the list of available Ollama models and populates the model selector.
 *
 * Restores the previously selected model from ``localStorage`` if it is still
 * present in the returned list.
 *
 * @return {Promise<void>}
 */
async function fetchModels() {
  try {
    const res = await fetch('/api/models');
    if (!res.ok) return;
    const { models } = await res.json();
    const savedChat     = localStorage.getItem('selectedModel');
    const savedAnalysis = localStorage.getItem('analysisModel');
    modelSel.innerHTML    = '';
    analysisSel.innerHTML = '';
    for (const name of models) {
      const optChat = document.createElement('option');
      optChat.value = name;
      optChat.textContent = name;
      if (name === savedChat) optChat.selected = true;
      modelSel.appendChild(optChat);

      const optAnalysis = document.createElement('option');
      optAnalysis.value = name;
      optAnalysis.textContent = name;
      if (name === savedAnalysis) optAnalysis.selected = true;
      analysisSel.appendChild(optAnalysis);
    }
    // Notify the backend of the current analysis model selection.
    if (analysisSel.value) {
      fetch('/api/set-analysis-model', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: analysisSel.value }),
      }).catch(() => {});
    }
  } catch (_) {}
}

async function pollAnalysisModelStatus() {
  const model = analysisSel.value;
  if (!model) return;
  if (uploadInProgress) return;
  try {
    const res = await fetch(`/api/model-status?model=${encodeURIComponent(model)}`);
    if (!res.ok) return;
    const { loaded } = await res.json();
    analysisDot.className = 'model-dot ' + (loaded ? 'loaded' : 'unloaded');
    analysisDotLabel.textContent = loaded ? 'Ready' : 'Not loaded';
    analysisDotLabel.className = 'model-dot-label ' + (loaded ? 'loaded' : 'unloaded');
  } catch (_) {
    analysisDot.className = 'model-dot';
    analysisDotLabel.textContent = 'Unknown';
    analysisDotLabel.className = 'model-dot-label';
  }
}

modelSel.addEventListener('change', () => {
  localStorage.setItem('selectedModel', modelSel.value);
  pollModelStatus();
});

analysisSel.addEventListener('change', async () => {
  const model = analysisSel.value;
  localStorage.setItem('analysisModel', model);
  try {
    await fetch('/api/set-analysis-model', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model }),
    });
  } catch (_) {}
  pollAnalysisModelStatus();
});

loadModelBtn.addEventListener('click', async () => {
  const model = modelSel.value;
  if (!model) return;
  loadModelBtn.disabled = true;
  modelDotLabel.textContent = 'Loading…';
  modelDotLabel.className = 'model-dot-label';
  modelDot.className = 'model-dot unloaded';
  try {
    const res = await fetch('/api/warm-model', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showErrorBar(err.detail || 'Failed to load model');
    }
  } catch (err) {
    showErrorBar(`Load error: ${err.message}`);
  } finally {
    loadModelBtn.disabled = false;
    await pollModelStatus();
  }
});

loadAnalysisBtn.addEventListener('click', async () => {
  const model = analysisSel.value;
  if (!model) return;
  loadAnalysisBtn.disabled = true;
  analysisDotLabel.textContent = 'Loading…';
  analysisDotLabel.className = 'model-dot-label';
  analysisDot.className = 'model-dot unloaded';
  try {
    const res = await fetch('/api/warm-model', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showErrorBar(err.detail || 'Failed to load analysis model');
    }
  } catch (err) {
    showErrorBar(`Load error: ${err.message}`);
  } finally {
    loadAnalysisBtn.disabled = false;
    await pollAnalysisModelStatus();
  }
});

refreshModelsBtn.addEventListener('click', async () => {
  refreshModelsBtn.disabled = true;
  refreshModelsBtn.textContent = '…';
  try {
    await fetchModels();
    await Promise.all([pollModelStatus(), pollAnalysisModelStatus()]);
  } finally {
    refreshModelsBtn.disabled = false;
    refreshModelsBtn.textContent = '↻';
  }
});

// ── Tab switching ─────────────────────────────────────────────
const tabBar         = document.getElementById('tab-bar');
const chatView       = document.getElementById('chat-view');
const workbenchView  = document.getElementById('workbench-view');
const libraryView    = document.getElementById('library-view');

tabBar.addEventListener('click', e => {
  const btn = e.target.closest('.tab-btn');
  if (!btn) return;
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  const tab = btn.dataset.tab;
  chatView.hidden      = tab !== 'chat';
  workbenchView.hidden = tab !== 'workbench';
  libraryView.hidden   = tab !== 'library';
  if (tab === 'workbench') loadWorkbench();
  if (tab === 'library')   loadLibrary();
});

// ── Workbench ─────────────────────────────────────────────────
const wbScopeList    = document.getElementById('wb-scope-list');
const wbManageBtn    = document.getElementById('wb-manage-btn');
const wbOpenChatBtn  = document.getElementById('wb-open-chat-btn');
const wbTypeFilter   = document.getElementById('wb-type-filter');
const wbUploadType   = document.getElementById('wb-upload-type');
const wbDocUpload    = document.getElementById('wb-doc-upload');
const wbUploadStatus = document.getElementById('wb-upload-status');
const wbDocTbody     = document.getElementById('wb-doc-tbody');

// Manage modal
const wbManageBackdrop = document.getElementById('wb-manage-backdrop');
const wbManageModal    = document.getElementById('wb-manage-modal');
const wbManageBody     = document.getElementById('wb-manage-body');
const wbManageClose    = document.getElementById('wb-manage-close');
const wbNewClientInput = document.getElementById('wb-new-client-input');
const wbAddClientBtn   = document.getElementById('wb-add-client-btn');

let wbClients     = [];
let wbAllProjects = [];
let wbDocs        = [];
// Active scope: { type: 'global'|'client'|'project', clientId, projectId, label }
let wbScope = { type: 'global', clientId: null, projectId: null, label: 'Global' };

function _esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

async function loadWorkbench() {
  const [cr, pr] = await Promise.all([
    fetch('/api/workspace/clients').catch(() => null),
    fetch('/api/workspace/projects').catch(() => null),
  ]);
  wbClients     = cr?.ok ? await cr.json() : [];
  wbAllProjects = pr?.ok ? await pr.json() : [];
  renderWbScopeList();
  await loadWbDocs();
}

function renderWbScopeList() {
  wbScopeList.innerHTML = '';

  // Global row
  const globalRow = _makeScopeRow('◆ Global', 'global-row', wbScope.type === 'global' && !wbScope.clientId);
  globalRow.addEventListener('click', () => setWbScope({ type: 'global', clientId: null, projectId: null, label: 'Global' }));
  wbScopeList.appendChild(globalRow);

  // Clients + their projects
  wbClients.forEach(c => {
    const clientActive = wbScope.type === 'client' && wbScope.clientId === c.id;
    const cRow = _makeScopeRow(c.name, 'client-row', clientActive);
    cRow.addEventListener('click', () => setWbScope({ type: 'client', clientId: c.id, projectId: null, label: c.name }));
    wbScopeList.appendChild(cRow);

    wbAllProjects.filter(p => p.client_id === c.id).forEach(p => {
      const projActive = wbScope.type === 'project' && wbScope.projectId === p.id;
      const pRow = _makeScopeRow(p.name, 'project-row', projActive);
      pRow.addEventListener('click', () => setWbScope({ type: 'project', clientId: c.id, projectId: p.id, label: p.name }));
      wbScopeList.appendChild(pRow);
    });
  });
}

function _makeScopeRow(label, extraClass, active) {
  const row = document.createElement('div');
  row.className = 'wb-scope-row' + (extraClass ? ' ' + extraClass : '') + (active ? ' active' : '');
  row.innerHTML = `<span class="wb-scope-dot"></span><span>${_esc(label)}</span>`;
  return row;
}

async function setWbScope(scope) {
  wbScope = scope;
  wbOpenChatBtn.disabled = scope.type !== 'project';
  document.getElementById('wb-scope-label').textContent = scope.label;
  renderWbScopeList();
  await loadWbDocs();
}

async function loadWbDocs() {
  const params = new URLSearchParams();
  if (wbScope.type === 'project' && wbScope.projectId) params.set('project_id', wbScope.projectId);
  else if (wbScope.type === 'client' && wbScope.clientId) params.set('client_id', wbScope.clientId);
  // global: no params → returns global-scoped docs only
  try {
    const res = await fetch(`/api/documents?${params}`);
    wbDocs = res.ok ? await res.json() : [];
  } catch { wbDocs = []; }
  renderWbDocs();
}

// Doc-type options shared between the upload selector and the inline edit form.
const _WB_DOC_TYPE_OPTIONS = [
  ['standard','Standard'], ['requirement','Requirement'], ['theop','THEOP'],
  ['fmea','FMEA'], ['hazard_analysis','Hazard Analysis'], ['fat','FAT'],
  ['sat','SAT'], ['contract','Contract'], ['correspondence','Correspondence'],
  ['plc_code','PLC Code'], ['technical_manual','Technical Manual'],
  ['datasheet','Datasheet'], ['firmware_notes','Firmware Notes'],
  ['app_note','App Note'], ['misc','Misc'],
];

function renderWbDocs() {
  const filter = wbTypeFilter.value;
  const rows = wbDocs.filter(d => !filter || d.doc_type === filter);
  if (!rows.length) {
    const msg = wbScope.type === 'global' && !wbClients.length
      ? 'No global documents yet. Upload a standard or reference document using the + Upload button.'
      : 'No documents found for this scope.';
    wbDocTbody.innerHTML = `<tr class="wb-empty-row"><td colspan="6">${msg}</td></tr>`;
    return;
  }
  wbDocTbody.innerHTML = '';
  rows.forEach(d => {
    const tr = document.createElement('tr');
    const date = (d.created_at || '').slice(0, 10);
    tr.innerHTML = `
      <td title="${_esc(d.filename)}">${_esc(d.filename)}</td>
      <td><span class="doc-type-badge">${_esc(d.doc_type)}</span></td>
      <td><span class="scope-badge ${d.scope_type}">${d.scope_type}</span></td>
      <td>${date}</td>
      <td>${d.chunk_count}</td>
      <td class="wb-actions">
        <button class="wb-edit-btn" data-id="${d.id}" title="Edit attributes">✎</button>
        <button class="wb-del-btn" data-id="${d.id}" data-name="${_esc(d.filename)}" title="Delete document">✕</button>
      </td>`;
    wbDocTbody.appendChild(tr);
  });
}

function _openWbEdit(tr, doc) {
  // Build inline edit row, inserted immediately after the document row.
  const editTr = document.createElement('tr');
  editTr.className = 'wb-edit-row';

  const typeOpts = _WB_DOC_TYPE_OPTIONS.map(([v, l]) =>
    `<option value="${v}"${v === doc.doc_type ? ' selected' : ''}>${l}</option>`
  ).join('');

  // Scope selector: encode as "type:id" so it's a single <select>
  const currentScopeVal = doc.scope_type === 'global' ? 'global:'
    : `${doc.scope_type}:${doc.scope_id || ''}`;
  const scopeOpts = [
    `<option value="global:"${doc.scope_type === 'global' ? ' selected' : ''}>Global</option>`,
    ...wbClients.map(c =>
      `<option value="client:${_esc(c.id)}"${doc.scope_type === 'client' && doc.scope_id === c.id ? ' selected' : ''}>`
      + `Client: ${_esc(c.name)}</option>`
    ),
    ...wbAllProjects.map(p => {
      const cl = wbClients.find(c => c.id === p.client_id);
      const lbl = cl ? `${_esc(p.name)} (${_esc(cl.name)})` : _esc(p.name);
      return `<option value="project:${_esc(p.id)}"${doc.scope_type === 'project' && doc.scope_id === p.id ? ' selected' : ''}>`
        + `Project: ${lbl}</option>`;
    }),
  ].join('');

  // Classification — whether public is allowed depends on the *current* scope selection
  function _buildClassOpts(scopeVal) {
    const isRestricted = scopeVal.startsWith('client:') || scopeVal.startsWith('project:');
    return [
      `<option value="client"${doc.classification === 'client' || isRestricted ? ' selected' : ''}>Client (confidential)</option>`,
      !isRestricted
        ? `<option value="public"${doc.classification === 'public' ? ' selected' : ''}>Public</option>`
        : '',
    ].join('');
  }

  editTr.innerHTML = `<td colspan="6">
    <div class="wb-edit-form">
      <label class="wb-edit-label">Filename</label>
      <input class="wb-edit-input" data-field="filename" value="${_esc(doc.filename)}" />
      <label class="wb-edit-label">Type</label>
      <select class="wb-edit-select" data-field="doc_type">${typeOpts}</select>
      <label class="wb-edit-label">Scope</label>
      <select class="wb-edit-select" data-field="scope">${scopeOpts}</select>
      <label class="wb-edit-label">Classification</label>
      <select class="wb-edit-select" data-field="classification">${_buildClassOpts(currentScopeVal)}</select>
      <button class="wb-edit-save">Save</button>
      <button class="wb-edit-cancel">Cancel</button>
    </div>
  </td>`;

  tr.after(editTr);
  tr.classList.add('wb-editing');
  editTr.querySelector('.wb-edit-input').focus();

  // When scope changes, rebuild classification options.
  editTr.querySelector('[data-field="scope"]').addEventListener('change', e => {
    const classSelect = editTr.querySelector('[data-field="classification"]');
    classSelect.innerHTML = _buildClassOpts(e.target.value);
  });

  editTr.querySelector('.wb-edit-cancel').addEventListener('click', () => {
    editTr.remove();
    tr.classList.remove('wb-editing');
  });

  editTr.querySelector('.wb-edit-save').addEventListener('click', async () => {
    const filename       = editTr.querySelector('[data-field="filename"]').value.trim();
    const doc_type       = editTr.querySelector('[data-field="doc_type"]').value;
    const classification = editTr.querySelector('[data-field="classification"]').value;
    const scopeVal       = editTr.querySelector('[data-field="scope"]').value;
    const [scope_type, scope_id] = scopeVal.split(':');

    const saveBtn = editTr.querySelector('.wb-edit-save');
    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving…';
    try {
      const res = await fetch(`/api/documents/${doc.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          filename, doc_type, classification,
          scope_type, scope_id: scope_id || null,
        }),
      });
      if (res.ok) {
        editTr.remove();
        tr.classList.remove('wb-editing');
        await loadWbDocs();
        setStatus('Document updated.', 'info');
      } else {
        const err = await res.json().catch(() => ({}));
        setStatus(err.detail || 'Failed to save changes.', 'error');
        saveBtn.disabled = false;
        saveBtn.textContent = 'Save';
      }
    } catch {
      setStatus('Failed to save changes — network error.', 'error');
      saveBtn.disabled = false;
      saveBtn.textContent = 'Save';
    }
  });
}

wbDocTbody.addEventListener('click', async e => {
  const editBtn = e.target.closest('.wb-edit-btn');
  if (editBtn) {
    const doc = wbDocs.find(d => d.id === editBtn.dataset.id);
    if (!doc) return;
    const tr = editBtn.closest('tr');
    // Toggle: if already editing this row, cancel.
    const existing = tr.nextElementSibling;
    if (existing?.classList.contains('wb-edit-row')) {
      existing.remove();
      tr.classList.remove('wb-editing');
      return;
    }
    // Close any other open edit rows first.
    wbDocTbody.querySelectorAll('.wb-edit-row').forEach(r => r.remove());
    wbDocTbody.querySelectorAll('.wb-editing').forEach(r => r.classList.remove('wb-editing'));
    _openWbEdit(tr, doc);
    return;
  }

  const delBtn = e.target.closest('.wb-del-btn');
  if (!delBtn) return;
  if (!confirm(`Delete "${delBtn.dataset.name}"?\n\nThis removes it permanently from the knowledge graph.`)) return;
  const res = await fetch(`/api/documents/${delBtn.dataset.id}`, { method: 'DELETE' });
  if (res.ok) await loadWbDocs();
  else showErrorBar('Failed to delete document.');
});

wbTypeFilter.addEventListener('change', renderWbDocs);

wbOpenChatBtn.addEventListener('click', () => {
  if (wbScope.type !== 'project') return;
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelector('.tab-btn[data-tab="chat"]').classList.add('active');
  chatView.hidden      = false;
  workbenchView.hidden = true;
  libraryView.hidden   = true;
  newChat();
  chatView.dataset.projectId   = wbScope.projectId;
  chatView.dataset.projectName = wbScope.label;
  input.placeholder = `Ask anything… (project: ${wbScope.label})`;
  input.focus();
});

wbDocUpload.addEventListener('change', async () => {
  const file = wbDocUpload.files[0];
  if (!file) return;
  const docType = wbUploadType.value;
  const fd = new FormData();
  fd.append('file', file);
  fd.append('doc_type', docType);
  if (wbScope.type === 'project' && wbScope.projectId) fd.append('project_id', wbScope.projectId);
  else if (wbScope.type === 'client' && wbScope.clientId) fd.append('client_id', wbScope.clientId);
  // global: no scope param

  setStatus(`Uploading ${file.name}…`, 'busy');
  try {
    const res = await fetch('/api/documents', { method: 'POST', body: fd });
    if (res.ok) {
      setStatus(`✓ ${file.name} uploaded`, 'info');
      await loadWbDocs();
    } else {
      const err = await res.json().catch(() => ({}));
      setStatus(err.detail || 'Upload failed.', 'error');
    }
  } catch {
    setStatus('Upload failed — network error.', 'error');
  }
  wbDocUpload.value = '';
});

// ── Manage modal ──────────────────────────────────────────────
function openManageModal() {
  renderManageModal();
  wbManageBackdrop.hidden = false;
  wbManageModal.hidden    = false;
}

function closeManageModal() {
  wbManageBackdrop.hidden = true;
  wbManageModal.hidden    = true;
}

function renderManageModal() {
  wbManageBody.innerHTML = '';
  if (!wbClients.length) {
    wbManageBody.innerHTML = '<p class="manage-empty">No clients yet. Add one below.</p>';
    return;
  }
  wbClients.forEach(c => {
    const projects = wbAllProjects.filter(p => p.client_id === c.id);
    const section = document.createElement('div');
    section.className = 'manage-client-section';

    const clientRow = document.createElement('div');
    clientRow.className = 'manage-client-row';
    clientRow.innerHTML = `<span class="manage-client-name">${_esc(c.name)}</span>`;
    const delClientBtn = document.createElement('button');
    delClientBtn.className = 'wb-del-btn';
    delClientBtn.title = 'Delete client';
    delClientBtn.textContent = '✕';
    delClientBtn.addEventListener('click', async () => {
      if (!confirm(`Delete client "${c.name}" and all its projects?`)) return;
      const res = await fetch(`/api/workspace/clients/${c.id}`, { method: 'DELETE' });
      if (res.ok) { await loadWorkbench(); renderManageModal(); }
      else showErrorBar('Failed to delete client.');
    });
    clientRow.appendChild(delClientBtn);
    section.appendChild(clientRow);

    projects.forEach(p => {
      const pRow = document.createElement('div');
      pRow.className = 'manage-project-row';
      pRow.innerHTML = `<span>◇ ${_esc(p.name)}</span>`;
      const delProjBtn = document.createElement('button');
      delProjBtn.className = 'wb-del-btn';
      delProjBtn.title = 'Delete project';
      delProjBtn.textContent = '✕';
      delProjBtn.addEventListener('click', async () => {
        if (!confirm(`Delete project "${p.name}"?`)) return;
        const res = await fetch(`/api/workspace/projects/${p.id}`, { method: 'DELETE' });
        if (res.ok) { await loadWorkbench(); renderManageModal(); }
        else showErrorBar('Failed to delete project.');
      });
      pRow.appendChild(delProjBtn);
      section.appendChild(pRow);
    });

    // Add project row
    const addProjRow = document.createElement('div');
    addProjRow.className = 'manage-add-project';
    const projInput = document.createElement('input');
    projInput.type = 'text';
    projInput.placeholder = 'New project name…';
    const addProjBtn = document.createElement('button');
    addProjBtn.className = 'icon-btn';
    addProjBtn.textContent = '+ Add';
    addProjBtn.addEventListener('click', async () => {
      const name = projInput.value.trim();
      if (!name) return;
      const res = await fetch('/api/workspace/projects', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, client_id: c.id }),
      });
      if (res.ok) { projInput.value = ''; await loadWorkbench(); renderManageModal(); }
      else { const e = await res.json().catch(() => ({})); showErrorBar(e.detail || 'Failed to create project.'); }
    });
    projInput.addEventListener('keydown', e => { if (e.key === 'Enter') addProjBtn.click(); });
    addProjRow.appendChild(projInput);
    addProjRow.appendChild(addProjBtn);
    section.appendChild(addProjRow);

    wbManageBody.appendChild(section);
  });
}

wbManageBtn.addEventListener('click', openManageModal);
wbManageClose.addEventListener('click', closeManageModal);
wbManageBackdrop.addEventListener('click', closeManageModal);

wbAddClientBtn.addEventListener('click', async () => {
  const name = wbNewClientInput.value.trim();
  if (!name) return;
  const res = await fetch('/api/workspace/clients', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  });
  if (res.ok) { wbNewClientInput.value = ''; await loadWorkbench(); renderManageModal(); }
  else { const e = await res.json().catch(() => ({})); showErrorBar(e.detail || 'Failed to create client.'); }
});
wbNewClientInput.addEventListener('keydown', e => { if (e.key === 'Enter') wbAddClientBtn.click(); });

// ── Maintenance actions ───────────────────────────────────────
const wbReindexBtn        = document.getElementById('wb-reindex-btn');
const wbReindexAllBtn     = document.getElementById('wb-reindex-all-btn');
const wbMigrateScopeBtn   = document.getElementById('wb-migrate-scope-btn');
const wbMaintenanceStatus = document.getElementById('wb-maintenance-status');

async function runReindex(params = {}) {
  const qs = new URLSearchParams(params).toString();
  const url = '/api/documents/reindex' + (qs ? '?' + qs : '');
  const btn = params.project_id || params.client_id ? wbReindexBtn : wbReindexAllBtn;
  const statusEl = params.project_id || params.client_id ? wbUploadStatus : wbMaintenanceStatus;
  btn.disabled = true;
  statusEl.textContent = 'Re-indexing…';
  try {
    const res = await fetch(url, { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (res.ok) {
      statusEl.textContent = `Done — ${data.docs_reindexed} doc(s), ${data.chunks_processed} chunk(s)`;
    } else {
      statusEl.textContent = data.detail || 'Re-index failed.';
    }
  } catch (e) {
    statusEl.textContent = 'Re-index error.';
  } finally {
    btn.disabled = false;
    setTimeout(() => { statusEl.textContent = ''; }, 8000);
  }
}

wbReindexBtn.addEventListener('click', () => {
  const params = {};
  if (wbScope.type === 'project' && wbScope.projectId) params.project_id = wbScope.projectId;
  else if (wbScope.type === 'client' && wbScope.clientId) params.client_id = wbScope.clientId;
  runReindex(params);
});

wbReindexAllBtn.addEventListener('click', () => runReindex());

wbMigrateScopeBtn.addEventListener('click', async () => {
  wbMigrateScopeBtn.disabled = true;
  wbMaintenanceStatus.textContent = 'Migrating…';
  try {
    const res = await fetch('/api/documents/migrate-concept-scope', { method: 'POST' });
    wbMaintenanceStatus.textContent = res.ok ? 'Migration complete.' : 'Migration failed.';
  } catch (e) {
    wbMaintenanceStatus.textContent = 'Migration error.';
  } finally {
    wbMigrateScopeBtn.disabled = false;
    setTimeout(() => { wbMaintenanceStatus.textContent = ''; }, 8000);
  }
});

// ── Help modal ────────────────────────────────────────────────
const helpBackdrop  = document.getElementById('help-backdrop');
const helpModal     = document.getElementById('help-modal');
const helpCloseBtn  = document.getElementById('help-close-btn');
const helpCloseX    = document.getElementById('help-close');
const helpBtn       = document.getElementById('help-btn');

function openHelp()  { helpBackdrop.hidden = false; helpModal.hidden = false; }
function closeHelp() { helpBackdrop.hidden = true;  helpModal.hidden = true; }

helpBtn.addEventListener('click', openHelp);
helpCloseBtn.addEventListener('click', closeHelp);
helpCloseX.addEventListener('click', closeHelp);
helpBackdrop.addEventListener('click', closeHelp);
document.addEventListener('keydown', e => { if (e.key === 'Escape') { closeHelp(); closeManageModal(); } });

// ── Library ───────────────────────────────────────────────────
const libMfrTree       = document.getElementById('lib-mfr-tree');
const libItemTbody     = document.getElementById('lib-item-tbody');
const libDoctypeFilter = document.getElementById('lib-doctype-filter');
const libRefreshBtn    = document.getElementById('lib-refresh-btn');
const libAddBtn        = document.getElementById('lib-add-btn');
const libSubBar        = document.getElementById('lib-sub-bar');

let libItems         = [];
let libActiveDocType = null;  // null = all categories

// Display labels and preferred ordering for doc_type values.
const _LIB_TYPE_LABELS = {
  standard:       'Standards',
  manual:         'Manuals',
  datasheet:      'Datasheets',
  firmware_notes: 'Firmware Notes',
  app_note:       'App Notes',
  mounting:       'Mounting',
};
const _LIB_TYPE_ORDER = ['standard', 'manual', 'datasheet', 'firmware_notes', 'app_note', 'mounting'];

// Sub-tab switching
libSubBar.addEventListener('click', e => {
  const btn = e.target.closest('.lib-sub-btn');
  if (!btn) return;
  document.querySelectorAll('.lib-sub-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  const subtab = btn.dataset.subtab;
  document.getElementById('lib-browse').hidden      = subtab !== 'browse';
  document.getElementById('lib-acquisition').hidden = subtab !== 'acquisition';
  document.getElementById('lib-escalation').hidden  = subtab !== 'escalation';
  document.getElementById('lib-connections').hidden = subtab !== 'connections';
});

async function loadLibrary() {
  const ir = await fetch('/api/library/items').catch(() => null);
  libItems = ir?.ok ? await ir.json() : [];
  renderLibMfrTree();
  renderLibItems();
}

function renderLibMfrTree() {
  libMfrTree.innerHTML = '';

  if (libItems.length === 0) {
    libMfrTree.innerHTML = '<p class="lib-empty-msg">No items in library yet.</p>';
    return;
  }

  // "All" row
  const allRow = document.createElement('div');
  allRow.className = 'lib-mfr-row' + (libActiveDocType === null ? ' active' : '');
  allRow.innerHTML = `<span class="lib-mfr-name">◆ All</span><span class="lib-mfr-count">${libItems.length}</span>`;
  allRow.addEventListener('click', () => { libActiveDocType = null; renderLibMfrTree(); renderLibItems(); });
  libMfrTree.appendChild(allRow);

  // Count items per doc_type
  const typeCounts = {};
  libItems.forEach(item => {
    const t = item.doc_type || 'misc';
    typeCounts[t] = (typeCounts[t] || 0) + 1;
  });

  // Render in preferred order, then any remaining types alphabetically
  const ordered = _LIB_TYPE_ORDER.filter(t => typeCounts[t]);
  Object.keys(typeCounts).sort().forEach(t => {
    if (!ordered.includes(t)) ordered.push(t);
  });

  ordered.forEach(docType => {
    const label = _LIB_TYPE_LABELS[docType] || docType;
    const row = document.createElement('div');
    row.className = 'lib-mfr-row' + (libActiveDocType === docType ? ' active' : '');
    row.innerHTML = `<span class="lib-mfr-name">${_esc(label)}</span>`
      + `<span class="lib-mfr-count">${typeCounts[docType]}</span>`;
    row.addEventListener('click', () => {
      libActiveDocType = docType;
      renderLibMfrTree();
      renderLibItems();
    });
    libMfrTree.appendChild(row);
  });
}

function _updateLibMfrFilter(visibleItems) {
  // Rebuild the source dropdown from the currently visible items.
  const current = libDoctypeFilter.value;
  const sources = [...new Set(visibleItems.map(i => i.manufacturer).filter(Boolean))].sort();
  libDoctypeFilter.innerHTML = '<option value="">All sources</option>'
    + sources.map(s => `<option value="${_esc(s)}"${s === current ? ' selected' : ''}>${_esc(s)}</option>`).join('');
}

function renderLibItems() {
  const srcFilter = libDoctypeFilter.value;
  let items = libActiveDocType
    ? libItems.filter(i => (i.doc_type || 'misc') === libActiveDocType)
    : libItems;

  _updateLibMfrFilter(items);

  if (srcFilter) items = items.filter(i => (i.manufacturer || '') === srcFilter);

  if (items.length === 0) {
    libItemTbody.innerHTML = '<tr class="lib-empty-row"><td colspan="6">'
      + (libItems.length === 0 ? 'Library is empty.' : 'No documents match the filter.') + '</td></tr>';
    return;
  }

  libItemTbody.innerHTML = items.map(item => {
    const date = (item.updated_at || item.created_at || '').slice(0, 10);
    const src  = item.product_id
      ? `<span class="lib-src-cell"><span class="lib-src-mfr">${_esc(item.manufacturer || '—')}</span>`
        + `<span class="lib-src-pid">${_esc(item.product_id)}</span></span>`
      : `<span class="lib-src-mfr">${_esc(item.manufacturer || '—')}</span>`; // source only
    const dot  = item.indexed
      ? '<span class="lib-indexed-dot indexed" title="Indexed">●</span>'
      : '<span class="lib-indexed-dot" title="Not indexed">○</span>';
    const dl   = `<a class="lib-dl-btn" href="/api/library/items/${_esc(item.id)}/download" `
      + `title="Download" download="${_esc(item.filename)}">↓</a>`;
    const del  = `<button class="lib-del-btn" data-id="${_esc(item.id)}" `
      + `data-name="${_esc(item.filename)}" title="Remove from library">✕</button>`;
    return `<tr>
      <td class="lib-filename" title="${_esc(item.filepath)}">${_esc(item.filename)}</td>
      <td>${src}</td>
      <td>${_esc(item.version || '—')}</td>
      <td>${date || '—'}</td>
      <td>${dot}</td>
      <td class="lib-actions">${dl}${del}</td>
    </tr>`;
  }).join('');

  // Delete buttons
  libItemTbody.querySelectorAll('.lib-del-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const name = btn.dataset.name;
      if (!confirm(`Remove "${name}" from the library?`)) return;
      const res = await fetch(`/api/library/items/${btn.dataset.id}`, { method: 'DELETE' });
      if (res.ok || res.status === 204) {
        libItems = libItems.filter(i => i.id !== btn.dataset.id);
        await loadLibrary();
      } else {
        showErrorBar('Failed to remove library item.');
      }
    });
  });
}

libDoctypeFilter.addEventListener('change', renderLibItems);
libRefreshBtn.addEventListener('click', loadLibrary);

// ── Library upload modal ───────────────────────────────────────
const libUploadBackdrop = document.getElementById('lib-upload-backdrop');
const libUploadModal    = document.getElementById('lib-upload-modal');
const libUploadClose    = document.getElementById('lib-upload-close');
const libUploadCancel   = document.getElementById('lib-upload-cancel');
const libUploadSubmit   = document.getElementById('lib-upload-submit');
const libUpSource       = document.getElementById('lib-up-source');
const libUpRef          = document.getElementById('lib-up-ref');
const libUpDoctype      = document.getElementById('lib-up-doctype');
const libUpVersion      = document.getElementById('lib-up-version');
const libUpFile         = document.getElementById('lib-up-file');

function openLibUpload() {
  libUpSource.value  = '';
  libUpRef.value     = '';
  libUpVersion.value = '';
  libUpFile.value    = '';
  libUpDoctype.value = 'technical_manual';
  libUploadBackdrop.hidden = false;
  libUploadModal.hidden    = false;
  libUpSource.focus();
}
function closeLibUpload() {
  libUploadBackdrop.hidden = true;
  libUploadModal.hidden    = true;
}

libAddBtn.addEventListener('click', openLibUpload);
libUploadClose.addEventListener('click', closeLibUpload);
libUploadCancel.addEventListener('click', closeLibUpload);
libUploadBackdrop.addEventListener('click', closeLibUpload);

libUploadSubmit.addEventListener('click', async () => {
  const source = libUpSource.value.trim();
  const file   = libUpFile.files[0];
  if (!source) { setStatus('Source is required.', 'error'); return; }
  if (!file)   { setStatus('Please select a file.', 'error'); return; }

  const fd = new FormData();
  fd.append('file',       file);
  fd.append('source',     source);
  fd.append('reference',  libUpRef.value.trim());
  fd.append('doc_type',   libUpDoctype.value);
  fd.append('version',    libUpVersion.value.trim());

  libUploadSubmit.disabled    = true;
  libUploadSubmit.textContent = 'Uploading…';
  try {
    const res = await fetch('/api/library/items/upload', { method: 'POST', body: fd });
    if (res.ok) {
      closeLibUpload();
      await loadLibrary();
      setStatus('File added to library.', 'info');
    } else {
      const err = await res.json().catch(() => ({}));
      setStatus(err.detail || 'Upload failed.', 'error');
    }
  } catch {
    setStatus('Upload failed — network error.', 'error');
  } finally {
    libUploadSubmit.disabled    = false;
    libUploadSubmit.textContent = 'Upload';
  }
});

// ── Acquisition ───────────────────────────────────────────────
const acqActiveList     = document.getElementById('acq-active-list');
const acqPendingList    = document.getElementById('acq-pending-list');
const acqSseDot         = document.getElementById('acq-sse-dot');
const acqAddBtn         = document.getElementById('acq-add-btn');
const acqApproveAllBtn  = document.getElementById('acq-approve-all-btn');
const acqRequestBackdrop = document.getElementById('acq-request-backdrop');
const acqRequestModal   = document.getElementById('acq-request-modal');
const acqRequestClose   = document.getElementById('acq-request-close');
const acqRequestCancel  = document.getElementById('acq-request-cancel');
const acqRequestSubmit  = document.getElementById('acq-request-submit');
const acqMfrInput       = document.getElementById('acq-mfr-input');
const acqProductInput   = document.getElementById('acq-product-input');
const acqDoctypeSelect  = document.getElementById('acq-doctype-select');
const acqUrlInput       = document.getElementById('acq-url-input');
const acqReasonInput    = document.getElementById('acq-reason-input');

let acqQueue   = [];       // all queue items
let acqSse     = null;     // EventSource instance
// item_id → { el, filesEl, statusEl } for active job cards
const acqActiveCards = new Map();

async function loadAcquisitionQueue() {
  const r = await fetch('/api/acquisition/queue').catch(() => null);
  acqQueue = r?.ok ? await r.json() : [];
  renderAcqPending();
  renderAcqActive();
}

function renderAcqPending() {
  const pending = acqQueue.filter(i => i.status === 'pending_approval');
  if (pending.length === 0) {
    acqPendingList.innerHTML = '<p class="acq-empty-msg">No items waiting for approval.</p>';
    acqApproveAllBtn.disabled = true;
    return;
  }
  acqApproveAllBtn.disabled = false;
  acqPendingList.innerHTML = pending.map(item => `
    <div class="acq-card" data-id="${_esc(item.id)}">
      <div class="acq-card-head">
        <span class="acq-card-pid">${_esc(item.manufacturer)} · ${_esc(item.product_id)}</span>
        ${item.doc_type ? `<span class="lib-type-badge">${_esc(item.doc_type)}</span>` : ''}
      </div>
      ${item.reason ? `<div class="acq-card-reason">${_esc(item.reason)}</div>` : ''}
      ${item.source_url ? `<div class="acq-card-url"><a href="${_esc(item.source_url)}" target="_blank" rel="noopener">${_esc(item.source_url)}</a></div>` : ''}
      <div class="acq-card-actions">
        <button class="wb-chat-btn acq-approve-btn" data-id="${_esc(item.id)}">Approve</button>
        <button class="wb-del-btn acq-reject-btn" data-id="${_esc(item.id)}">Reject</button>
      </div>
    </div>
  `).join('');

  acqPendingList.querySelectorAll('.acq-approve-btn').forEach(btn => {
    btn.addEventListener('click', () => approveAcqItem(btn.dataset.id));
  });
  acqPendingList.querySelectorAll('.acq-reject-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const res = await fetch(`/api/acquisition/queue/${btn.dataset.id}/reject`, { method: 'PATCH' });
      if (res.ok) { await loadAcquisitionQueue(); }
      else showErrorBar('Failed to reject item.');
    });
  });
}

function renderAcqActive() {
  // Show in_progress + complete + failed items not yet in the live cards map.
  const active = acqQueue.filter(i =>
    ['approved','in_progress','complete','failed'].includes(i.status)
  );
  if (active.length === 0 && acqActiveCards.size === 0) {
    acqActiveList.innerHTML = '<p class="acq-empty-msg">No active jobs.</p>';
    return;
  }
  // Render cards for historical items not already in the live map.
  active.forEach(item => {
    if (acqActiveCards.has(item.id)) return;
    _addActiveCard(item.id, item.manufacturer, item.product_id, item.status,
                   item.error || null);
  });
}

function _addActiveCard(id, manufacturer, product_id, initialStatus, errorText) {
  // Remove "no active jobs" placeholder.
  const placeholder = acqActiveList.querySelector('.acq-empty-msg');
  if (placeholder) placeholder.remove();

  const card = document.createElement('div');
  card.className = 'acq-card acq-active-card';
  card.dataset.id = id;

  const statusClass = { complete: 'success', failed: 'error', in_progress: 'running' }[initialStatus] || 'running';
  const statusText  = { complete: '✓ Complete', failed: '✗ Failed', in_progress: '⟳ Running',
                        approved: '⟳ Starting…' }[initialStatus] || initialStatus;

  card.innerHTML = `
    <div class="acq-card-head">
      <span class="acq-card-pid">${_esc(manufacturer)} · ${_esc(product_id)}</span>
      <span class="acq-status-badge ${statusClass}">${statusText}</span>
    </div>
    <div class="acq-card-files"></div>
    ${errorText ? `<div class="acq-card-error">${_esc(errorText)}</div>` : ''}
    ${initialStatus === 'failed'
      ? `<button class="icon-btn acq-retry-btn" data-id="${_esc(id)}">↻ Retry</button>`
      : ''}
  `;

  acqActiveList.insertBefore(card, acqActiveList.firstChild);

  const filesEl  = card.querySelector('.acq-card-files');
  const statusEl = card.querySelector('.acq-status-badge');
  acqActiveCards.set(id, { card, filesEl, statusEl });

  card.querySelectorAll('.acq-retry-btn').forEach(btn => {
    btn.addEventListener('click', () => retryAcqItem(btn.dataset.id));
  });
}

function _updateActiveCard(id, status, extra = {}) {
  const entry = acqActiveCards.get(id);
  if (!entry) return;
  const { card, filesEl, statusEl } = entry;

  if (status === 'running') {
    statusEl.className = 'acq-status-badge running';
    statusEl.textContent = extra.message ? `⟳ ${extra.message}` : '⟳ Running';
  } else if (status === 'file') {
    const li = document.createElement('div');
    li.className = 'acq-file-item';
    li.textContent = `↓ ${extra.filename || ''}`;
    filesEl.appendChild(li);
  } else if (status === 'file_error') {
    const li = document.createElement('div');
    li.className = 'acq-file-item error';
    li.textContent = `✗ ${extra.filename || ''}: ${extra.error || ''}`;
    filesEl.appendChild(li);
  } else if (status === 'complete') {
    statusEl.className = 'acq-status-badge success';
    statusEl.textContent = `✓ Complete (${extra.files_added ?? 0} file${extra.files_added !== 1 ? 's' : ''})`;
  } else if (status === 'error') {
    statusEl.className = 'acq-status-badge error';
    statusEl.textContent = '✗ Failed';
    const errEl = document.createElement('div');
    errEl.className = 'acq-card-error';
    errEl.textContent = extra.error || 'Unknown error';
    card.appendChild(errEl);
    const retryBtn = document.createElement('button');
    retryBtn.className = 'icon-btn acq-retry-btn';
    retryBtn.textContent = '↻ Retry';
    retryBtn.dataset.id = id;
    retryBtn.addEventListener('click', () => retryAcqItem(id));
    card.appendChild(retryBtn);
  } else if (status === 'escalated') {
    statusEl.className = 'acq-status-badge warning';
    statusEl.textContent = extra.auto ? '☁ Escalated (auto)' : '☁ Escalated';
    const msgEl = document.createElement('div');
    msgEl.className = 'acq-card-error';
    msgEl.textContent = extra.message || 'No docs found — queued for cloud escalation.';
    card.appendChild(msgEl);
    const escBtn = document.createElement('button');
    escBtn.className = 'icon-btn';
    escBtn.textContent = '→ View Escalation';
    escBtn.addEventListener('click', () => {
      document.querySelector('.lib-sub-btn[data-subtab="escalation"]')?.click();
    });
    card.appendChild(escBtn);
  }
}

async function approveAcqItem(id) {
  const res = await fetch(`/api/acquisition/queue/${id}/approve`, { method: 'PATCH' });
  if (!res.ok) { showErrorBar('Failed to approve item.'); return; }
  // Find the item in acqQueue for card creation.
  const item = acqQueue.find(i => i.id === id);
  if (item) _addActiveCard(id, item.manufacturer, item.product_id, 'approved', null);
  await loadAcquisitionQueue();
}

async function retryAcqItem(id) {
  const res = await fetch(`/api/acquisition/queue/${id}/retry`, { method: 'POST' });
  if (!res.ok) { showErrorBar('Failed to retry item.'); return; }
  const entry = acqActiveCards.get(id);
  if (entry) {
    entry.statusEl.className = 'acq-status-badge running';
    entry.statusEl.textContent = '⟳ Starting…';
    // Remove retry button if present.
    entry.card.querySelectorAll('.acq-retry-btn').forEach(b => b.remove());
    entry.card.querySelectorAll('.acq-card-error').forEach(b => b.remove());
  }
}

// SSE connection management.
function connectAcqSse() {
  if (acqSse) return;  // already connected
  acqSse = new EventSource('/api/acquisition/stream');
  acqSseDot.classList.add('connected');
  acqSseDot.title = 'SSE connected';

  acqSse.onmessage = e => {
    let event;
    try { event = JSON.parse(e.data); } catch { return; }

    const { type, id } = event;
    if (type === 'start') {
      if (!acqActiveCards.has(id)) {
        _addActiveCard(id, event.manufacturer, event.product_id, 'approved', null);
      }
      loadAcquisitionQueue();
    } else if (type === 'progress') {
      _updateActiveCard(id, 'running', event);
    } else if (type === 'file') {
      _updateActiveCard(id, 'file', event);
    } else if (type === 'file_error') {
      _updateActiveCard(id, 'file_error', event);
    } else if (type === 'complete') {
      _updateActiveCard(id, 'complete', event);
      loadAcquisitionQueue();
      loadLibrary();  // refresh library items
    } else if (type === 'error') {
      _updateActiveCard(id, 'error', event);
      loadAcquisitionQueue();
    } else if (type === 'escalated') {
      _updateActiveCard(id, 'escalated', event);
      loadAcquisitionQueue();
      setStatus(event.message || 'No docs found — escalated to cloud queue.', 'warning');
    }
  };

  acqSse.onerror = () => {
    acqSseDot.classList.remove('connected');
    acqSseDot.title = 'SSE disconnected — reconnecting…';
    acqSse.close();
    acqSse = null;
    // Reconnect after 5 s.
    setTimeout(() => {
      if (document.getElementById('lib-acquisition') &&
          !document.getElementById('lib-acquisition').hidden) {
        connectAcqSse();
      }
    }, 5000);
  };
}

function disconnectAcqSse() {
  if (!acqSse) return;
  acqSse.close();
  acqSse = null;
  acqSseDot.classList.remove('connected');
  acqSseDot.title = 'SSE disconnected';
}

// Hook into sub-tab switching to manage SSE connection.
libSubBar.addEventListener('click', e => {
  const btn = e.target.closest('.lib-sub-btn');
  if (!btn) return;
  const subtab = btn.dataset.subtab;
  if (subtab === 'acquisition') {
    loadAcquisitionQueue();
    connectAcqSse();
  } else {
    disconnectAcqSse();
  }
}, true);  // capture so it runs before the existing handler

// Request modal.
function openAcqRequestModal() {
  acqMfrInput.value      = '';
  acqProductInput.value  = '';
  acqUrlInput.value      = '';
  acqReasonInput.value   = '';
  acqDoctypeSelect.value = '';
  acqRequestBackdrop.hidden = false;
  acqRequestModal.hidden    = false;
  acqMfrInput.focus();
}
function closeAcqRequestModal() {
  acqRequestBackdrop.hidden = true;
  acqRequestModal.hidden    = true;
}

acqAddBtn.addEventListener('click', openAcqRequestModal);
acqRequestClose.addEventListener('click', closeAcqRequestModal);
acqRequestCancel.addEventListener('click', closeAcqRequestModal);
acqRequestBackdrop.addEventListener('click', closeAcqRequestModal);

acqRequestSubmit.addEventListener('click', async () => {
  const mfr = acqMfrInput.value.trim();
  const pid = acqProductInput.value.trim();
  if (!mfr) { acqMfrInput.focus(); return; }
  if (!pid) { acqProductInput.focus(); return; }
  const body = {
    manufacturer: mfr,
    product_id:   pid,
    doc_type:     acqDoctypeSelect.value || null,
    source_url:   acqUrlInput.value.trim() || null,
    reason:       acqReasonInput.value.trim() || null,
  };
  const res = await fetch('/api/acquisition/queue', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (res.ok) {
    closeAcqRequestModal();
    await loadAcquisitionQueue();
  } else {
    const e = await res.json().catch(() => ({}));
    showErrorBar(e.detail || 'Failed to add to queue.');
  }
});

acqMfrInput.addEventListener('keydown',     e => { if (e.key === 'Enter') acqProductInput.focus(); });
acqProductInput.addEventListener('keydown', e => { if (e.key === 'Enter') acqRequestSubmit.click(); });

acqApproveAllBtn.addEventListener('click', async () => {
  const pending = acqQueue.filter(i => i.status === 'pending_approval');
  for (const item of pending) {
    await approveAcqItem(item.id);
  }
});

// ── Escalation ────────────────────────────────────────────────
const escActiveList    = document.getElementById('esc-active-list');
const escPendingList   = document.getElementById('esc-pending-list');
const escSseDot        = document.getElementById('esc-sse-dot');
const escApproveAllBtn = document.getElementById('esc-approve-all-btn');
const escBadge         = document.getElementById('esc-badge');
const escProviderLabel = document.getElementById('esc-provider-label');

let escQueue = [];
let escSse   = null;
const escActiveCards = new Map();

async function loadEscalationQueue() {
  const r = await fetch('/api/escalation/queue').catch(() => null);
  escQueue = r?.ok ? await r.json() : [];
  renderEscPending();
  renderEscActive();
  _updateEscBadge();
}

function _updateEscBadge() {
  const count = escQueue.filter(i => i.status === 'pending_approval').length;
  if (count > 0) {
    escBadge.textContent = count;
    escBadge.hidden = false;
  } else {
    escBadge.hidden = true;
  }
}

function _escTruncate(text, n = 120) {
  return text.length > n ? text.slice(0, n) + '…' : text;
}

function renderEscPending() {
  const pending = escQueue.filter(i => i.status === 'pending_approval');
  if (pending.length === 0) {
    escPendingList.innerHTML = '<p class="acq-empty-msg">No items waiting for approval.</p>';
    escApproveAllBtn.disabled = true;
    return;
  }
  const publicCount = pending.filter(i => !i.has_client_docs).length;
  escApproveAllBtn.disabled = publicCount === 0;

  escPendingList.innerHTML = pending.map(item => `
    <div class="acq-card" data-id="${_esc(item.id)}">
      <div class="acq-card-head">
        <span class="acq-card-pid">${_esc(_escTruncate(item.query_text, 60))}</span>
        ${item.has_client_docs
          ? '<span class="esc-client-badge">client data</span>'
          : '<span class="esc-public-badge">public</span>'}
      </div>
      <div class="acq-card-reason">${_esc(_escTruncate(item.query_text))}</div>
      <div class="acq-card-actions">
        <button class="wb-chat-btn esc-approve-btn" data-id="${_esc(item.id)}">Approve</button>
        <button class="wb-del-btn esc-reject-btn"   data-id="${_esc(item.id)}">Reject</button>
      </div>
    </div>
  `).join('');

  escPendingList.querySelectorAll('.esc-approve-btn').forEach(btn => {
    btn.addEventListener('click', () => approveEscItem(btn.dataset.id));
  });
  escPendingList.querySelectorAll('.esc-reject-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const res = await fetch(`/api/escalation/queue/${btn.dataset.id}/reject`, { method: 'PATCH' });
      if (res.ok) await loadEscalationQueue();
      else showErrorBar('Failed to reject escalation.');
    });
  });
}

function renderEscActive() {
  const active = escQueue.filter(i =>
    ['approved','in_progress','complete','failed'].includes(i.status)
  );
  if (active.length === 0 && escActiveCards.size === 0) {
    escActiveList.innerHTML = '<p class="acq-empty-msg">No escalations yet.</p>';
    return;
  }
  active.forEach(item => {
    if (escActiveCards.has(item.id)) return;
    _addEscCard(item.id, item.query_text, item.status, item.response || null, item.error || null);
  });
}

function _addEscCard(id, queryText, initialStatus, response, errorText) {
  const placeholder = escActiveList.querySelector('.acq-empty-msg');
  if (placeholder) placeholder.remove();

  const card = document.createElement('div');
  card.className = 'acq-card esc-card';
  card.dataset.id = id;

  const statusClass = { complete: 'success', failed: 'error', in_progress: 'running' }[initialStatus] || 'running';
  const statusText  = { complete: '✓ Complete', failed: '✗ Failed', in_progress: '⟳ Running',
                        approved: '⟳ Starting…' }[initialStatus] || initialStatus;

  card.innerHTML = `
    <div class="acq-card-head">
      <span class="acq-card-pid">${_esc(_escTruncate(queryText, 70))}</span>
      <span class="acq-status-badge ${statusClass}">${statusText}</span>
    </div>
    ${response ? `<div class="esc-response">${_esc(response)}</div>` : '<div class="esc-response-placeholder"></div>'}
    ${errorText ? `<div class="acq-card-error">${_esc(errorText)}</div>` : ''}
    ${initialStatus === 'failed'
      ? `<button class="icon-btn acq-retry-btn" data-id="${_esc(id)}">↻ Retry</button>`
      : ''}
  `;

  escActiveList.insertBefore(card, escActiveList.firstChild);
  const statusEl     = card.querySelector('.acq-status-badge');
  const responseEl   = card.querySelector('.esc-response, .esc-response-placeholder');
  escActiveCards.set(id, { card, statusEl, responseEl });

  card.querySelectorAll('.acq-retry-btn').forEach(btn => {
    btn.addEventListener('click', () => retryEscItem(btn.dataset.id));
  });
}

function _updateEscCard(id, status, extra = {}) {
  const entry = escActiveCards.get(id);
  if (!entry) return;
  const { card, statusEl, responseEl } = entry;

  if (status === 'thinking') {
    statusEl.className = 'acq-status-badge running';
    statusEl.textContent = `⟳ ${extra.message || 'Thinking…'}`;
  } else if (status === 'complete') {
    statusEl.className = 'acq-status-badge success';
    statusEl.textContent = extra.cached ? '✓ Complete (cached)' : '✓ Complete';
    if (responseEl) {
      responseEl.className = 'esc-response';
      responseEl.textContent = extra.response || '';
    }
  } else if (status === 'error') {
    statusEl.className = 'acq-status-badge error';
    statusEl.textContent = '✗ Failed';
    const errEl = document.createElement('div');
    errEl.className = 'acq-card-error';
    errEl.textContent = extra.error || 'Unknown error';
    card.appendChild(errEl);
    const retryBtn = document.createElement('button');
    retryBtn.className = 'icon-btn acq-retry-btn';
    retryBtn.textContent = '↻ Retry';
    retryBtn.dataset.id = id;
    retryBtn.addEventListener('click', () => retryEscItem(id));
    card.appendChild(retryBtn);
  }
}

async function approveEscItem(id) {
  const res = await fetch(`/api/escalation/queue/${id}/approve`, { method: 'PATCH' });
  if (!res.ok) { showErrorBar('Failed to approve escalation.'); return; }
  const item = escQueue.find(i => i.id === id);
  if (item) _addEscCard(id, item.query_text, 'approved', null, null);
  await loadEscalationQueue();
}

async function retryEscItem(id) {
  const res = await fetch(`/api/escalation/queue/${id}/retry`, { method: 'POST' });
  if (!res.ok) { showErrorBar('Failed to retry escalation.'); return; }
  const entry = escActiveCards.get(id);
  if (entry) {
    entry.statusEl.className = 'acq-status-badge running';
    entry.statusEl.textContent = '⟳ Starting…';
    entry.card.querySelectorAll('.acq-retry-btn').forEach(b => b.remove());
    entry.card.querySelectorAll('.acq-card-error').forEach(b => b.remove());
  }
}

function connectEscSse() {
  if (escSse) return;
  escSse = new EventSource('/api/escalation/stream');
  escSseDot.classList.add('connected');
  escSseDot.title = 'SSE connected';

  escSse.onmessage = e => {
    let event;
    try { event = JSON.parse(e.data); } catch { return; }
    const { type, id } = event;
    if (type === 'start') {
      if (!escActiveCards.has(id)) _addEscCard(id, event.query_text, 'approved', null, null);
      loadEscalationQueue();
    } else if (type === 'thinking') {
      _updateEscCard(id, 'thinking', event);
    } else if (type === 'complete') {
      _updateEscCard(id, 'complete', event);
      loadEscalationQueue();
    } else if (type === 'error') {
      _updateEscCard(id, 'error', event);
      loadEscalationQueue();
    }
  };

  escSse.onerror = () => {
    escSseDot.classList.remove('connected');
    escSseDot.title = 'SSE disconnected — reconnecting…';
    escSse.close();
    escSse = null;
    setTimeout(() => {
      if (!document.getElementById('lib-escalation').hidden) connectEscSse();
    }, 5000);
  };
}

function disconnectEscSse() {
  if (!escSse) return;
  escSse.close();
  escSse = null;
  escSseDot.classList.remove('connected');
  escSseDot.title = 'SSE disconnected';
}

// Hook into sub-tab switching.
libSubBar.addEventListener('click', e => {
  const btn = e.target.closest('.lib-sub-btn');
  if (!btn) return;
  const subtab = btn.dataset.subtab;
  if (subtab === 'escalation') {
    loadEscalationQueue();
    connectEscSse();
  } else {
    disconnectEscSse();
  }
}, true);

escApproveAllBtn.addEventListener('click', async () => {
  const pending = escQueue.filter(i => i.status === 'pending_approval' && !i.has_client_docs);
  for (const item of pending) await approveEscItem(item.id);
});

// Hook into sub-tab switching to load connections on first visit.
libSubBar.addEventListener('click', e => {
  const btn = e.target.closest('.lib-sub-btn');
  if (!btn) return;
  if (btn.dataset.subtab === 'connections') loadConnections();
}, true);

// Poll for pending escalation badge while library view is visible.
setInterval(() => {
  if (!document.getElementById('library-view').hidden) {
    fetch('/api/escalation/queue?status=pending_approval')
      .then(r => r.ok ? r.json() : [])
      .then(items => {
        const count = items.length;
        if (count > 0) { escBadge.textContent = count; escBadge.hidden = false; }
        else escBadge.hidden = true;
      })
      .catch(() => {});
  }
}, 10000);

// ── Connections ───────────────────────────────────────────────
const connList = document.getElementById('conn-list');

// Mutable state: connection type → { config (with password masked), enabled }
let _connections = [];

async function loadConnections() {
  const r = await fetch('/api/connections').catch(() => null);
  _connections = r?.ok ? await r.json() : [];
  renderConnections();
}

function renderConnections() {
  if (_connections.length === 0) {
    connList.innerHTML = '<p class="acq-empty-msg">No connections configured.</p>';
    return;
  }
  connList.innerHTML = '';
  for (const conn of _connections) {
    connList.appendChild(_buildConnCard(conn));
  }
}

function _buildConnCard(conn) {
  const card = document.createElement('div');
  card.className = 'conn-card';
  card.dataset.type = conn.type;

  const enabledClass = conn.enabled ? 'conn-status-on' : 'conn-status-off';
  const enabledText  = conn.enabled ? 'Enabled' : 'Disabled';

  // Build config fields HTML
  const fieldsHtml = conn.fields.map(f => {
    const val = conn.config[f.key] ?? '';
    if (f.type === 'bool') {
      return `<div class="conn-field-row">
        <label>${_esc(f.label)}</label>
        <input type="checkbox" class="conn-field" data-key="${_esc(f.key)}"
          ${val ? 'checked' : ''} />
      </div>`;
    }
    if (f.type === 'select' && f.options) {
      const opts = f.options.map(o =>
        `<option value="${_esc(o)}" ${o === val ? 'selected' : ''}>${_esc(o)}</option>`
      ).join('');
      return `<div class="conn-field-row">
        <label>${_esc(f.label)}</label>
        <select class="conn-field" data-key="${_esc(f.key)}">${opts}</select>
      </div>`;
    }
    if (f.type === 'password') {
      return `<div class="conn-field-row">
        <label>${_esc(f.label)}</label>
        <input type="password" class="conn-field" data-key="${_esc(f.key)}"
          placeholder="Enter password…" autocomplete="new-password" />
      </div>`;
    }
    return `<div class="conn-field-row">
      <label>${_esc(f.label)}</label>
      <input type="${_esc(f.type === 'number' ? 'number' : 'text')}"
        class="conn-field" data-key="${_esc(f.key)}"
        value="${_esc(String(val))}"
        placeholder="${_esc(f.label)}" />
    </div>`;
  }).join('');

  const indexBtnHtml = conn.type === 'mfiles'
    ? `<button class="wb-chat-btn conn-index-btn" ${conn.enabled ? '' : 'disabled'} title="Download and index all documents from the M-Files vault">Index Vault</button>`
    : '';

  card.innerHTML = `
    <div class="conn-card-head">
      <div class="conn-card-title">
        <span class="conn-label">${_esc(conn.label)}</span>
        <span class="conn-status ${enabledClass}">${enabledText}</span>
      </div>
      <p class="conn-desc">${_esc(conn.description)}</p>
    </div>
    <div class="conn-fields">${fieldsHtml}</div>
    <div class="conn-card-foot">
      <button class="icon-btn conn-test-btn">Test</button>
      <button class="icon-btn conn-save-btn">Save</button>
      <button class="${conn.enabled ? 'wb-del-btn conn-disable-btn' : 'wb-chat-btn conn-enable-btn'}">
        ${conn.enabled ? 'Disable' : 'Enable'}
      </button>
      ${indexBtnHtml}
      <span class="conn-test-result"></span>
    </div>
  `;

  // Try to pre-fill from env vars if config is empty.
  if (!conn.config.host && !conn.config.vault) {
    fetch(`/api/connections/${conn.type}/env-hint`)
      .then(r => r.ok ? r.json() : null)
      .then(hint => {
        if (!hint?.has_env) return;
        card.querySelectorAll('.conn-field').forEach(input => {
          const key = input.dataset.key;
          if (hint.config[key] && input.type !== 'password') {
            input.value = hint.config[key];
          }
        });
      })
      .catch(() => {});
  }

  const testResult = card.querySelector('.conn-test-result');

  card.querySelector('.conn-save-btn').addEventListener('click', async () => {
    const config = _readConnFields(card, conn.fields);
    const res = await fetch(`/api/connections/${conn.type}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ config }),
    });
    if (res.ok) {
      testResult.className = 'conn-test-result ok';
      testResult.textContent = 'Saved.';
    } else {
      testResult.className = 'conn-test-result fail';
      testResult.textContent = 'Save failed.';
    }
  });

  card.querySelector('.conn-test-btn').addEventListener('click', async () => {
    testResult.className = 'conn-test-result';
    testResult.textContent = 'Testing…';
    const res = await fetch(`/api/connections/${conn.type}/test`, { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (data.ok) {
      testResult.className = 'conn-test-result ok';
      const info = [data.vault_name, data.server_version].filter(Boolean).join(' · ');
      testResult.textContent = `✓ Connected${info ? ' — ' + info : ''}`;
    } else {
      testResult.className = 'conn-test-result fail';
      testResult.textContent = `✗ ${data.error || 'Connection failed'}`;
    }
  });

  const enableBtn  = card.querySelector('.conn-enable-btn');
  const disableBtn = card.querySelector('.conn-disable-btn');

  if (enableBtn) {
    enableBtn.addEventListener('click', async () => {
      const res = await fetch(`/api/connections/${conn.type}/enable`, { method: 'PATCH' });
      if (res.ok) await loadConnections();
      else showErrorBar('Failed to enable connection.');
    });
  }
  if (disableBtn) {
    disableBtn.addEventListener('click', async () => {
      const res = await fetch(`/api/connections/${conn.type}/disable`, { method: 'PATCH' });
      if (res.ok) await loadConnections();
      else showErrorBar('Failed to disable connection.');
    });
  }

  const indexBtn = card.querySelector('.conn-index-btn');
  if (indexBtn) {
    indexBtn.addEventListener('click', async () => {
      indexBtn.disabled = true;
      indexBtn.textContent = 'Starting…';
      testResult.className = 'conn-test-result';
      testResult.textContent = '';

      const startRes = await fetch('/api/connections/mfiles/index', { method: 'POST' });
      if (!startRes.ok) {
        const err = await startRes.json().catch(() => ({}));
        testResult.className = 'conn-test-result fail';
        testResult.textContent = err.detail || 'Failed to start indexer.';
        indexBtn.disabled = false;
        indexBtn.textContent = 'Index Vault';
        return;
      }

      setStatus('M-Files vault index started…', 'busy');
      let indexed = 0;

      const es = new EventSource('/api/connections/mfiles/index/stream');
      es.onmessage = evt => {
        const data = JSON.parse(evt.data);
        if (data.type === 'file') {
          indexed++;
          testResult.textContent = `Indexed ${indexed} file${indexed !== 1 ? 's' : ''}…`;
          setStatus(`M-Files: indexed ${indexed} file${indexed !== 1 ? 's' : ''}…`, 'busy');
        } else if (data.type === 'complete') {
          es.close();
          indexBtn.disabled = false;
          indexBtn.textContent = 'Index Vault';
          testResult.className = 'conn-test-result ok';
          testResult.textContent = `✓ Done — ${data.indexed} indexed, ${data.skipped} skipped${data.errors ? ', ' + data.errors + ' errors' : ''}`;
          setStatus(`M-Files vault index complete — ${data.indexed} files indexed.`, 'info');
        } else if (data.type === 'error') {
          es.close();
          indexBtn.disabled = false;
          indexBtn.textContent = 'Index Vault';
          testResult.className = 'conn-test-result fail';
          testResult.textContent = `✗ ${data.error}`;
          setStatus(data.error, 'error');
        }
      };
      es.onerror = () => {
        es.close();
        indexBtn.disabled = false;
        indexBtn.textContent = 'Index Vault';
        if (!testResult.textContent.startsWith('✓')) {
          testResult.className = 'conn-test-result fail';
          testResult.textContent = 'Stream disconnected.';
        }
      };
    });
  }

  return card;
}

function _readConnFields(card, fields) {
  const config = {};
  card.querySelectorAll('.conn-field').forEach(input => {
    const key = input.dataset.key;
    const field = fields.find(f => f.key === key);
    if (!field) return;
    if (field.type === 'bool') {
      config[key] = input.checked;
    } else if (field.type === 'number') {
      config[key] = input.value ? parseInt(input.value, 10) : null;
    } else {
      config[key] = input.value.trim();
    }
  });
  return config;
}

// ── System Prompts ────────────────────────────────────────────
const spSelect      = document.getElementById('sp-select');
const spEditBtn     = document.getElementById('sp-edit-btn');
const spNewBtn      = document.getElementById('sp-new-btn');
const spEditor      = document.getElementById('sp-editor');
const spNameInput   = document.getElementById('sp-name-input');
const spContentInput = document.getElementById('sp-content-input');
const spSaveBtn     = document.getElementById('sp-save-btn');
const spCancelBtn   = document.getElementById('sp-cancel-btn');
const spDeleteBtn   = document.getElementById('sp-delete-btn');

let _systemPrompts  = [];  // cached list from API
let _editingSpId    = null; // null = creating new, number = editing existing

async function fetchSystemPrompts() {
  try {
    const res = await fetch('/api/system-prompts');
    if (!res.ok) return;
    _systemPrompts = await res.json();
    renderSpSelect();
  } catch (_) {}
}

function renderSpSelect() {
  const prev = spSelect.value;
  spSelect.innerHTML = '<option value="">(none)</option>';
  for (const sp of _systemPrompts) {
    const opt = document.createElement('option');
    opt.value = sp.id;
    opt.textContent = sp.name;
    spSelect.appendChild(opt);
  }
  // Restore selection if it still exists
  if (prev && _systemPrompts.some(s => String(s.id) === prev)) {
    spSelect.value = prev;
  }
  spEditBtn.disabled = !spSelect.value;
}

async function applySpToConversation(spId) {
  if (!currentConvId) return;
  try {
    await fetch(`/api/conversations/${currentConvId}/system-prompt`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ system_prompt_id: spId || null }),
    });
    await fetchConversations();
  } catch (_) {}
}

spSelect.addEventListener('change', async () => {
  spEditBtn.disabled = !spSelect.value;
  const spId = spSelect.value ? parseInt(spSelect.value, 10) : null;
  await applySpToConversation(spId);
});

spEditBtn.addEventListener('click', () => {
  const sp = _systemPrompts.find(s => String(s.id) === spSelect.value);
  if (!sp) return;
  _editingSpId = sp.id;
  spNameInput.value    = sp.name;
  spContentInput.value = sp.content;
  spDeleteBtn.hidden   = false;
  spEditor.hidden      = false;
  spNameInput.focus();
});

spNewBtn.addEventListener('click', () => {
  _editingSpId = null;
  spNameInput.value    = '';
  spContentInput.value = '';
  spDeleteBtn.hidden   = true;
  spEditor.hidden      = false;
  spNameInput.focus();
});

spCancelBtn.addEventListener('click', () => {
  spEditor.hidden = true;
});

spSaveBtn.addEventListener('click', async () => {
  const name    = spNameInput.value.trim();
  const content = spContentInput.value.trim();
  if (!name)    { spNameInput.focus();    return; }
  if (!content) { spContentInput.focus(); return; }

  spSaveBtn.disabled    = true;
  spSaveBtn.textContent = 'Saving…';
  try {
    let res;
    if (_editingSpId) {
      res = await fetch(`/api/system-prompts/${_editingSpId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, content }),
      });
    } else {
      res = await fetch('/api/system-prompts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, content }),
      });
    }
    if (res.ok) {
      const saved = await res.json();
      spEditor.hidden = true;
      await fetchSystemPrompts();
      spSelect.value = saved.id || _editingSpId;
      spEditBtn.disabled = !spSelect.value;
      setStatus(`System prompt "${name}" saved.`, 'info');
      if (!_editingSpId) {
        await applySpToConversation(saved.id);
      }
    } else {
      const err = await res.json().catch(() => ({}));
      setStatus(err.detail || 'Failed to save system prompt.', 'error');
    }
  } catch {
    setStatus('Failed to save system prompt — network error.', 'error');
  } finally {
    spSaveBtn.disabled    = false;
    spSaveBtn.textContent = 'Save';
  }
});

spDeleteBtn.addEventListener('click', async () => {
  if (!_editingSpId) return;
  const sp = _systemPrompts.find(s => s.id === _editingSpId);
  if (!confirm(`Delete system prompt "${sp?.name}"?`)) return;
  try {
    const res = await fetch(`/api/system-prompts/${_editingSpId}`, { method: 'DELETE' });
    if (res.ok || res.status === 204) {
      spEditor.hidden = true;
      await fetchSystemPrompts();
      spSelect.value = '';
      spEditBtn.disabled = true;
      setStatus('System prompt deleted.', 'info');
    } else {
      setStatus('Failed to delete system prompt.', 'error');
    }
  } catch {
    setStatus('Failed to delete system prompt — network error.', 'error');
  }
});

function syncSpForConversation(conv) {
  if (!conv) {
    spSelect.value = '';
    spEditBtn.disabled = true;
    return;
  }
  const spId = conv.system_prompt_id;
  spSelect.value = spId ? String(spId) : '';
  spEditBtn.disabled = !spSelect.value;
}

// ── Conversation export ────────────────────────────────────────
const exportMenu    = document.getElementById('export-menu');
const exportMdBtn   = document.getElementById('export-md-btn');
const exportJsonBtn = document.getElementById('export-json-btn');

let _exportConvId  = null;
let _exportAnchor  = null;

function openExportMenu(convId, anchorEl) {
  _exportConvId = convId;
  _exportAnchor = anchorEl;
  const rect = anchorEl.getBoundingClientRect();
  exportMenu.style.top  = (rect.bottom + window.scrollY + 4) + 'px';
  exportMenu.style.left = (rect.left  + window.scrollX)     + 'px';
  exportMenu.hidden = false;
}

function closeExportMenu() {
  exportMenu.hidden = true;
  _exportConvId = null;
}

document.addEventListener('click', e => {
  if (!exportMenu.hidden && !exportMenu.contains(e.target)) {
    closeExportMenu();
  }
});

async function triggerExport(fmt) {
  if (!_exportConvId) return;
  closeExportMenu();
  try {
    const res = await fetch(`/api/conversations/${_exportConvId}/export?format=${fmt}`);
    if (!res.ok) { setStatus('Export failed.', 'error'); return; }
    const blob = await res.blob();
    const cd   = res.headers.get('Content-Disposition') || '';
    const fnMatch = cd.match(/filename="([^"]+)"/);
    const filename = fnMatch ? fnMatch[1] : `conversation.${fmt}`;
    const url = URL.createObjectURL(blob);
    const a   = document.createElement('a');
    a.href     = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch {
    setStatus('Export failed — network error.', 'error');
  }
}

exportMdBtn.addEventListener('click',   () => triggerExport('md'));
exportJsonBtn.addEventListener('click', () => triggerExport('json'));

// ── Bootstrap ─────────────────────────────────────────────────

/**
 * Fetch runtime site config and apply public library mode if active.
 * In library mode:
 *   - Only the Library tab is visible (Chat + Workbench tabs are hidden)
 *   - Within Library, only the Browse sub-tab is shown
 *   - The sidebar (document uploads, conversations) is hidden
 *   - A read-only notice is shown in the header branding area
 */
async function applySiteConfig() {
  try {
    const res = await fetch('/api/site-config');
    if (!res.ok) return;
    const cfg = await res.json();
    if (!cfg.public_library_mode) return;

    // Hide Chat and Workbench tabs; activate Library tab
    document.querySelectorAll('.tab-btn[data-tab="chat"], .tab-btn[data-tab="workbench"]')
      .forEach(b => b.hidden = true);
    const libTabBtn = document.querySelector('.tab-btn[data-tab="library"]');
    if (libTabBtn) libTabBtn.click();

    // Hide Acquisition, Escalation, Connections sub-tabs
    ['acquisition', 'escalation', 'connections'].forEach(st => {
      const btn = document.querySelector(`.lib-sub-btn[data-subtab="${st}"]`);
      if (btn) btn.hidden = true;
    });

    // Hide sidebar and sidebar toggle
    sidebar.classList.add('collapsed');
    sidebarToggle.hidden = true;

    // Add a read-only badge next to the brand title
    const brand = document.querySelector('.brand');
    if (brand) {
      const badge = document.createElement('span');
      badge.className = 'lib-readonly-badge';
      badge.textContent = 'Public Library';
      brand.appendChild(badge);
    }
  } catch { /* non-critical */ }
}

applySiteConfig();
fetchModels().then(() => Promise.all([pollModelStatus(), pollAnalysisModelStatus()]));
fetchConversations();
fetchDocuments();
fetchSystemPrompts();
pollGpu();
setInterval(pollGpu, 3000);
pollSystem();
setInterval(pollSystem, 3000);
setInterval(pollModelStatus, 5000);
setInterval(pollAnalysisModelStatus, 5000);
pollMerllm();
setInterval(pollMerllm, 15000);
input.focus();
