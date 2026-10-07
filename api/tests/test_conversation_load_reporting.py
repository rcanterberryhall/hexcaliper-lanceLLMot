"""
test_conversation_load_reporting.py — Failure reporting in the conversation switch.

`loadConversation` in `web/app.js` moves eight pieces of UI state from one
conversation to another: `currentConvId`, the agent-mode toggle, the chat pane,
the rendered messages, the sidebar highlight, the chat-docs visibility, the
chat-document list and the system-prompt selector.  Only the message render
depends on message content — it goes through `marked` and `hljs`, which load
from CDNs and may be absent — so only it can throw once the switch is under way.

Wrapping the whole switch in one `catch` made that throw skip the four steps
after the render loop, leaving the sidebar highlight, the document list and the
system-prompt selector on the *previous* conversation while `currentConvId` had
already moved to the new one, with nothing reported (issue #66).  These tests
pin the two properties that prevented it: no failure path returns without
telling the operator, and the steps that make the pane agree with
`currentConvId` sit at the function's own level rather than inside a block the
render can abort.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB = REPO_ROOT / "web"

# Steps that must run for the pane to describe the conversation that
# `currentConvId` points at; each is skipped if the render loop can abort them.
STATE_SYNC_CALLS = (
    "setActiveConvItem(id);",
    "setChatDocsVisible(true);",
    "await fetchChatDocuments();",
    "syncSpForConversation(conv);",
)


def _load_conversation_source() -> str:
    """Return the body of `loadConversation`, brace-matched out of web/app.js."""
    app_js = (WEB / "app.js").read_text()
    start = app_js.find("async function loadConversation(id) {")
    assert start >= 0, "Could not locate loadConversation in web/app.js"
    depth, end = 0, -1
    for i in range(app_js.index("{", start), len(app_js)):
        if app_js[i] == "{":
            depth += 1
        elif app_js[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    assert end > start, "Could not brace-match the end of loadConversation"
    source = app_js[start:end]
    assert "fetch(`/api/conversations/${id}`)" in source, (
        "The extracted function no longer fetches a conversation; this test is "
        "reading the wrong code"
    )
    return source


# ── Every failure path reports ───────────────────────────────────────────────

def test_no_early_return_is_silent():
    """A conversation that cannot be opened is reported, not passed over.

    Before #66 a 404 — the conversation was deleted in another tab — returned
    without touching anything, so clicking a sidebar entry was indistinguishable
    from not clicking it.
    """
    lines = _load_conversation_source().splitlines()
    # `return;` bails out of the switch; `return x;` would be a value and is not
    # a bail-out.  Matched anywhere on the line so an inline `if (…) return;`
    # counts too — that was the shape of #66's silent 404 path.
    exits = [i for i, line in enumerate(lines) if re.search(r"\breturn;", line)]
    assert exits, "Expected loadConversation to guard against a failed open"
    for i in exits:
        window = "\n".join(lines[max(0, i - 6):i + 1])
        assert "showErrorBar(" in window, (
            f"loadConversation abandons the open at line {i + 1} of the function "
            f"without reporting why, so the click is indistinguishable from no "
            f"click:\n{window}"
        )


def test_message_render_is_guarded_per_message():
    """A message that will not format is re-added, not dropped with the rest."""
    source = _load_conversation_source()
    loop = re.search(r"for \(const msg of conv\.messages.*?\n  \}", source, re.S)
    assert loop, "Could not locate the message render loop in loadConversation"
    body = loop.group(0)
    assert "try {" in body, (
        "The message render is not guarded per message, so one unrenderable "
        "message aborts the whole switch part-way through"
    )
    assert "{ isError: true }" in body, (
        "A message whose markdown render throws must be re-added through "
        "createMessage's error path, which uses textContent instead of marked"
    )


# ── The switch completes ─────────────────────────────────────────────────────

def test_state_sync_runs_at_function_level():
    """The steps that make the pane agree with `currentConvId` are not nestable.

    At two-space indentation they sit in `loadConversation`'s own body, so
    neither the render loop nor a `try` can skip them.  Any deeper indentation
    means something above can abort them and reintroduce #66's split state.
    """
    source = _load_conversation_source()
    for call in STATE_SYNC_CALLS:
        assert f"\n  {call}" in source, (
            f"`{call}` is not at loadConversation's own indentation level; if a "
            f"failing message render can skip it, the pane keeps the previous "
            f"conversation's state while currentConvId points at the new one"
        )


def test_state_sync_follows_the_render_loop():
    """`currentConvId` moves first, so the sync must come after the render."""
    source = _load_conversation_source()
    render_end = source.index("for (const msg of conv.messages")
    for call in STATE_SYNC_CALLS:
        assert source.index(call) > render_end, (
            f"`{call}` runs before the messages are rendered"
        )


# ── The report surface survives a repaint ────────────────────────────────────

def test_error_bar_is_static_markup():
    """`showErrorBar` must write somewhere no render reassigns.

    A report rendered into a container that something else rebuilds is erased
    before it can be read, so the bar lives in `web/index.html`.
    """
    html = (WEB / "index.html").read_text()
    for element_id in ("error-bar", "error-bar-text"):
        assert f'id="{element_id}"' in html, (
            f"#{element_id} is not in web/index.html; showErrorBar has no "
            f"static element to report a failed conversation open into"
        )
    app_js = (WEB / "app.js").read_text()
    assert "errorBarText.textContent = msg;" in app_js, (
        "showErrorBar no longer writes to the static #error-bar-text span"
    )
