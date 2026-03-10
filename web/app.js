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
const modelDot        = document.getElementById('model-dot');
const modelDotLabel   = document.getElementById('model-dot-label');
const loadModelBtn    = document.getElementById('load-model-btn');
const refreshModelsBtn = document.getElementById('refresh-models-btn');
const errorBar      = document.getElementById('error-bar');
const errorBarText  = document.getElementById('error-bar-text');
const errorBarDismiss = document.getElementById('error-bar-dismiss');
const systemPromptWrap = document.getElementById('system-prompt-wrap');
const systemPromptInput = document.getElementById('system-prompt');
const systemBtn         = document.getElementById('system-btn');

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
  input.focus();
}

newChatBtn.addEventListener('click', newChat);

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
  del.addEventListener('click', onDelete);

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
  } catch (err) {
    alert(err.message);
  } finally {
    uploadInProgress = false;
    clearInterval(gpuFastInterval);
    gpuFastInterval = null;
    placeholder.remove();
    if (conversationId) await fetchChatDocuments(conversationId);
    else await fetchDocuments();
    await pollModelStatus();
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

// ── GPU meter ─────────────────────────────────────────────────
/**
 * Polls the ``/api/gpu`` endpoint and updates the GPU meter widget.
 *
 * Updates GPU utilisation percentage, the colour-coded usage bar (green →
 * warm → hot), and the VRAM used/total readout.  Adds the ``unavailable``
 * class to the meter when the API returns an error or the GPU is not present.
 *
 * @return {Promise<void>}
 */
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
  pollModelStatus();
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

refreshModelsBtn.addEventListener('click', async () => {
  refreshModelsBtn.disabled = true;
  refreshModelsBtn.textContent = '…';
  try {
    await fetchModels();
    await pollModelStatus();
  } finally {
    refreshModelsBtn.disabled = false;
    refreshModelsBtn.textContent = '↻';
  }
});

// ── Bootstrap ─────────────────────────────────────────────────
fetchModels().then(pollModelStatus);
fetchConversations();
fetchDocuments();
pollGpu();
setInterval(pollGpu, 3000);
setInterval(pollModelStatus, 5000);
input.focus();
