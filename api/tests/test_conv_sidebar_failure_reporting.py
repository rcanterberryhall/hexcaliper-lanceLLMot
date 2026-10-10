"""Every failure path in the conversation sidebar reports, and none of them lies.

The sidebar's five API paths — rename, list refresh, conversation delete,
chat-document load and chat-document delete — each ended in a swallowed
exception or an unchecked status.  Four were merely silent; the rename was worse,
because it wrote the new title to the screen before knowing whether the server
had accepted it, so a refusal (404 once the conversation was deleted in another
tab, 403 for another Cloudflare Access identity, 502 from a restarting backend)
looked exactly like a success until the next ``fetchConversations()`` put the old
title back minutes later (issue #72).

These tests pin the properties that prevent the reappearance of each one:

* no failure path returns without reporting, on any of the five;
* the rename paints the title only *after* the response was checked;
* a failed document load empties the rail rather than leaving the previous
  conversation's documents listed under the newly-opened one;
* the report surfaces are static markup, so a sidebar repaint cannot erase the
  message before it is read.

They read ``web/app.js`` rather than exercising it, brace-matching each function
out of the real file so an edit that moves or renames one fails loudly instead
of silently testing nothing.
"""
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB = REPO_ROOT / "web"

# The five paths this issue covers, plus the helper that turns a failed response
# into something worth showing an operator.
SIDEBAR_FUNCTIONS = (
    "startRenameConv",
    "fetchConversations",
    "deleteConversation",
    "fetchChatDocuments",
    "renderChatDocList",
)

# The idioms that made these failures invisible.  `.catch(() => {})` is the one
# that hid the refused rename; the other two hid everything else.
SWALLOW_IDIOMS = (
    (r"\.catch\(\s*\(\s*\)\s*=>\s*\{\s*\}\s*\)", ".catch(() => {})"),
    (r"catch\s*\(\s*\w*\s*\)\s*\{\s*\}", "catch (_) {}"),
    (r"catch\s*\{\s*\}", "catch {}"),
)

# A report has to reach one of the two surfaces that outlive a sidebar repaint.
REPORT_CALLS = ("showErrorBar(", "setStatus(")


def _app_js() -> str:
    return (WEB / "app.js").read_text()


def _function_source(name: str) -> str:
    """Return *name*'s full source, brace-matched out of web/app.js."""
    app_js = _app_js()
    for signature in (f"async function {name}(", f"function {name}("):
        start = app_js.find(signature)
        if start >= 0:
            break
    else:
        raise AssertionError(f"Could not locate {name} in web/app.js")
    depth, end = 0, -1
    for i in range(app_js.index("{", start), len(app_js)):
        if app_js[i] == "{":
            depth += 1
        elif app_js[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    assert end > start, f"Could not brace-match the end of {name}"
    return app_js[start:end]


@pytest.fixture(scope="module")
def sidebar() -> dict:
    """The five functions' sources, keyed by name."""
    sources = {name: _function_source(name) for name in SIDEBAR_FUNCTIONS}
    # Guards: each slice is the code these tests believe it is.  Without these a
    # later rename would leave every assertion below passing against nothing.
    assert "method: 'PATCH'" in sources["startRenameConv"]
    assert "fetch('/api/conversations')" in sources["fetchConversations"]
    assert "{ method: 'DELETE' }" in sources["deleteConversation"]
    assert "/api/documents?conversation_id=" in sources["fetchChatDocuments"]
    assert "/api/documents/${doc.id}" in sources["renderChatDocList"]
    return sources


# ── Nothing is swallowed ─────────────────────────────────────────────────────

@pytest.mark.parametrize("name", SIDEBAR_FUNCTIONS)
def test_no_swallowed_failure_in_the_sidebar(sidebar, name):
    """None of the five discards a failure without saying anything."""
    source = sidebar[name]
    for pattern, idiom in SWALLOW_IDIOMS:
        assert not re.search(pattern, source), (
            f"{name} still contains `{idiom}`, which is how this defect family "
            f"stayed invisible: the rejection is raised correctly and then "
            f"dropped, so the control reads as inert"
        )


@pytest.mark.parametrize("name", SIDEBAR_FUNCTIONS)
def test_every_exit_after_a_request_reports(sidebar, name):
    """Once a request has gone out, no bail-out is silent.

    ``return;`` with no value is how each of these abandoned its work.  The rule
    is positional rather than a list of exempt conditions: bail-outs *before*
    the ``fetch`` are cheap pre-flight guards that have nothing to report yet
    (no conversation id, the title was not changed, the editor already settled),
    while every exit *after* it is the outcome of a request the operator is
    waiting on and must say what happened.

    An earlier version of this test exempted bail-outs by matching the
    surrounding lines for those conditions, and passed against the unfixed file
    because the pre-flight ``if (!convId) return;`` sat close enough to the real
    failure exit to shield it.
    """
    source = sidebar[name]
    request_at = source.find("fetch(")
    assert request_at >= 0, f"{name} no longer issues a request; this test is reading the wrong code"
    lines = source.splitlines()
    # Line index of the first `fetch(`, so exits can be placed either side of it.
    request_line = source[:request_at].count("\n")
    exits = [
        i for i, line in enumerate(lines)
        if re.search(r"\breturn;", line) and i > request_line
    ]
    assert exits, f"Expected {name} to handle at least one request outcome"
    for i in exits:
        # The report sits within a few lines above the bail-out, in the same
        # branch; `close(); return;` after a report is still a reported exit.
        window = "\n".join(lines[max(0, i - 6):i + 1])
        assert any(call in window for call in REPORT_CALLS), (
            f"{name} abandons the request at line {i + 1} of the function "
            f"without telling the operator why, so the control reads as "
            f"inert:\n{window}"
        )


# ── The rename does not claim a success it has not had ──────────────────────

def test_rename_paints_the_title_only_after_acceptance(sidebar):
    """The sidebar shows the server's answer, not the operator's intent.

    Writing ``titleEl.textContent`` before checking the response is the whole of
    the defect: it made a 404, 403 or 502 rename indistinguishable from a saved
    one.
    """
    source = sidebar["startRenameConv"]
    assert "if (!res.ok)" in source, (
        "startRenameConv does not check the PATCH response at all, so every "
        "status is treated as a success"
    )
    paints = [m.start() for m in re.finditer(r"titleEl\.textContent\s*=\s*newTitle", source)]
    assert paints, "startRenameConv no longer writes the renamed title"
    guard = source.index("if (!res.ok)")
    for at in paints:
        assert at > guard, (
            "startRenameConv writes the new title before the response is "
            "checked, so a refused rename is shown as saved"
        )


def test_rename_reports_the_title_it_could_not_save(sidebar):
    """The attempted title survives the failure, in the message.

    The editor closes and the span reverts to the old title, so the report is
    the only place the operator's typing still exists.
    """
    source = sidebar["startRenameConv"]
    reports = re.findall(r"showErrorBar\(`[^`]*`", source)
    assert len(reports) >= 2, (
        "Expected startRenameConv to report both a refused PATCH and a network "
        "failure"
    )
    for report in reports:
        assert "${newTitle}" in report, (
            f"A failed rename is reported without the title that was not saved, "
            f"so the operator's typing is lost: {report}"
        )


def test_rename_editor_settles_once(sidebar):
    """Enter, Escape and blur produce one outcome between them.

    ``commit`` awaits the PATCH while the input is still focused, so a blur
    arriving during the request used to send a second one; and because whether
    removing a focused input fires ``blur`` is engine-dependent, that same gap
    decided whether Escape cancelled or saved.
    """
    source = sidebar["startRenameConv"]
    assert "let settled = false;" in source, (
        "startRenameConv has no latch, so an in-flight commit can be commanded "
        "a second time by the blur that follows Enter"
    )
    assert "if (settled) return;" in source, "commit() does not check the latch"
    assert re.search(r"if \(e\.key === 'Escape'\)\s*\{\s*close\(\);", source), (
        "Escape does not route through close(), so a later blur can still "
        "commit the edit the operator cancelled"
    )


# ── A refusal is never a no-op ──────────────────────────────────────────────

def test_conversation_delete_reports_a_refusal(sidebar):
    """A 403 or 502 on ✕ is reported rather than skipped.

    The original ``if (res.status === 204 || res.ok) { … }`` had no ``else``, so
    pressing ✕ was indistinguishable from not pressing it.
    """
    source = sidebar["deleteConversation"]
    assert "showErrorBar(" in source, (
        "deleteConversation reports nothing, so a refused delete leaves the row "
        "in place with no explanation"
    )
    success = source.index("res.status === 204 || res.ok")
    assert source.index("showErrorBar(", success) > success, (
        "deleteConversation's report is not on the failure side of the success "
        "check"
    )


def test_a_404_delete_still_refreshes_the_list(sidebar):
    """The stale row leaves the sidebar even though the delete was refused.

    A 404 means the conversation is already gone — deleted in another tab, or an
    earlier ✕ succeeded — so the refresh is the only thing that takes the row
    that is still answering clicks off screen.
    """
    source = sidebar["deleteConversation"]
    assert re.search(r"res\.status === 404.*fetchConversations\(\)", source, re.S), (
        "A 404 delete does not refresh the list, so the row it could not delete "
        "stays on screen and keeps accepting clicks"
    )


def test_chat_document_delete_checks_its_response(sidebar):
    """✕ on a chat document reports, like the identical Workbench delete does.

    ``_makeDocItem`` has already made the operator confirm a permanent removal
    from the knowledge graph; the unchecked ``fetch`` meant a refusal simply
    re-rendered the document and the button read as inert.
    """
    source = sidebar["renderChatDocList"]
    assert re.search(r"res\s*=\s*await fetch\(`/api/documents/\$\{doc\.id\}`", source), (
        "The chat-document delete still discards its response, so a 403 or 502 "
        "is indistinguishable from a successful deletion"
    )
    assert "res.ok || res.status === 204" in source, (
        "The chat-document delete does not check for success; this is the shape "
        "the system-prompt delete already uses"
    )
    assert "${doc.filename}" in source, (
        "A failed document delete must name the document that survived — the "
        "rail holds several and ✕ is per-row"
    )


# ── A failed load clears rather than keeps ──────────────────────────────────

def test_failed_document_load_empties_the_rail(sidebar):
    """Every failure in ``fetchChatDocuments`` clears the list first.

    Returning early left the previous conversation's documents listed under the
    newly-opened one, with ✕ buttons that deleted them — and
    ``loadConversation`` reads that list to decide whether to raise the
    copyright gate, so the gate fired for another conversation's documents.
    """
    source = sidebar["fetchChatDocuments"]
    clears = source.count("renderChatDocList([])")
    reports = source.count("showErrorBar(")
    assert reports >= 2, (
        "Expected fetchChatDocuments to report a refused load and a network "
        "failure"
    )
    assert clears == reports, (
        f"fetchChatDocuments reports {reports} failures but clears the rail on "
        f"only {clears} of them; an unreported-but-kept rail is the stale-"
        f"documents defect returning"
    )
    lines = source.splitlines()
    for i, line in enumerate(lines):
        if "showErrorBar(" not in line:
            continue
        above = "\n".join(lines[max(0, i - 2):i])
        assert "renderChatDocList([])" in above, (
            f"fetchChatDocuments reports a failure at line {i + 1} without "
            f"having emptied the rail, so the previous conversation's documents "
            f"stay listed under the new one:\n{line.strip()}"
        )


def test_list_refresh_failure_is_a_warning_not_an_error(sidebar):
    """A stale conversation list is usable, so it is kept and labelled.

    Unlike the document rail, nothing in the conversation list is scoped to the
    conversation on screen, so keeping it beats blanking it — but it is reported,
    because this is also the refresh that reveals a refused rename.
    """
    source = sidebar["fetchConversations"]
    reports = re.findall(r"showErrorBar\([^;]*\);", source, re.S)
    assert len(reports) >= 2, "Expected fetchConversations to report its failures"
    for report in reports:
        assert "'warning'" in report, (
            f"A failed list refresh is reported at error level; the list on "
            f"screen is still usable, so this is a warning: {report}"
        )
    assert "renderConvList([])" not in source, (
        "A failed refresh must not blank the conversation list — a stale list "
        "is more use than none"
    )


# ── The helper, and the surfaces it reports onto ────────────────────────────

def test_failure_detail_prefers_the_server_reason(sidebar):
    """``_failureDetail`` shows the endpoint's own words where it has them.

    The conversation and document routers answer with ``detail`` set —
    ``"Conversation not found."``, ``"Access denied."`` — which is far better
    than a bare status; an nginx 502 sends HTML, so the fallback is needed too.
    """
    source = _function_source("_failureDetail")
    assert ".detail ||" in source, "_failureDetail does not prefer the server's detail field"
    assert "HTTP ${res.status}" in source, (
        "_failureDetail has no fallback for a body that is absent or not JSON, "
        "so a 502 from nginx would report nothing"
    )


def test_report_surfaces_are_static_markup():
    """The report outlives the repaint that follows it.

    Every one of these paths triggers a sidebar or rail repaint, so a message
    rendered into a container that gets rebuilt is erased before it can be read.
    Both surfaces live in web/index.html.
    """
    html = (WEB / "index.html").read_text()
    for element_id in ("error-bar", "error-bar-text", "status-bar", "status-msg"):
        assert f'id="{element_id}"' in html, (
            f"#{element_id} is not in web/index.html; the sidebar has no static "
            f"element to report a failure into"
        )
