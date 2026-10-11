"""
test_scope_badge_load_failure.py — the chat header's scope badge must not
present a failed document load as a document count.

Covers the defect in issue #79: when ``GET /documents`` answered with a
non-2xx, ``updateScopeBadge()`` collapsed the response into an empty list, so
the badge read ``0 docs`` and raised the empty-scope warning ("No documents in
this scope") — an API fault asserted as the project holding no documents, in
the one place the operator looks to confirm what the assistant is answering
from. The thrown path was already honest (``count = '?'``), so the two
branches of one function disagreed.

Two groups of tests:

* **Backend premises** — what ``GET /documents`` actually answers for each
  scope. These must pass both before and after the frontend fix: they
  establish that an empty list is a legitimate answer (so the empty-scope
  warning has to survive) and that the project query is a *superset* of the
  global one (so the two requests can land in either order, which is why the
  badge needs a generation token).
* **Shipped-asset assertions** — read ``web/app.js`` and ``web/styles.css``
  directly, because there is no JS test runner in this repo. They are
  positional where position is the point: a guard that runs before the fetch
  proves nothing about a response that has already arrived.
"""
import io
import re
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
APP_JS = REPO / "web" / "app.js"
STYLES = REPO / "web" / "styles.css"


# ── Shipped-asset helpers ─────────────────────────────────────────────────────

def _slice_fn(source: str, signature: str) -> str:
    """
    Return the body of the function introduced by *signature*, from its
    signature line to the first column-0 ``}``.

    Slicing the shipped file by marker (rather than by line number) means a
    later edit that moves or renames the function fails this helper loudly
    instead of silently asserting against nothing.
    """
    lines = source.split("\n")
    start = next((i for i, l in enumerate(lines) if l.startswith(signature)), None)
    assert start is not None, f"{signature!r} not found in web/app.js"
    end = start
    while end < len(lines) and lines[end] != "}":
        end += 1
    assert end < len(lines), f"no column-0 closing brace found after {signature!r}"
    return "\n".join(lines[start:end + 1])


@pytest.fixture(scope="module")
def app_js() -> str:
    return io.open(APP_JS, encoding="utf-8").read()


@pytest.fixture(scope="module")
def badge_fn(app_js: str) -> str:
    return _slice_fn(app_js, "async function updateScopeBadge() {")


# ── Helpers for the router tests ──────────────────────────────────────────────

def upload(client, filename, doc_type="standard", client_id=None, project_id=None):
    params = {"doc_type": doc_type}
    if client_id:
        params["client_id"] = client_id
    if project_id:
        params["project_id"] = project_id
    return client.post(
        "/documents",
        params=params,
        files={"file": (filename, io.BytesIO(b"body"), "text/plain")},
    )


def names(response) -> set:
    return {d["filename"] for d in response.json()}


def make_project(client, client_name="ACME", project_name="Line 7 Upgrade"):
    cid = client.post("/workspace/clients", json={"name": client_name}).json()["id"]
    pid = client.post(
        "/workspace/projects", json={"name": project_name, "client_id": cid}
    ).json()["id"]
    return cid, pid


# ── Backend premises ──────────────────────────────────────────────────────────

def test_empty_project_scope_is_a_legitimate_empty_answer(app_client):
    """
    A project holding nothing answers 200 with an empty list — so ``0`` is a
    real count the badge must still be allowed to warn about. The fix may only
    suppress the warning for a *failed* request, never for a genuine zero.
    """
    _cid, pid = make_project(app_client)
    r = app_client.get("/documents", params={"project_id": pid})
    assert r.status_code == 200
    assert r.json() == []


def test_global_scope_excludes_project_documents(app_client):
    """Without ``project_id`` the endpoint answers the global library alone."""
    _cid, pid = make_project(app_client)
    upload(app_client, "project_only.pdf", project_id=pid)
    upload(app_client, "in_global.pdf")

    r = app_client.get("/documents")
    assert r.status_code == 200
    assert names(r) == {"in_global.pdf"}


def test_project_scope_is_a_superset_of_global(app_client):
    """
    ``?project_id=`` unions the global library into the response, so the two
    requests the badge issues return different sets and the project one is the
    broader (and so likely slower) query. That asymmetry is the reason a scope
    change while a request is in flight can be overtaken, and the reason the
    paint needs a generation token.
    """
    _cid, pid = make_project(app_client)
    upload(app_client, "in_global.pdf")
    upload(app_client, "project_only.pdf", project_id=pid)

    glob = names(app_client.get("/documents"))
    proj = names(app_client.get("/documents", params={"project_id": pid}))

    assert proj == {"in_global.pdf", "project_only.pdf"}
    assert glob < proj


def test_project_scope_includes_the_parent_client(app_client):
    """
    A project's response also carries its parent client's documents, which is
    what makes the badge's count a claim about what the conversation can
    retrieve from rather than about what the project holds.
    """
    cid, pid = make_project(app_client)
    upload(app_client, "client_wide.pdf", doc_type="requirement", client_id=cid)
    upload(app_client, "project_only.pdf", project_id=pid)

    assert names(app_client.get("/documents", params={"project_id": pid})) == {
        "client_wide.pdf", "project_only.pdf"
    }


# ── The failure path is not rendered as a count ───────────────────────────────

def test_non_2xx_is_not_collapsed_into_an_empty_list(badge_fn: str):
    """
    The defect itself: ``res.ok ? await res.json() : []`` made a refusal and an
    empty library the same value.
    """
    assert "res.ok ? await res.json() : []" not in badge_fn
    assert not re.search(r"res\.ok\s*\?[^;]*:\s*\[\]", badge_fn), (
        "a non-2xx response must not become an empty document list"
    )


def test_non_2xx_renders_the_unknown_count_token(badge_fn: str):
    """
    A failure renders ``'?'`` — the token the thrown path already used — and
    both failure paths now set it, so there is one representation of "not
    established" rather than two disagreeing branches.
    """
    assert badge_fn.count("count  = '?';") == 2, (
        "both the non-2xx branch and the catch must render the unknown token"
    )


def test_the_reason_prefers_the_endpoint_detail_over_the_status(badge_fn: str):
    """
    FastAPI's handlers answer with a ``detail`` body; an nginx 502 or a
    Cloudflare 524 does not, so the status is the fallback. This is the idiom
    already used by the attach and bulk-save paths in this file.
    """
    assert "err.detail || `HTTP ${res.status}`" in badge_fn
    assert "res.json().catch(() => ({}))" in badge_fn, (
        "a non-JSON error body must not throw while building the reason"
    )


def test_the_reason_is_shown_in_the_count_title(badge_fn: str):
    """The fault is identifiable on screen, and interpolated through esc()."""
    assert "Document count unavailable" in badge_fn
    assert "${esc(reason)}" in badge_fn, "the server's reason must be escaped"


def test_the_empty_scope_warning_still_requires_a_real_zero(badge_fn: str):
    """
    Guard, and the assertion that must *keep* passing: the warning is derived
    from a count of exactly 0 in a project scope. ``'?' === 0`` is false, so a
    failed load cannot reach it.
    """
    assert "const warn    = count === 0 && !!projectId;" in badge_fn
    assert badge_fn.count("No documents in this scope") == 1
    assert badge_fn.count("warn ? '<span class=\"scope-warn-icon\"") == 1, (
        "the empty-scope warning must have exactly one, warn-gated, emission"
    )


def test_a_failed_count_is_visually_distinguishable(badge_fn: str):
    """A '?' is not painted in the same muted colour as a real count."""
    assert "scope-count-error" in badge_fn
    styles = io.open(STYLES, encoding="utf-8").read()
    assert ".scope-count.scope-count-error" in styles, (
        "web/styles.css must define the failed-count colour the markup asks for"
    )


# ── The paint belongs to the scope currently on screen ────────────────────────

def test_the_badge_has_a_single_paint_site(badge_fn: str):
    """
    One assignment to ``scopeBadge.innerHTML``, which is what makes the
    positional check in the next test meaningful — a guard placed before the
    only paint site covers every render.
    """
    assert badge_fn.count("scopeBadge.innerHTML =") == 1


def test_the_generation_token_is_claimed_before_the_request(app_js: str, badge_fn: str):
    """
    The token is a module-level counter claimed at entry, so each call knows
    whether a later one has superseded it.
    """
    assert "let _scopeBadgeSeq = 0;" in app_js
    claim = badge_fn.index("const seq = ++_scopeBadgeSeq;")
    fetch = badge_fn.index("await fetch(`/api/documents")
    assert claim < fetch, "the generation must be claimed before the request is issued"


def test_the_generation_is_checked_between_the_response_and_the_paint(badge_fn: str):
    """
    Positional on purpose. A staleness check sitting *before* the fetch would
    satisfy a text search while proving nothing about a response that has
    already arrived — the abandoned request is only identifiable after its
    ``await`` resumes. So the check has to fall after the fetch and before the
    first write to the badge.
    """
    fetch = badge_fn.index("await fetch(`/api/documents")
    check = badge_fn.index("if (seq !== _scopeBadgeSeq) return;")
    class_write = badge_fn.index("scopeBadge.className =")
    html_write = badge_fn.index("scopeBadge.innerHTML =")

    assert fetch < check, "the staleness check must run after the response arrives"
    assert check < class_write < html_write, (
        "the staleness check must precede every write to the badge"
    )


def test_the_scope_label_and_the_count_come_from_the_same_request(badge_fn: str):
    """
    The label is captured at entry alongside the query it is fetched with, so
    the badge cannot be titled for one scope and counted from another. With the
    staleness check in place the whole badge either paints from one request or
    does not paint at all.
    """
    label = badge_fn.index("scopeLabel  = `Project: ${projectName || projectId}`;")
    query = badge_fn.index("queryParam  = `?project_id=${encodeURIComponent(projectId)}`;")
    fetch = badge_fn.index("await fetch(`/api/documents")
    assert label < fetch and query < fetch
    assert "${esc(scopeLabel)}" in badge_fn
