"""
test_analysis_model_apply_reporting.py — the Analysis selector states the
server's answer (#82).

The chat selector is true by construction: ``modelSel.value`` travels in the
body of every ``/api/chat`` request. The Analysis selector is not — it sets
``config.ANALYSIS_MODEL`` in the API process, and a ``<select>`` moves to the
operator's choice natively, so the dropdown asserted a switch whether or not
``POST /api/set-analysis-model`` landed. The write had no ``res.ok`` check and
its ``catch`` was empty, ``localStorage`` was written before the request, and
the dot beside it reports whether the *selected* model is resident in Ollama —
so a refusal was shown as an acceptance and confirmed by a green ``Ready``.

Most of these are text properties of the shipped ``web/app.js``: the behaviour
is a browser's, but the guard against a future edit dropping the check back out
is the source. Four tests — the three backend premises the fix rests on and the
chat selector's independence — pass before the fix on purpose, because the
suite must not be satisfiable by deleting the control instead of fixing it.
"""
import re
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[2] / "web" / "app.js"


@pytest.fixture(scope="module")
def source():
    """The shipped front-end script, read as text."""
    assert APP_JS.is_file(), f"missing {APP_JS}"
    return APP_JS.read_text(encoding="utf-8")


def _body(source, declaration):
    """
    Return the body of a top-level declaration.

    Matches *declaration* at column 0 and reads to the next line whose first
    character closes it, which survives the template literals in this file in a
    way brace counting does not.
    """
    lines = source.split("\n")
    for i, line in enumerate(lines):
        if line.startswith(declaration):
            for j in range(i + 1, len(lines)):
                if lines[j][:1] in ("}", ")"):
                    return "\n".join(lines[i : j + 1])
            pytest.fail(f"unterminated declaration: {declaration}")
    pytest.fail(f"declaration not found: {declaration}")


# ── The write is checked ─────────────────────────────────────────────────────

def test_every_set_analysis_model_write_goes_through_the_one_helper(source):
    """A second unchecked call site is the defect returning."""
    assert len(re.findall(r"'/api/set-analysis-model'", source)) == 1
    assert "'/api/set-analysis-model'" in _body(
        source, "async function _applyAnalysisModel(model)"
    )


def test_the_helper_checks_the_response_status(source):
    body = _body(source, "async function _applyAnalysisModel(model)")
    assert "if (!res.ok)" in body, "the response status is never read"


def test_the_helper_reports_the_endpoints_own_words(source):
    """`set_analysis_model` sets `detail`; an nginx 502 body is HTML, hence the fallback."""
    body = _body(source, "async function _applyAnalysisModel(model)")
    assert "err.detail" in body
    assert "HTTP ${res.status}" in body


def test_the_network_case_is_reported_rather_than_swallowed(source):
    body = _body(source, "async function _applyAnalysisModel(model)")
    assert re.search(r"catch \(err\) \{\s*return \{ ok: false, detail: err\.message \}", body), (
        "a thrown request is still disposed of silently"
    )


def test_no_empty_catch_remains_on_the_analysis_selector_path(source):
    body = _body(source, "analysisSel.addEventListener('change', async () => {")
    assert "catch (_) {}" not in body


# ── The optimistic value is written only after the check ─────────────────────

def test_localstorage_is_written_only_where_the_server_confirmed(source):
    """
    Persisting before the answer let a refused model survive a reload and be
    re-POSTed on the next load. Both remaining writes follow a confirmation.
    """
    writers = [
        decl
        for decl in (
            "async function _applyAnalysisModel(model)",
            "async function _revertAnalysisSelector()",
        )
        if "localStorage.setItem('analysisModel'" in _body(source, decl)
    ]
    assert writers == [
        "async function _applyAnalysisModel(model)",
        "async function _revertAnalysisSelector()",
    ]
    assert len(re.findall(r"localStorage\.setItem\('analysisModel'", source)) == 2


def test_the_confirmed_write_follows_the_status_check(source):
    body = _body(source, "async function _applyAnalysisModel(model)")
    assert body.index("if (!res.ok)") < body.index("localStorage.setItem('analysisModel'")


def test_the_change_handler_does_not_persist_its_own_value(source):
    body = _body(source, "analysisSel.addEventListener('change', async () => {")
    assert "localStorage.setItem" not in body


# ── The revert target is recorded state, not the element ─────────────────────

def test_the_confirmed_model_is_recorded_at_module_scope(source):
    """A `change` event fires with the new value already in place, so the
    replaced value cannot be read back off the `<select>`."""
    assert "let _appliedAnalysisModel = null;" in source
    assert "_appliedAnalysisModel = data.model || model;" in _body(
        source, "async function _applyAnalysisModel(model)"
    )


def test_the_revert_falls_back_to_the_servers_own_answer(source):
    """`GET /api/health` carries `analysis_model`; nothing read it before."""
    body = _body(source, "async function _revertAnalysisSelector()")
    assert "'/api/health'" in body
    assert "analysis_model" in body


def test_the_revert_only_selects_a_model_the_list_offers(source):
    body = _body(source, "async function _revertAnalysisSelector()")
    assert "Array.from(analysisSel.options).some(o => o.value === model)" in body


# ── Both callers report ──────────────────────────────────────────────────────

CALLERS = [
    ("analysisSel.addEventListener('change', async () => {", "the operator's switch"),
    ("async function fetchModels()", "the page-load notify"),
]


@pytest.mark.parametrize("declaration,label", CALLERS)
def test_caller_reverts_and_reports_on_refusal(source, declaration, label):
    body = _body(source, declaration)
    assert "_applyAnalysisModel(" in body, f"{label} does not go through the helper"
    assert "_revertAnalysisSelector()" in body, f"{label} leaves the refused model selected"
    assert "showErrorBar(" in body, f"{label} reports nothing"


@pytest.mark.parametrize("declaration,label", CALLERS)
def test_the_report_names_the_model_that_was_refused(source, declaration, label):
    """The dropdown reverts, so the message is the only surviving copy of it."""
    body = _body(source, declaration)
    assert "${result.detail}" in body
    assert "Still using ${current}" in body


def test_the_page_load_notify_is_not_awaited(source):
    """The model list is already on screen and must not wait on this write."""
    body = _body(source, "async function fetchModels()")
    assert "await _applyAnalysisModel(" not in body
    assert "_applyAnalysisModel(restored).then(" in body


def test_the_dot_is_repainted_after_a_revert(source):
    """Reverting without re-polling leaves the refused model's dot on screen."""
    for declaration, _ in CALLERS:
        assert "pollAnalysisModelStatus()" in _body(source, declaration)


# ── Backend premises (pass before the fix) ───────────────────────────────────

def test_health_reports_the_live_analysis_model(app_client):
    """Passes pre-fix: the fallback the revert uses already exists, unread."""
    import config

    body = app_client.get("/health").json()
    assert body["analysis_model"] == config.ANALYSIS_MODEL


def test_set_analysis_model_returns_the_model_it_took(app_client, monkeypatch):
    """Passes pre-fix: `data.model` is what the helper records as confirmed."""
    import config

    # The endpoint writes process state; restore it so suite order cannot matter.
    monkeypatch.setattr(config, "ANALYSIS_MODEL", config.ANALYSIS_MODEL)
    body = app_client.post("/set-analysis-model", json={"model": "a-test-model"}).json()
    assert body == {"ok": True, "model": "a-test-model"}
    assert config.ANALYSIS_MODEL == "a-test-model"


def test_set_analysis_model_refuses_an_empty_model_with_a_detail(app_client):
    """Passes pre-fix: one of the refusals the selector displayed as success."""
    res = app_client.post("/set-analysis-model", json={"model": "  "})
    assert res.status_code == 400
    assert res.json()["detail"] == "model is required"


def test_the_chat_selector_still_needs_no_write(source):
    """Passes pre-fix: it rides in the request body, and must stay that way."""
    body = _body(source, "modelSel.addEventListener('change', () => {")
    assert "'/api/set-analysis-model'" not in body
    assert "const body = agentMode ? { message } : { message, model };" in source
