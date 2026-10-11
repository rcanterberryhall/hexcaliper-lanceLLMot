"""
test_wb_completeness_scope.py — the Workbench completeness grid may only count
documents scoped to the selected project.

Two halves, and they depend on each other.

The router half establishes the premise the frontend has to cope with:
``GET /documents?project_id=P`` is a *visibility* query, not an inventory one.
It deliberately unions the global library and the parent client's documents into
the response (a sibling project's documents are not reachable — only the
``global`` clause is unpaired), so the response is strictly larger than the project's own holdings
and a per-type count taken over the whole of it is not a statement about the
project.

The asset half asserts that ``renderWbCompleteness`` in ``web/app.js``
partitions that response by document scope instead of counting it wholesale.
Counting it wholesale is what made a library FMEA template turn a project's
FMEA badge green while the project held no FMEA at all, and the grid's own help
row in ``web/index.html`` promises a per-project claim.
"""
import io
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
APP_JS = REPO / "web" / "app.js"
INDEX_HTML = REPO / "web" / "index.html"
STYLES_CSS = REPO / "web" / "styles.css"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _upload(client, filename, doc_type, project_id=None, client_id=None):
    params = {"doc_type": doc_type}
    if project_id:
        params["project_id"] = project_id
    if client_id:
        params["client_id"] = client_id
    r = client.post(
        "/documents",
        params=params,
        files={"file": (filename, io.BytesIO(b"document text"), "text/plain")},
    )
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def workspace(app_client):
    """One client with two sibling projects."""
    c = app_client.post("/workspace/clients", json={"name": "Acme"})
    assert c.status_code == 201, c.text
    c = c.json()
    p1 = app_client.post(
        "/workspace/projects", json={"name": "Line 7 Upgrade", "client_id": c["id"]}
    ).json()
    p2 = app_client.post(
        "/workspace/projects", json={"name": "Line 8 Upgrade", "client_id": c["id"]}
    ).json()
    return c, p1, p2


def _by_type(docs, doc_type):
    return [d for d in docs if d["doc_type"] == doc_type]


# ── The premise: a project request answers with more than the project ─────────

def test_project_request_includes_the_global_library(app_client, workspace):
    """A global FMEA is visible from a project that holds nothing."""
    _upload(app_client, "fmea-template.xlsx", "fmea")          # global scope
    _c, p1, _p2 = workspace

    docs = app_client.get("/documents", params={"project_id": p1["id"]}).json()

    fmeas = _by_type(docs, "fmea")
    assert len(fmeas) == 1
    assert fmeas[0]["scope_type"] == "global"
    assert fmeas[0]["scope_id"] is None


def test_project_request_includes_the_parent_clients_documents(app_client, workspace):
    """A client-scoped SAT reaches every project under that client."""
    c, p1, _p2 = workspace
    _upload(app_client, "acme-sat.pdf", "sat", client_id=c["id"])

    docs = app_client.get("/documents", params={"project_id": p1["id"]}).json()

    sats = _by_type(docs, "sat")
    assert len(sats) == 1
    assert sats[0]["scope_type"] == "client"
    assert sats[0]["scope_id"] == c["id"]


def test_the_projects_own_document_is_tagged_with_its_project_id(app_client, workspace):
    """The scope fields the grid has to filter on are present and correct."""
    _c, p1, _p2 = workspace
    _upload(app_client, "line7-fmea.xlsx", "fmea", project_id=p1["id"])

    docs = app_client.get("/documents", params={"project_id": p1["id"]}).json()

    fmeas = _by_type(docs, "fmea")
    assert len(fmeas) == 1
    assert fmeas[0]["scope_type"] == "project"
    assert fmeas[0]["scope_id"] == p1["id"]


def test_counting_the_whole_response_overstates_what_the_project_holds(
    app_client, workspace
):
    """
    The paired premise, and the reason the frontend needs a filter rather than a
    length check: with a library FMEA and the client's own FMEA in the response,
    a wholesale count says 2 where the project holds 0.
    """
    c, p1, _p2 = workspace
    _upload(app_client, "fmea-template.xlsx", "fmea")                    # global
    _upload(app_client, "acme-fmea.xlsx", "fmea", client_id=c["id"])     # client

    docs = app_client.get("/documents", params={"project_id": p1["id"]}).json()

    wholesale = len(_by_type(docs, "fmea"))
    own = len([
        d for d in _by_type(docs, "fmea")
        if d["scope_type"] == "project" and d["scope_id"] == p1["id"]
    ])
    assert wholesale == 2, "both wider-scope FMEAs are visible from the project"
    assert own == 0, "this project holds no FMEA of its own"
    assert wholesale != own, "a wholesale count cannot state what the project holds"


def test_a_sibling_projects_document_does_not_reach_this_project(
    app_client, workspace
):
    """
    Bounds the defect. Only the ``global`` clause is unpaired in
    ``db.list_documents_for_scope``; ``("project", id)`` and ``("client", id)``
    both match an exact id, so a *sibling* project's deliverable is not visible
    here and was never miscounted. The two vectors are the global library and
    the parent client — nothing else.
    """
    _c, p1, p2 = workspace
    _upload(app_client, "line8-theop.docx", "theop", project_id=p2["id"])

    docs = app_client.get("/documents", params={"project_id": p1["id"]}).json()

    assert _by_type(docs, "theop") == []


def test_a_project_holding_its_own_document_still_counts_it(app_client, workspace):
    """Guard: the filter must not throw the project's own documents away."""
    _c, p1, _p2 = workspace
    _upload(app_client, "fmea-template.xlsx", "fmea")                     # global
    _upload(app_client, "line7-fmea.xlsx", "fmea", project_id=p1["id"])   # its own

    docs = app_client.get("/documents", params={"project_id": p1["id"]}).json()

    own = [
        d for d in _by_type(docs, "fmea")
        if d["scope_type"] == "project" and d["scope_id"] == p1["id"]
    ]
    assert len(own) == 1


# ── The shipped grid: does it partition, or does it count wholesale? ──────────

def _render_completeness_source():
    """The body of renderWbCompleteness as shipped, sliced by source markers."""
    src = APP_JS.read_text(encoding="utf-8")
    start = src.index("function renderWbCompleteness")
    end = src.index("function sortWbRows", start)
    body = src[start:end]
    assert "_WB_ALL_COMPLETENESS_TYPES" in body, "slice missed the badge loop"
    return body


def test_the_grid_decides_each_badge_from_the_projects_own_documents():
    """
    The count behind a badge must be restricted to documents scoped to the
    selected project. Both halves are required: ``scope_type`` alone lets a
    sibling project's deliverable through, and ``wbScope.projectId`` is the only
    thing that distinguishes them.
    """
    body = _render_completeness_source()
    assert "scope_type" in body, (
        "renderWbCompleteness ignores document scope, so the global library and "
        "the parent client's documents are counted as the project's own"
    )
    assert "wbScope.projectId" in body, (
        "renderWbCompleteness does not compare scope_id against the selected "
        "project, so a sibling project's document counts for this one"
    )


def test_the_grid_does_not_count_the_response_wholesale():
    """The scope-blind loop this replaced must not come back."""
    body = _render_completeness_source()
    blind = re.search(
        r"for\s*\(const\s+d\s+of\s+wbDocs\)\s*counts\[d\.doc_type\]", body
    )
    assert blind is None, (
        "the per-type tally is taken over every visible document again"
    )


def test_documents_inherited_from_a_wider_scope_are_still_reported():
    """
    Filtering them out of the badge must not make them vanish: the table below
    lists them, so an unexplained red badge beside a visible FMEA row would read
    as a broken grid. The inherited chip is what reconciles the two.
    """
    body = _render_completeness_source()
    assert "cb-inherited" in body
    css = STYLES_CSS.read_text(encoding="utf-8")
    assert ".cb-inherited" in css, "the inherited chip has no styling rule"


def test_the_tooltip_states_the_scope_it_is_talking_about():
    body = _render_completeness_source()
    assert "in this project" in body, (
        "the tooltip reports a bare document count, which is the ambiguity the "
        "badge colour already had"
    )


def test_the_help_row_states_that_the_grid_counts_only_the_project():
    """
    The in-UI help is the grid's specification; it promised a per-project claim
    while the code counted every visible document. Keep them agreeing.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    row = re.search(r"<tr><td>Completeness grid</td><td>(.*?)</td></tr>", html, re.S)
    assert row, "the completeness-grid help row is gone"
    text = row.group(1)
    assert "only documents scoped to that project" in text
    assert "global library" in text and "parent client" in text


# ── Vocabulary: the grid's type list is the backend's ────────────────────────

def test_every_completeness_type_is_a_type_the_backend_accepts():
    """
    A badge for a type ``DOC_TYPES`` does not contain can never be satisfied —
    uploads coerce an unknown type to ``misc`` — so it would red out for good.
    """
    import models

    src = APP_JS.read_text(encoding="utf-8")
    block = re.search(
        r"const _WB_ALL_COMPLETENESS_TYPES = \[(.*?)\];", src, re.S
    )
    assert block, "_WB_ALL_COMPLETENESS_TYPES is gone"
    types = re.findall(r"'([a-z_]+)'", block.group(1))
    assert types, "no types parsed out of _WB_ALL_COMPLETENESS_TYPES"
    assert set(types) <= set(models.DOC_TYPES), (
        f"grid types absent from DOC_TYPES: {sorted(set(types) - set(models.DOC_TYPES))}"
    )


def test_every_safety_critical_type_is_a_type_the_backend_accepts():
    import models

    src = APP_JS.read_text(encoding="utf-8")
    block = re.search(r"const _WB_SAFETY_CRITICAL_TYPES = \[(.*?)\];", src, re.S)
    assert block, "_WB_SAFETY_CRITICAL_TYPES is gone"
    types = re.findall(r"'([a-z_]+)'", block.group(1))
    assert len(types) == 5
    assert set(types) <= set(models.DOC_TYPES)
