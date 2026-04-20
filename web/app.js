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
const scopeBadge        = document.getElementById('scope-badge');

let currentConvId = null;
let abortController = null;
let uploadInProgress = false;
let gpuFastInterval = null;

// ── Helpers ────────────────────────────────────────────────────

/**
 * HTML-escape a value before interpolating it into an innerHTML template.
 *
 * Several innerHTML templates below (scope badge, sources block) interpolate
 * server-provided strings like document names, chunk text, and concept
 * labels. Those can contain angle brackets, ampersands, or quotes that would
 * otherwise break the DOM or introduce XSS — so every interpolation goes
 * through ``esc()``. Null/undefined are coerced to an empty string so
 * callers don't need to pre-check.
 *
 * @param {*} value - The value to escape.
 * @return {string} The HTML-escaped string.
 */
function esc(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// ── Scope badge ────────────────────────────────────────────────

/**
 * Refresh the scope badge in the chat header.
 *
 * Reads the active project from chatView.dataset (set when a workbench project
 * is opened in chat) and fetches the document count for that scope via the
 * /api/documents endpoint.
 */
async function updateScopeBadge() {
  if (!scopeBadge) return;
  const projectId   = document.getElementById('chat-view')?.dataset?.projectId;
  const projectName = document.getElementById('chat-view')?.dataset?.projectName;

  let scopeLabel, queryParam;
  if (projectId) {
    scopeLabel  = `Project: ${projectName || projectId}`;
    queryParam  = `?project_id=${encodeURIComponent(projectId)}`;
  } else {
    scopeLabel = 'Global';
    queryParam = '';
  }

  let count = '…';
  try {
    const res  = await fetch(`/api/documents${queryParam}`);
    const docs = res.ok ? await res.json() : [];
    count = docs.length;
  } catch (_) { count = '?'; }

  const warn    = count === 0 && !!projectId;
  scopeBadge.className = 'scope-badge-pill' + (warn ? ' scope-warn' : '');
  scopeBadge.innerHTML =
    `<span class="scope-type">${esc(scopeLabel)}</span>` +
    `<span class="scope-count">${count} doc${count !== 1 ? 's' : ''}</span>` +
    (warn ? '<span class="scope-warn-icon" title="No documents in this scope">⚠</span>' : '');
}

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

/**
 * Appends a "Deep Analysis →" button to an AI message, submitting the
 * exchange to merLLM's batch API for low-priority background processing.
 * Polls for completion and renders the result as a new message.
 */
function addDeepAnalysisBtn(inner, bubble, convId, queryText) {
  const btn = document.createElement('button');
  btn.className = 'deep-btn';
  btn.textContent = 'Deep Analysis →';
  btn.title = 'Submit for deep analysis via merLLM batch queue';
  btn.addEventListener('click', async () => {
    if (btn.disabled) return;
    btn.disabled = true;
    btn.textContent = 'Queuing…';
    const responseText = bubble.innerText || bubble.textContent;
    const prompt = [
      'Please provide a deep, thorough analysis of the following conversation exchange.',
      'Explore implications, nuances, alternative perspectives, and any aspects that warrant deeper investigation.',
      'Do not summarize — analyze.\n',
      'User:',
      queryText,
      '\nAssistant:',
      responseText,
    ].join('\n');
    try {
      const res = await fetch('/api/batch/submit', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source_app: 'lancellmot', prompt }),
      });
      if (!res.ok) throw new Error(((await res.json().catch(() => ({}))).detail) || res.status);
      const { id } = await res.json();
      btn.textContent = '⏳ Queued';
      setStatus('Deep analysis queued — will run when a GPU slot is available.', 'info');
      _pollDeepAnalysis(id, btn);
    } catch (e) {
      btn.disabled = false;
      btn.textContent = 'Deep Analysis →';
      setStatus('Deep analysis submit failed: ' + e.message, 'error');
    }
  });
  inner.appendChild(btn);
}

function _pollDeepAnalysis(jobId, btn) {
  const MAX = 360; // 30 min at 5s intervals
  let n = 0;
  const iv = setInterval(async () => {
    if (++n > MAX) {
      clearInterval(iv);
      btn.textContent = 'Timed out';
      return;
    }
    try {
      const s = await fetch(`/api/batch/status/${jobId}`).then(r => r.json());
      if (s.status === 'completed') {
        clearInterval(iv);
        const data = await fetch(`/api/batch/results/${jobId}`).then(r => r.json());
        btn.textContent = '✓ Done';
        createMessage('ai', data.result || '(no result)', { modelTag: 'deep analysis' });
        scrollToBottom();
        setStatus('Deep analysis complete.', 'info');
      } else if (s.status === 'failed') {
        clearInterval(iv);
        btn.disabled = false;
        btn.textContent = 'Failed';
        setStatus('Deep analysis failed: ' + (s.error || 'unknown'), 'error');
      } else {
        btn.textContent = s.status === 'running' ? '⚙ Running…' : '⏳ Queued';
      }
    } catch (_) { /* network hiccup — keep polling */ }
  }, 5000);
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
/**
 * Build a collapsible Sources attribution block showing retrieved documents
 * and graph nodes. Inserted below the assistant message bubble.
 *
 * @param {Object[]} documents - [{name, chunk, score, anchor}]
 *   `anchor` is the structural citation label from the chunker (#31),
 *   e.g. "§4.3 Architectural constraints" or "## Verification strategy".
 *   Empty string for fixed-window-fallback chunks or pre-#31 documents.
 * @param {Object[]} graphNodes - [{entity, relation, score}]
 * @returns {HTMLElement} The root element of the sources block.
 */
function buildSourcesBlock(documents, graphNodes) {
  const block = document.createElement('div');
  block.className = 'sources-block collapsed';

  const header = document.createElement('div');
  header.className = 'sources-header';
  const total = documents.length + graphNodes.length;
  header.innerHTML = `<span class="sources-chevron">▸</span><span class="sources-label">Sources (${total})</span>`;
  header.addEventListener('click', () => block.classList.toggle('collapsed'));

  const content = document.createElement('div');
  content.className = 'sources-content';

  if (documents.length === 0 && graphNodes.length === 0) {
    content.innerHTML = '<span class="sources-empty">No documents matched this query.</span>';
  } else {
    if (documents.length > 0) {
      const sec = document.createElement('div');
      sec.className = 'sources-section';
      sec.innerHTML = '<div class="sources-section-title">Documents</div>' +
        documents.map(d => {
          const pct = Math.round((d.score || 0) * 100);
          const anchorHtml = d.anchor
            ? `<div class="sources-anchor">${esc(d.anchor)}</div>`
            : '';
          return `<div class="sources-item">
            <span class="sources-doc-name">${esc(d.name)}</span>
            <span class="sources-score">${pct}%</span>
            ${anchorHtml}
            <div class="sources-chunk">${esc(d.chunk)}…</div>
          </div>`;
        }).join('');
      content.appendChild(sec);
    }
    if (graphNodes.length > 0) {
      const sec = document.createElement('div');
      sec.className = 'sources-section';
      sec.innerHTML = '<div class="sources-section-title">Graph</div>' +
        graphNodes.map(n =>
          `<div class="sources-item sources-graph-item">
            <span class="sources-entity">${esc(n.entity)}</span>
            <span class="sources-relation">${esc(n.relation)}</span>
          </div>`
        ).join('');
      content.appendChild(sec);
    }
  }

  block.appendChild(header);
  block.appendChild(content);
  return block;
}

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
    if (chatDocList.children.length > 0) {
      showCopyrightNotice();
    } else {
      input.focus();
    }
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

// ── Copyright acknowledgement ─────────────────────────────────
// Shown on page load, on every newChat(), and whenever a conversation with
// attached documents is opened — reminding the user of their obligations
// before they act on potentially copyrighted material.
const copyrightBackdrop = document.getElementById('copyright-backdrop');
const copyrightModal    = document.getElementById('copyright-modal');
const copyrightAckBtn   = document.getElementById('copyright-ack-btn');

function showCopyrightNotice() {
  copyrightBackdrop.hidden = false;
  copyrightModal.hidden    = false;
  input.disabled           = true;
  sendBtn.disabled         = true;
  copyrightAckBtn.focus();
}

function acknowledgeCopyright() {
  copyrightBackdrop.hidden = true;
  copyrightModal.hidden    = true;
  input.disabled           = false;
  sendBtn.disabled         = false;
  input.focus();
}

copyrightAckBtn.addEventListener('click', acknowledgeCopyright);

showCopyrightNotice();

/**
 * Resets the UI to a blank new-chat state.
 *
 * Clears the chat window, empties the chat-document list, deselects any
 * active sidebar item, hides the chat-docs section, and focuses the input.
 * Requires the copyright notice to be acknowledged before the input is
 * usable (per issue #36).
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
  showCopyrightNotice();
}

newChatBtn.addEventListener('click', () => {
  newChat();
  // Clear any workbench project context
  delete chatView.dataset.projectId;
  delete chatView.dataset.projectName;
  input.placeholder = 'Ask anything…';
  updateScopeBadge();
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
    await Promise.all([pollModelStatus(), pollAnalysisModelStatus()]);
  }
}

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
        } else if (evt.type === 'sources') {
          const sb = buildSourcesBlock(evt.documents || [], evt.graph_nodes || []);
          inner.insertBefore(sb, bubble.nextSibling);
          scrollToBottom();
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
      addDeepAnalysisBtn(inner, bubble, doneData.conversation_id, message);
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
    const gpus = d.gpus || {};
    const allHealthy = Object.values(gpus).every(g => g.health === 'healthy');
    const anyFaulted = Object.values(gpus).some(g => g.health === 'faulted');
    const health = allHealthy ? 'healthy' : anyFaulted ? 'faulted' : 'degraded';
    dot.className = 'merllm-dot ' + health;
    // Show active/queued as two separate numbers so the user can see queue
    // depth even when both GPUs are saturated. "active" = jobs on a GPU
    // right now; "queued" = jobs waiting in merLLM's SQLite queue. Prior
    // build summed the two, which made a 435-deep backlog indistinguishable
    // from a quiet system with 2 in flight.
    const active = d.queue?.running ?? 0;
    const queued = d.queue?.queued  ?? 0;
    const badge  = (active > 0 || queued > 0) ? ` (${active}/${queued})` : '';
    label.textContent = 'merLLM' + badge;
    const tip = `Active ${active} / Queued ${queued} — routing: ${d.routing || 'round_robin'}`;
    label.title = tip + (d.warnings?.length ? '\n⚠ ' + d.warnings.join('\n⚠ ') : '');
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
    // Prepend "merLLM default" option asynchronously (don't block model list)
    _prependMerllmDefault(models);
  } catch (_) {}
}

async function _prependMerllmDefault(existingModels) {
  try {
    const res = await fetch('/api/merllm/default-model');
    if (!res.ok) return;
    const d = await res.json();
    if (!d.model) return;
    for (const sel of [modelSel, analysisSel]) {
      // Don't add if already present as a "merLLM default" option
      if (sel.querySelector('option[data-merllm-default]')) continue;
      const opt = document.createElement('option');
      opt.value = d.model;
      opt.textContent = `merLLM default (${d.model})`;
      opt.dataset.merllmDefault = '1';
      sel.insertBefore(opt, sel.firstChild);
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
const tabBar        = document.getElementById('tab-bar');
const chatView      = document.getElementById('chat-view');
const workbenchView = document.getElementById('workbench-view');

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
const wbUploadClass  = document.getElementById('wb-upload-classification');
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
let wbSortKey = null;
let wbSortAsc = true;
// Doc IDs currently checked in the table. Cleared whenever the underlying
// doc list reloads — the render pass drops any IDs no longer present.
const wbSelectedIds = new Set();

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
  const restricted = scope.type === 'client' || scope.type === 'project';
  wbUploadClass.value = restricted ? 'client' : 'client';
  wbUploadClass.disabled = restricted;
  wbUploadClass.querySelector('option[value="public"]').disabled = restricted;
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

_WB_DOC_TYPE_OPTIONS.forEach(([v, l]) => {
  const opt = document.createElement('option');
  opt.value = v;
  opt.textContent = l;
  if (v === 'misc') opt.selected = true;
  wbUploadType.appendChild(opt);
});

const _WB_SAFETY_CRITICAL_TYPES = ['theop', 'fmea', 'hazard_analysis', 'fat', 'sat'];
const _WB_ALL_COMPLETENESS_TYPES = [
  'theop', 'fmea', 'hazard_analysis', 'fat', 'sat',
  'standard', 'requirement', 'contract', 'correspondence',
  'plc_code', 'technical_manual', 'datasheet', 'firmware_notes', 'app_note', 'misc',
];
const _WB_TYPE_ABBREV = {
  theop: 'THEOP', fmea: 'FMEA', hazard_analysis: 'HA', fat: 'FAT', sat: 'SAT',
  standard: 'STD', requirement: 'REQ', contract: 'CON', correspondence: 'COR',
  plc_code: 'PLC', technical_manual: 'MAN', datasheet: 'DS',
  firmware_notes: 'FW', app_note: 'APP', misc: 'MISC',
};

function renderWbCompleteness() {
  const el = document.getElementById('wb-completeness');
  if (!el) return;
  if (wbScope.type !== 'project') { el.hidden = true; return; }
  el.hidden = false;
  const counts = {};
  for (const d of wbDocs) counts[d.doc_type] = (counts[d.doc_type] || 0) + 1;
  el.innerHTML = _WB_ALL_COMPLETENESS_TYPES.map(t => {
    const count = counts[t] || 0;
    const critical = _WB_SAFETY_CRITICAL_TYPES.includes(t);
    const cls = count > 0 ? 'present' : (critical ? 'critical missing' : 'missing');
    const abbrev = _WB_TYPE_ABBREV[t] || t;
    return `<span class="wb-completeness-badge ${cls}" title="${t}: ${count} document(s)">`
      + `${abbrev}${count > 0 ? ' <span class="cb-count">' + count + '</span>' : ''}</span>`;
  }).join('');
}

function sortWbRows(rows) {
  if (!wbSortKey) return rows;
  const sorted = [...rows];
  sorted.sort((a, b) => {
    let va = a[wbSortKey] ?? '';
    let vb = b[wbSortKey] ?? '';
    if (wbSortKey === 'chunk_count') return wbSortAsc ? va - vb : vb - va;
    va = String(va).toLowerCase();
    vb = String(vb).toLowerCase();
    return wbSortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
  });
  return sorted;
}

function updateSortHeaders() {
  document.querySelectorAll('.wb-table th.sortable').forEach(th => {
    const key = th.dataset.sortKey;
    const arrow = th.querySelector('.sort-arrow');
    if (arrow) arrow.remove();
    th.classList.toggle('sort-active', key === wbSortKey);
    if (key === wbSortKey) {
      const span = document.createElement('span');
      span.className = 'sort-arrow';
      span.textContent = wbSortAsc ? ' ▲' : ' ▼';
      th.appendChild(span);
    }
  });
}

function renderWbDocs() {
  renderWbCompleteness();
  updateSortHeaders();
  const filter = wbTypeFilter.value;
  const filtered = wbDocs.filter(d => !filter || d.doc_type === filter);
  const rows = sortWbRows(filtered);

  // Drop any selections that are no longer visible (scope change, filter change,
  // or deletion) so the bulk bar count always reflects reality.
  const visibleIds = new Set(rows.map(d => d.id));
  for (const id of wbSelectedIds) if (!visibleIds.has(id)) wbSelectedIds.delete(id);

  if (!rows.length) {
    const msg = wbScope.type === 'global' && !wbClients.length
      ? 'No global documents yet. Upload a standard or reference document using the + Upload button.'
      : 'No documents found for this scope.';
    wbDocTbody.innerHTML = `<tr class="wb-empty-row"><td colspan="7">${msg}</td></tr>`;
    updateWbBulkBar();
    return;
  }
  wbDocTbody.innerHTML = '';
  rows.forEach(d => {
    const tr = document.createElement('tr');
    const date = (d.created_at || '').slice(0, 10);
    const checked = wbSelectedIds.has(d.id) ? ' checked' : '';
    if (checked) tr.classList.add('wb-row-selected');
    tr.innerHTML = `
      <td class="wb-check-col"><input type="checkbox" class="wb-row-check" data-id="${d.id}"${checked} /></td>
      <td title="${_esc(d.filename)}">${_esc(d.filename)}</td>
      <td><span class="doc-type-badge">${_esc(d.doc_type)}</span></td>
      <td><span class="scope-badge ${d.scope_type}">${d.scope_type}</span></td>
      <td>${date}</td>
      <td>${d.chunk_count}</td>
      <td class="wb-actions">
        <button class="wb-download-btn" data-id="${d.id}" data-name="${_esc(d.filename)}" title="Download document">⬇</button>
        <button class="wb-chat-attach-btn" data-id="${d.id}" data-name="${_esc(d.filename)}" title="Add to current conversation">+ Chat</button>
        <button class="wb-edit-btn" data-id="${d.id}" title="Edit attributes">✎</button>
        <button class="wb-del-btn" data-id="${d.id}" data-name="${_esc(d.filename)}" title="Delete document">✕</button>
      </td>`;
    wbDocTbody.appendChild(tr);
  });
  updateWbBulkBar();
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
  const dlBtn = e.target.closest('.wb-download-btn');
  if (dlBtn) {
    const docId   = dlBtn.dataset.id;
    const docName = dlBtn.dataset.name;
    dlBtn.disabled = true;
    try {
      const res = await fetch(`/api/documents/${docId}/download`);
      if (!res.ok) {
        setStatus('Download not available — backend endpoint not yet implemented.', 'warning');
        return;
      }
      const blob = await res.blob();
      const url  = URL.createObjectURL(blob);
      const a    = document.createElement('a');
      a.href     = url;
      a.download = docName;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch {
      setStatus('Download failed — network error.', 'error');
    } finally {
      dlBtn.disabled = false;
    }
    return;
  }

  const attachBtn = e.target.closest('.wb-chat-attach-btn');
  if (attachBtn) {
    const docId   = attachBtn.dataset.id;
    const docName = attachBtn.dataset.name;

    // Ensure a conversation exists — create one if needed.
    if (!currentConvId) {
      // Switch to chat tab and start a new conversation placeholder.
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelector('.tab-btn[data-tab="chat"]').classList.add('active');
      chatView.hidden      = false;
      workbenchView.hidden = true;
      newChat();
      // Assign a provisional conversation so the attach can proceed.
      // We'll create the real conversation on first message; for now,
      // open a blank chat so the user sends a message.
      showErrorBar(`Switched to Chat. Send a message first, then attach "${docName}" again.`, 'info');
      return;
    }

    attachBtn.disabled    = true;
    attachBtn.textContent = '…';
    try {
      const res = await fetch(`/api/documents/${docId}/attach`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ conversation_id: currentConvId }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      attachBtn.textContent = '✓';
      await fetchChatDocuments();
      updateScopeBadge();
    } catch (err) {
      attachBtn.disabled    = false;
      attachBtn.textContent = '+ Chat';
      showErrorBar(`Attach failed: ${err.message}`);
    }
    return;
  }

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

document.querySelector('.wb-table thead').addEventListener('click', e => {
  const th = e.target.closest('th.sortable');
  if (!th) return;
  const key = th.dataset.sortKey;
  if (wbSortKey === key) wbSortAsc = !wbSortAsc;
  else { wbSortKey = key; wbSortAsc = true; }
  renderWbDocs();
});

// ── Bulk selection + edit ─────────────────────────────────────
const wbCheckAll        = document.getElementById('wb-check-all');
const wbBulkBar         = document.getElementById('wb-bulk-bar');
const wbBulkCountN      = document.getElementById('wb-bulk-count-n');
const wbBulkEditBtn     = document.getElementById('wb-bulk-edit-btn');
const wbBulkClearBtn    = document.getElementById('wb-bulk-clear-btn');
const wbBulkBackdrop    = document.getElementById('wb-bulk-backdrop');
const wbBulkModal       = document.getElementById('wb-bulk-modal');
const wbBulkClose       = document.getElementById('wb-bulk-close');
const wbBulkCancel      = document.getElementById('wb-bulk-cancel');
const wbBulkSave        = document.getElementById('wb-bulk-save');
const wbBulkTargetN     = document.getElementById('wb-bulk-target-n');
const wbBulkDocType     = document.getElementById('wb-bulk-doc-type');
const wbBulkScope       = document.getElementById('wb-bulk-scope');
const wbBulkClass       = document.getElementById('wb-bulk-classification');
const wbBulkWarning     = document.getElementById('wb-bulk-warning');
const wbBulkStatus      = document.getElementById('wb-bulk-status');

function updateWbBulkBar() {
  const n = wbSelectedIds.size;
  wbBulkBar.hidden = n === 0;
  wbBulkCountN.textContent = String(n);
  // Keep the "select all" checkbox state aligned with the rendered rows.
  const rowChecks = wbDocTbody.querySelectorAll('.wb-row-check');
  const total = rowChecks.length;
  const checked = Array.from(rowChecks).filter(c => c.checked).length;
  wbCheckAll.checked       = total > 0 && checked === total;
  wbCheckAll.indeterminate = checked > 0 && checked < total;
}

wbDocTbody.addEventListener('change', e => {
  const cb = e.target.closest('.wb-row-check');
  if (!cb) return;
  const id = cb.dataset.id;
  if (cb.checked) wbSelectedIds.add(id);
  else wbSelectedIds.delete(id);
  cb.closest('tr').classList.toggle('wb-row-selected', cb.checked);
  updateWbBulkBar();
});

wbCheckAll.addEventListener('change', () => {
  const want = wbCheckAll.checked;
  wbDocTbody.querySelectorAll('.wb-row-check').forEach(cb => {
    cb.checked = want;
    const id = cb.dataset.id;
    if (want) wbSelectedIds.add(id);
    else wbSelectedIds.delete(id);
    cb.closest('tr').classList.toggle('wb-row-selected', want);
  });
  updateWbBulkBar();
});

wbBulkClearBtn.addEventListener('click', () => {
  wbSelectedIds.clear();
  wbDocTbody.querySelectorAll('.wb-row-check').forEach(cb => {
    cb.checked = false;
    cb.closest('tr').classList.remove('wb-row-selected');
  });
  updateWbBulkBar();
});

function _buildBulkScopeOptions() {
  const opts = ['<option value="">Keep current</option>',
                '<option value="global:">Global</option>'];
  wbClients.forEach(c => {
    opts.push(`<option value="client:${_esc(c.id)}">Client: ${_esc(c.name)}</option>`);
  });
  wbAllProjects.forEach(p => {
    const cl = wbClients.find(c => c.id === p.client_id);
    const lbl = cl ? `${_esc(p.name)} (${_esc(cl.name)})` : _esc(p.name);
    opts.push(`<option value="project:${_esc(p.id)}">Project: ${lbl}</option>`);
  });
  return opts.join('');
}

function _buildBulkDocTypeOptions() {
  const opts = ['<option value="">Keep current</option>'];
  _WB_DOC_TYPE_OPTIONS.forEach(([v, l]) => {
    opts.push(`<option value="${v}">${l}</option>`);
  });
  return opts.join('');
}

function _refreshBulkWarning() {
  // Warn if the user picked classification=public while also moving scope to
  // client/project — the backend will reject it, so catch it client-side.
  const scopeVal = wbBulkScope.value;
  const cls = wbBulkClass.value;
  const restricted = scopeVal.startsWith('client:') || scopeVal.startsWith('project:');
  if (restricted && cls === 'public') {
    wbBulkWarning.textContent = 'Client and project scopes cannot be classified as public. Pick a different classification or leave it at Keep current.';
    wbBulkWarning.hidden = false;
  } else {
    wbBulkWarning.hidden = true;
  }
}

function openBulkModal() {
  if (wbSelectedIds.size === 0) return;
  wbBulkDocType.innerHTML = _buildBulkDocTypeOptions();
  wbBulkScope.innerHTML   = _buildBulkScopeOptions();
  wbBulkDocType.value = '';
  wbBulkScope.value   = '';
  wbBulkClass.value   = '';
  wbBulkTargetN.textContent = String(wbSelectedIds.size);
  wbBulkWarning.hidden = true;
  wbBulkStatus.textContent = '';
  wbBulkSave.disabled = false;
  wbBulkSave.textContent = 'Apply';
  wbBulkBackdrop.hidden = false;
  wbBulkModal.hidden    = false;
}

function closeBulkModal() {
  wbBulkBackdrop.hidden = true;
  wbBulkModal.hidden    = true;
}

wbBulkEditBtn.addEventListener('click', openBulkModal);
wbBulkClose .addEventListener('click', closeBulkModal);
wbBulkCancel.addEventListener('click', closeBulkModal);
wbBulkBackdrop.addEventListener('click', closeBulkModal);
wbBulkScope .addEventListener('change', _refreshBulkWarning);
wbBulkClass .addEventListener('change', _refreshBulkWarning);

wbBulkSave.addEventListener('click', async () => {
  const ids = Array.from(wbSelectedIds);
  if (!ids.length) { closeBulkModal(); return; }

  const body = {};
  if (wbBulkDocType.value) body.doc_type = wbBulkDocType.value;
  if (wbBulkScope.value) {
    const [scope_type, scope_id] = wbBulkScope.value.split(':');
    body.scope_type = scope_type;
    body.scope_id   = scope_id || null;
  }
  if (wbBulkClass.value) body.classification = wbBulkClass.value;

  if (!Object.keys(body).length) {
    wbBulkStatus.textContent = 'Nothing to change — pick at least one field.';
    return;
  }

  const restricted = body.scope_type === 'client' || body.scope_type === 'project';
  if (restricted && body.classification === 'public') {
    _refreshBulkWarning();
    return;
  }

  wbBulkSave.disabled = true;
  wbBulkSave.textContent = 'Applying…';
  wbBulkStatus.textContent = `0 / ${ids.length} done`;

  let done = 0;
  let failed = 0;
  const firstErrors = [];
  await Promise.all(ids.map(async id => {
    try {
      const res = await fetch(`/api/documents/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (res.ok) {
        done++;
      } else {
        failed++;
        if (firstErrors.length < 3) {
          const err = await res.json().catch(() => ({}));
          firstErrors.push(err.detail || `HTTP ${res.status}`);
        }
      }
    } catch {
      failed++;
      if (firstErrors.length < 3) firstErrors.push('Network error');
    } finally {
      wbBulkStatus.textContent = `${done + failed} / ${ids.length} done`;
    }
  }));

  if (failed === 0) {
    closeBulkModal();
    wbSelectedIds.clear();
    await loadWbDocs();
    setStatus(`Updated ${done} document${done === 1 ? '' : 's'}.`, 'info');
  } else {
    wbBulkSave.disabled = false;
    wbBulkSave.textContent = 'Apply';
    wbBulkStatus.textContent = `${done} updated, ${failed} failed — ${firstErrors.join('; ')}`;
    // Reload the table so successful updates are reflected; selections that
    // actually succeeded are no longer on those rows' old state.
    await loadWbDocs();
  }
});

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
  updateScopeBadge();
  input.focus();
});

wbDocUpload.addEventListener('change', async () => {
  const files = Array.from(wbDocUpload.files);
  if (!files.length) return;
  wbDocUpload.value = '';

  const docType        = wbUploadType.value;
  const classification = wbUploadClass.value;
  const total   = files.length;
  let   done    = 0;
  let   failed  = 0;

  uploadInProgress = true;
  gpuFastInterval = setInterval(pollGpu, 500);

  for (let i = 0; i < files.length; i++) {
    const file   = files[i];
    const prefix = total > 1 ? `[${i + 1}/${total}] ` : '';

    const fd = new FormData();
    fd.append('file', file);
    fd.append('doc_type', docType);
    fd.append('classification', classification);
    fd.append('defer_index', 'true');
    if (wbScope.type === 'project' && wbScope.projectId) fd.append('project_id', wbScope.projectId);
    else if (wbScope.type === 'client' && wbScope.clientId) fd.append('client_id', wbScope.clientId);

    try {
      await new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/documents');
        xhr.upload.addEventListener('load', () => {
          setStatus(`${prefix}Embedding ${file.name}…`, 'busy');
        });
        xhr.addEventListener('load', () => {
          if (xhr.status >= 200 && xhr.status < 300) resolve();
          else {
            let detail = `HTTP ${xhr.status}`;
            try { detail = JSON.parse(xhr.responseText).detail || detail; } catch (_) {}
            reject(new Error(detail));
          }
        });
        xhr.addEventListener('error', () => reject(new Error('Network error')));
        xhr.addEventListener('abort', () => reject(new Error('Aborted')));
        setStatus(`${prefix}Uploading ${file.name}…`, 'busy');
        xhr.send(fd);
      });
      done++;
      setStatus(`${prefix}✓ ${file.name} — ${total - i - 1} remaining`, 'busy');
    } catch (err) {
      failed++;
      setStatus(`${prefix}✗ ${file.name}: ${err.message}`, 'error');
    }
  }

  uploadInProgress = false;
  clearInterval(gpuFastInterval);
  gpuFastInterval = null;

  await loadWbDocs();
  await Promise.all([pollModelStatus(), pollAnalysisModelStatus()]);

  if (failed === 0) {
    setStatus(
      total === 1
        ? `✓ ${files[0].name} uploaded — categorise then click Index Documents`
        : `✓ ${done} files uploaded — categorise then click Index Documents`,
      'info'
    );
  } else {
    setStatus(`${done} of ${total} uploaded, ${failed} failed`, failed === total ? 'error' : 'warning');
  }
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

const REINDEX_POLL_MS = 3000;
let _reindexPollTimer = null;

function _formatEta(seconds) {
  if (seconds == null) return '~ETA pending';
  if (seconds < 60)    return `~${seconds}s remaining`;
  const mins = Math.round(seconds / 60);
  if (mins < 60)       return `~${mins} min remaining`;
  const hrs  = Math.floor(mins / 60);
  const rem  = mins % 60;
  return `~${hrs}h ${rem}m remaining`;
}

function _renderReindexProgress(statusEl, state) {
  if (!state) { statusEl.innerHTML = ''; statusEl.textContent = ''; return; }
  const pct = state.chunks_total > 0
    ? Math.min(100, Math.round((state.chunks_done / state.chunks_total) * 100))
    : 0;
  let label;
  if (state.status === 'completed') {
    label = `Done — ${state.docs_done} doc(s), ${state.chunks_done} chunk(s)`;
  } else if (state.status === 'failed') {
    label = `Failed — ${state.error || 'unknown error'}`;
  } else {
    const cur = state.current_doc;
    const docPart   = `doc ${state.docs_done + (cur ? 1 : 0)}/${state.docs_total}`;
    const chunkPart = `${state.chunks_done.toLocaleString()} / ${state.chunks_total.toLocaleString()} chunks`;
    label = `Reindexing — ${docPart} · ${chunkPart} · ${_formatEta(state.eta_seconds)}`;
  }
  const klass = state.status === 'failed' ? 'reindex-progress is-failed' : 'reindex-progress';
  statusEl.innerHTML = `
    <div class="${klass}">
      <div class="reindex-bar-track"><div class="reindex-bar-fill" style="width:${pct}%"></div></div>
      <span class="reindex-label"></span>
    </div>`;
  statusEl.querySelector('.reindex-label').textContent = label;
}

function _stopReindexPoll() {
  if (_reindexPollTimer) { clearTimeout(_reindexPollTimer); _reindexPollTimer = null; }
}

async function _pollReindex(runId, statusEl, btns) {
  try {
    const res = await fetch('/api/documents/reindex/status?run_id=' + encodeURIComponent(runId));
    if (!res.ok) {
      _renderReindexProgress(statusEl, null);
      btns.forEach(b => { if (b) b.disabled = false; });
      return;
    }
    const state = await res.json();
    _renderReindexProgress(statusEl, state);
    if (state.status === 'running') {
      _reindexPollTimer = setTimeout(() => _pollReindex(runId, statusEl, btns), REINDEX_POLL_MS);
    } else {
      btns.forEach(b => { if (b) b.disabled = false; });
      // Leave the final state visible briefly, then clear.
      setTimeout(() => _renderReindexProgress(statusEl, null), 12000);
    }
  } catch (e) {
    _renderReindexProgress(statusEl, null);
    btns.forEach(b => { if (b) b.disabled = false; });
  }
}

async function runReindex(params = {}) {
  const qs = new URLSearchParams(params).toString();
  const url = '/api/documents/reindex' + (qs ? '?' + qs : '');
  const scoped   = params.project_id || params.client_id;
  const statusEl = scoped ? wbUploadStatus : wbMaintenanceStatus;
  const btns     = [wbReindexBtn, wbReindexAllBtn];
  _stopReindexPoll();
  btns.forEach(b => { if (b) b.disabled = true; });
  _renderReindexProgress(statusEl, {
    status: 'running', docs_total: 0, docs_done: 0,
    chunks_total: 0, chunks_done: 0, current_doc: null, eta_seconds: null,
  });
  try {
    const res = await fetch(url, { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.run_id) {
      _renderReindexProgress(statusEl, null);
      statusEl.textContent = data.detail || 'Re-index failed to start.';
      btns.forEach(b => { if (b) b.disabled = false; });
      setTimeout(() => { statusEl.textContent = ''; }, 8000);
      return;
    }
    _renderReindexProgress(statusEl, data);
    _pollReindex(data.run_id, statusEl, btns);
  } catch (e) {
    _renderReindexProgress(statusEl, null);
    statusEl.textContent = 'Re-index error.';
    btns.forEach(b => { if (b) b.disabled = false; });
    setTimeout(() => { statusEl.textContent = ''; }, 8000);
  }
}

// On page load, resume polling if a reindex is already in flight (e.g. the
// page was refreshed mid-run, or another tab kicked one off).
async function resumeReindexPollIfActive() {
  try {
    const res = await fetch('/api/documents/reindex/status');
    if (!res.ok) return;
    const state = await res.json();
    if (state.status !== 'running') return;
    const scoped = state.scope && (state.scope.project_id || state.scope.client_id);
    const statusEl = scoped ? wbUploadStatus : wbMaintenanceStatus;
    const btns = [wbReindexBtn, wbReindexAllBtn];
    btns.forEach(b => { if (b) b.disabled = true; });
    _renderReindexProgress(statusEl, state);
    _pollReindex(state.run_id, statusEl, btns);
  } catch (e) { /* no active run */ }
}
resumeReindexPollIfActive();

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

// ── Server activity poll ───────────────────────────────────────
// Reflects actual server state so any browser tab shows the same status.

async function pollActivity() {
  try {
    const res = await fetch('/api/activity');
    if (!res.ok) return;
    const { uploads } = await res.json();
    if (!uploads || uploads.length === 0) return;

    // Only update the status bar when the server reports active work —
    // never clear it here; that's the upload handler's job.
    const count = uploads.length;
    const names = uploads.map(u => u.filename);
    const maxElapsed = Math.max(...uploads.map(u => u.elapsed_sec));
    const elapsedFmt = maxElapsed >= 60
      ? `${Math.floor(maxElapsed / 60)}m ${Math.round(maxElapsed % 60)}s`
      : `${Math.round(maxElapsed)}s`;

    const stageLabels = { parsing: 'Parsing', embedding: 'Embedding', finalizing: 'Finalizing' };
    const stage = stageLabels[uploads[0].stage] || 'Processing';
    const msg = count === 1
      ? `${stage} ${names[0]}… (${elapsedFmt})`
      : `${stage} ${count} files… (${elapsedFmt})`;

    setStatus(msg, 'busy');
  } catch { /* non-critical */ }
}

// ── Bootstrap ─────────────────────────────────────────────────

async function applySiteConfig() {
  // Reserved for future site-config modes.
}

applySiteConfig();
fetchModels().then(() => Promise.all([pollModelStatus(), pollAnalysisModelStatus()]));
fetchConversations();
updateScopeBadge();

// "Browse library" button in chat docs header → switch to workbench
const browseLibraryBtn = document.getElementById('browse-library-btn');
if (browseLibraryBtn) {
  browseLibraryBtn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelector('.tab-btn[data-tab="workbench"]').classList.add('active');
    chatView.hidden      = false;
    workbenchView.hidden = false;
    chatView.hidden      = true;
    loadWorkbench();
  });
}

if (scopeBadge) {
  scopeBadge.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelector('.tab-btn[data-tab="workbench"]').classList.add('active');
    chatView.hidden      = true;
    workbenchView.hidden = false;
    loadWorkbench();
  });
}
fetchSystemPrompts();
pollGpu();
setInterval(pollGpu, 3000);
pollSystem();
setInterval(pollSystem, 3000);
setInterval(pollModelStatus, 5000);
setInterval(pollAnalysisModelStatus, 5000);
pollMerllm();
setInterval(pollMerllm, 15000);
pollActivity();
setInterval(pollActivity, 3000);
input.focus();
