"""
test_wb_docs_load_reporting.py — How the Workbench reports a document load.

`loadWbDocs()` fills the one array that both the document table and the
per-project completeness grid are painted from, and the grid's badges are a
claim about what a project holds: a zero count reds out the five
safety-critical document types.  Two properties therefore have to hold in the
shipped `web/app.js`, and both were absent in issue #70:

  1. A failed request is not an empty scope.  Collapsing a non-2xx response or
     a thrown fetch into `wbDocs = []` made a 500 report every safety-critical
     type missing, with the table underneath saying `No documents found for
     this scope.`
  2. A load that has been overtaken does not render.  The scope rail commits to
     the clicked scope synchronously, so without a generation token the
     response that lands last wins — leaving the grid titled for one project
     and counted from another's documents.

These assertions read the shipped asset, so the properties cannot be lost by a
later edit to the frontend.  The behaviour of the endpoint itself (a 200 with a
JSON list) is covered by `test_documents_router.py`.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB = REPO_ROOT / "web"


def _app_js() -> str:
    """The shipped frontend with whole-line comments removed.

    House style puts a *why* comment above each step, and a text needle matches
    a comment as readily as the code it describes — an ordering assertion built
    on one compares against the comment's position.
    """
    lines = (WEB / "app.js").read_text().split("\n")
    return "\n".join(l for l in lines if not l.lstrip().startswith("//"))


def _body(src: str, signature: str) -> str:
    """Return the body of the function whose declaration starts with `signature`."""
    start = src.index(signature)
    depth, i = 0, src.index("{", start)
    open_brace = i
    while True:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[open_brace:i + 1]
        i += 1


def _wb_table_column_count() -> int:
    """Count the `<th>` cells in the Workbench document table's header row."""
    html = (WEB / "index.html").read_text()
    thead = re.search(r'<table class="wb-table">.*?<thead>(.*?)</thead>', html, re.S)
    assert thead, "Could not locate the wb-table <thead> in web/index.html"
    columns = re.findall(r"<th[\s>]", thead.group(1))
    assert columns, "Found no <th> cells in the wb-table header row"
    return len(columns)


# ── A load that has been overtaken must not render ───────────────────────────

def test_load_takes_a_generation_token_before_issuing_its_request():
    body = _body(_app_js(), "async function loadWbDocs(")
    assert "++_wbDocsSeq" in body, (
        "loadWbDocs does not claim a generation number, so two in-flight loads "
        "cannot be told apart"
    )
    assert body.index("++_wbDocsSeq") < body.index("await fetch("), (
        "the generation number must be claimed before the request is issued"
    )


def test_load_checks_its_token_between_the_response_and_the_first_render():
    body = _body(_app_js(), "async function loadWbDocs(")
    check = body.index("seq !== _wbDocsSeq")
    assert body.index("await fetch(") < check, (
        "an overtake check before the await cannot see a later load"
    )
    assert check < body.index("paintWbDocsPane()"), (
        "the overtake check must come before the render, or the abandoned load "
        "still draws over its successor"
    )


def test_load_describes_the_scope_it_was_issued_for():
    """The rail moves on during the request, so the scope is captured at entry."""
    body = _body(_app_js(), "async function loadWbDocs(")
    assert "const scope = wbScope;" in body
    after_capture = body[body.index("const scope = wbScope;") + 24:]
    assert "wbScope" not in after_capture, (
        "loadWbDocs re-reads the live wbScope after capturing it, so one load "
        "can request or label a scope other than its own"
    )


# ── A failed request is not an empty scope ───────────────────────────────────

def test_load_does_not_collapse_a_failure_into_an_empty_list():
    body = _body(_app_js(), "async function loadWbDocs(")
    assert "res.ok ? await res.json() : []" not in body, (
        "a non-2xx response is being turned into an empty document list, which "
        "the completeness grid reads as 'this project holds none of these types'"
    )
    assert "res.status" in body, "the HTTP status is not recorded as the reason"
    assert "wbDocsError" in body, "nothing records that the load failed"


def test_a_failed_load_is_resolved_rather_than_left_to_the_empty_state_row():
    src = _app_js()
    chokepoint = _body(src, "function paintWbDocsPane(")
    assert "wbDocsError" in chokepoint and "renderWbDocsUnavailable()" in chokepoint, (
        "the repaint chokepoint does not distinguish a failed load, so a filter "
        "or sort click replaces the failure with an empty-scope row"
    )
    failure_row = _body(src, "function renderWbDocsUnavailable(")
    assert "No documents found" not in failure_row
    assert "wbDocsError.reason" in failure_row, (
        "the failure row does not state why the load failed"
    )
    assert "wbDocsError.scope.label" in failure_row, (
        "the failure row must name the scope the failed load was for, not "
        "whichever scope is selected when it is painted"
    )


def test_every_document_pane_repaint_goes_through_the_chokepoint():
    src = _app_js()
    calls = len(re.findall(r"(?<!function )renderWbDocs\(\)", src))
    assert calls == 1, (
        f"renderWbDocs() is called from {calls} places; every repaint has to go "
        "through paintWbDocsPane() or a stated failure can be overwritten by "
        "the last scope that loaded successfully"
    )
    assert "renderWbDocs()" in _body(src, "function paintWbDocsPane(")


def test_failure_row_offers_a_way_to_retry():
    src = _app_js()
    failure_row = _body(src, "function renderWbDocsUnavailable(")
    assert "wb-retry-btn" in failure_row, "the failed pane gives no control to retry with"
    assert "loadWbDocs()" in failure_row, "the retry control is not wired to a reload"
    assert ".wb-retry-btn" in (WEB / "styles.css").read_text(), (
        "the retry control has no style, so it renders as an unstyled button"
    )


def test_failure_row_spans_every_column():
    """Same invariant as the other full-width rows (issue #64)."""
    expected = _wb_table_column_count()
    failure_row = _body(_app_js(), "function renderWbDocsUnavailable(")
    match = re.search(r'<tr class="wb-empty-row wb-error-row"><td colspan="(\d+)">', failure_row)
    assert match, "Could not locate the failure row's <td> in web/app.js"
    assert int(match.group(1)) == expected, (
        f"the failure row spans {match.group(1)} of the table's {expected} columns"
    )


# ── The completeness grid only claims what it is holding ─────────────────────

def test_completeness_grid_resolves_the_load_before_counting_types():
    body = _body(_app_js(), "function renderWbCompleteness(")
    guard = min(body.index("wbDocsError"), body.index("_sameWbScope"))
    assert guard < body.index("critical missing"), (
        "the grid classes types as missing before checking whether the "
        "documents it is counting are this project's and arrived intact"
    )
    assert "wb-completeness-note" in body, (
        "the grid has no way to say it cannot count, so silence and absence "
        "look the same"
    )
    assert ".wb-completeness-note" in (WEB / "styles.css").read_text()


def test_completeness_grid_compares_scopes_by_value():
    body = _body(_app_js(), "function _sameWbScope(")
    for field in ("type", "clientId", "projectId"):
        assert f"a.{field} === b.{field}" in body, (
            f"scopes are compared without {field}, so two different scopes can "
            "be treated as the same one"
        )


# ── Guards on what the fix must not remove ───────────────────────────────────

def test_an_empty_scope_still_reads_as_an_empty_scope():
    """A successful load that returns nothing still says the scope is empty.

    The failure path above exists to stop a *fault* making that claim, not to
    stop the claim being made when it is true.
    """
    body = _body(_app_js(), "function renderWbDocs(")
    assert "No documents found for this scope." in body


def test_a_genuinely_absent_safety_critical_type_is_still_reported():
    body = _body(_app_js(), "function renderWbCompleteness(")
    assert "_WB_SAFETY_CRITICAL_TYPES.includes(t)" in body, (
        "the grid no longer singles out the five safety-critical document "
        "types, which is the claim it exists to make"
    )
    assert "critical missing" in body
