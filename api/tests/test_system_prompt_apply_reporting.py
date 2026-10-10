"""
test_system_prompt_apply_reporting.py — The saved-system-prompt selector states
the server's answer, not the operator's intent.

Guards hexcaliper-lanceLLMot#74: a system-prompt assignment the server refused
must not be left on screen as applied, and a selection made before the
conversation exists must not be silently dropped.

Before the fix, `applySpToConversation` issued a bare `await fetch(...)` whose
response was never assigned, so there was no `res.ok` check anywhere in the
function, and `catch (_) {}` disposed of the network case. A `<select>` keeps
whatever the operator picked without any help from the code, so all of 404
("Conversation not found." — deleted in another tab), 403 ("Access denied." —
ownership, resolved from Cloudflare Access's authenticated-user header), 404
("System prompt not found.") and an nginx 502 ended with the dropdown naming a
prompt the conversation did not have. That is a claim about the model's
standing instructions: `routers/chat.py` prepends the saved prompt's content as
the `system` message and prefers it over the free-text box, so every later
reply in the conversation was built from the prompt the operator thought they
had replaced.

These are source assertions, not behavioural ones — the browser-side logic is
exercised separately. They exist so the properties the fix rests on cannot be
removed without a red test.
"""
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[2] / "web" / "app.js"


@pytest.fixture(scope="module")
def app_js() -> str:
    """The shipped frontend source, read from the file the server serves."""
    return APP_JS.read_text(encoding="utf-8")


def _block_from(source: str, marker: str) -> str:
    """
    Return *marker* plus the brace-balanced block that follows it.

    Lets a test read one function or one event handler in isolation, so an
    assertion about "the failure path" cannot be satisfied by similar-looking
    code elsewhere in a 2,900-line file.
    """
    start = source.index(marker)
    depth = 0
    for i in range(start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
    raise AssertionError(f"unbalanced braces after {marker!r}")


@pytest.fixture(scope="module")
def apply_fn(app_js: str) -> str:
    """The source of `applySpToConversation`, the only assignment path."""
    return _block_from(app_js, "async function applySpToConversation(")


# ── The response has to be readable, and read ────────────────────────────────

def test_assignment_request_is_assigned_so_its_status_can_be_read(apply_fn: str):
    """A bare `await fetch(...)` statement throws the status away before any
    check is possible — the defect's own signature."""
    assert "res = await fetch(" in apply_fn, (
        "the PATCH response is not assigned to a variable, so its status "
        "cannot be checked"
    )


def test_assignment_checks_the_status_before_treating_it_as_made(apply_fn: str):
    """The select already shows the operator's choice, so an unchecked
    response is a success claim nobody verified."""
    assert "!res.ok" in apply_fn, (
        "no response-status check in applySpToConversation — a refused "
        "assignment would be left on screen as applied"
    )


# ── A refusal puts the dropdown back ─────────────────────────────────────────

def test_all_three_failure_paths_restore_the_previous_selection(apply_fn: str):
    """Three ways the assignment can fail to land — no conversation yet, a
    network throw, a non-2xx — and each has to undo the selection, because
    nothing else will."""
    reverts = apply_fn.count("_revertSpSelect(prevValue)")
    assert reverts == 3, (
        f"expected the no-conversation guard, the network catch and the "
        f"non-2xx branch each to restore the selection, found {reverts} "
        f"call(s) to _revertSpSelect"
    )


def test_failure_report_prefers_the_endpoints_own_words(apply_fn: str):
    """`routers/system_prompts.py` always sets `detail`; an nginx 502 sets no
    JSON body at all, so the status is the fallback."""
    assert ".detail ||" in apply_fn, (
        "the failure report does not prefer the endpoint's own detail field"
    )
    assert "HTTP ${res.status}" in apply_fn, (
        "no fallback for a response body that is absent or not JSON (an "
        "nginx 502 or Cloudflare 524 carries HTML)"
    )
    assert "showErrorBar(" in apply_fn, (
        "a refused assignment is not reported on the static error bar"
    )


def test_no_conversation_guard_reports_instead_of_returning_in_silence(apply_fn: str):
    """Checked by *position* — everything before the request is the
    early-return path. The send path carries only the free-text prompt, so
    accepting a selection here would promise what no request delivers."""
    head = apply_fn[: apply_fn.index("await fetch(")]
    assert "_revertSpSelect" in head, (
        "the no-conversation path keeps a selection it never sends"
    )
    assert "setStatus(" in head, (
        "the no-conversation path drops the selection without saying why"
    )


# ── A confirmed assignment is recorded before anything else can fail ────────

def test_confirmation_is_recorded_before_the_sidebar_refresh(apply_fn: str):
    """`fetchConversations()` reports its own failures. If the assignment were
    recorded after it, a failed sidebar refresh would read as a refused
    assignment and revert a dropdown the server had accepted."""
    assert apply_fn.index("_appliedSpValue = spSelect.value") < apply_fn.index(
        "await fetchConversations()"
    ), (
        "the confirmed assignment is recorded after the sidebar refresh, so "
        "the refresh's own failure could be attributed to the assignment"
    )


def test_successful_assignment_still_refreshes_the_conversation_list(apply_fn: str):
    """No-regression guard: the sidebar `SP` badge is painted from the
    conversation list, so dropping this refresh would trade one stale
    indicator for another."""
    assert "await fetchConversations()" in apply_fn, (
        "the success path no longer refreshes the conversation list"
    )


# ── The revert target has to track what the server said ─────────────────────

def test_change_handler_supplies_the_value_being_replaced(app_js: str):
    """A `change` event fires with the new value already in place, so the
    replaced value cannot be read off the element and must come from the
    recorded one."""
    handler = _block_from(app_js, "spSelect.addEventListener('change',")
    assert "const prevValue = _appliedSpValue" in handler, (
        "the change handler does not capture the value being replaced, so a "
        "refusal has nothing to revert to"
    )
    assert "applySpToConversation(spId, prevValue)" in handler, (
        "the change handler does not pass the value to revert to"
    )


def test_change_handler_still_tracks_edit_button_enablement(app_js: str):
    """No-regression guard: Edit is only meaningful with a prompt selected,
    and that coupling predates this fix."""
    handler = _block_from(app_js, "spSelect.addEventListener('change',")
    assert "spEditBtn.disabled = !spSelect.value" in handler, (
        "the change handler no longer syncs the Edit button to the selection"
    )


def test_conversation_load_records_the_assignment_it_displays(app_js: str):
    """`syncSpForConversation` is the server's answer arriving — both the
    blank-chat branch and the loaded-conversation branch have to record it,
    or a later refusal reverts to a value the conversation never had."""
    sync = _block_from(app_js, "function syncSpForConversation(")
    assert sync.count("_appliedSpValue") == 2, (
        "syncSpForConversation does not record the displayed assignment in "
        "both its branches"
    )


def test_revert_helper_resyncs_the_edit_button(app_js: str):
    """Reverting the selection without resyncing Edit would leave the button
    enabled for a prompt that is no longer selected."""
    revert = _block_from(app_js, "function _revertSpSelect(")
    assert "spSelect.value = value" in revert
    assert "spEditBtn.disabled = !spSelect.value" in revert, (
        "_revertSpSelect leaves the Edit button out of sync with the "
        "restored selection"
    )
