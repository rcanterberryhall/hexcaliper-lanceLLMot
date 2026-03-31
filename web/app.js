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
const gpuMeters     = document.getElementById('gpu-meters');
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

// ── Tab switching ─────────────────────────────────────────────
const tabBar         = document.getElementById('tab-bar');
const chatView       = document.getElementById('chat-view');
const workbenchView  = document.getElementById('workbench-view');

tabBar.addEventListener('click', e => {
  const btn = e.target.closest('.tab-btn');
  if (!btn) return;
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  const tab = btn.dataset.tab;
  chatView.hidden      = tab !== 'chat';
  workbenchView.hidden = tab !== 'workbench';
  if (tab === 'workbench') loadWorkbench();
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
    fetch('/api/library/clients').catch(() => null),
    fetch('/api/library/projects').catch(() => null),
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
      <td><button class="wb-del-btn" data-id="${d.id}" data-name="${_esc(d.filename)}" title="Delete document">✕</button></td>`;
    wbDocTbody.appendChild(tr);
  });
}

wbDocTbody.addEventListener('click', async e => {
  const btn = e.target.closest('.wb-del-btn');
  if (!btn) return;
  if (!confirm(`Delete "${btn.dataset.name}"?\n\nThis removes it permanently from the knowledge graph.`)) return;
  const res = await fetch(`/api/documents/${btn.dataset.id}`, { method: 'DELETE' });
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

  wbUploadStatus.textContent = `Uploading ${file.name}…`;
  try {
    const res = await fetch('/api/documents', { method: 'POST', body: fd });
    if (res.ok) {
      wbUploadStatus.textContent = `✓ ${file.name} uploaded`;
      await loadWbDocs();
    } else {
      const err = await res.json().catch(() => ({}));
      wbUploadStatus.textContent = '';
      showErrorBar(err.detail || 'Upload failed.');
    }
  } catch {
    wbUploadStatus.textContent = '';
    showErrorBar('Upload failed.');
  }
  setTimeout(() => { wbUploadStatus.textContent = ''; }, 4000);
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
      const res = await fetch(`/api/library/clients/${c.id}`, { method: 'DELETE' });
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
        const res = await fetch(`/api/library/projects/${p.id}`, { method: 'DELETE' });
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
      const res = await fetch('/api/library/projects', {
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
  const res = await fetch('/api/library/clients', {
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

// ── Bootstrap ─────────────────────────────────────────────────
fetchModels().then(pollModelStatus);
fetchConversations();
fetchDocuments();
pollGpu();
setInterval(pollGpu, 3000);
pollSystem();
setInterval(pollSystem, 3000);
setInterval(pollModelStatus, 5000);
input.focus();
