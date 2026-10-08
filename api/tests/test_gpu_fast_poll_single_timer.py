"""
test_gpu_fast_poll_single_timer.py — the fast GPU poll has one owner (#68).

The sidebar attach upload and the Workbench batch upload both want the 500 ms
``pollGpu`` tick and both suppress the model-status polls while they run. When
each path armed and cleared ``gpuFastInterval`` itself, starting one upload
while the other was still running orphaned an interval that nothing held, and
``clearInterval`` of the lost handle is a silent no-op — so it polled
``/api/gpu`` twice a second for the life of the page.

These are text properties of the shipped ``web/app.js``, so the regression they
guard against is a future edit arming the handle outside the helper again. Two
of them — the sidebar upload still marking the model dot busy, and the slow
3 s heartbeat still running — pass before the fix on purpose: without them the
suite could be satisfied by deleting the fast poll rather than giving it one
owner.
"""
import re
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[2] / "web" / "app.js"

ARM = "gpuFastInterval = setInterval(pollGpu, 500)"
CLEAR = "clearInterval(gpuFastInterval)"


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


# ── One owner ────────────────────────────────────────────────────────────────

def test_handle_has_one_arming_site_and_one_teardown_site(source):
    """More arming sites than teardown sites is the defect itself."""
    assert source.count(ARM) == 1, "the fast GPU poll is armed in more than one place"
    assert source.count(CLEAR) == 1, "gpuFastInterval is cleared in more than one place"


def test_the_arming_site_is_inside_start_gpu_fast_poll(source):
    assert ARM in _body(source, "function startGpuFastPoll()")


def test_the_teardown_site_is_inside_stop_gpu_fast_poll(source):
    assert CLEAR in _body(source, "function stopGpuFastPoll()")


def test_arming_clears_any_running_poll_before_assigning(source):
    """Idempotent arming is what makes a second upload unable to orphan the first."""
    body = _body(source, "function startGpuFastPoll()")
    assert body.index("stopGpuFastPoll()") < body.index(ARM)


def test_no_other_top_level_function_arms_the_fast_poll(source):
    """`setInterval(pollGpu, 500)` outside the helper is the regression."""
    assert len(re.findall(r"setInterval\(pollGpu,\s*500\)", source)) == 1


# ── Both upload paths go through the helpers ─────────────────────────────────

UPLOAD_PATHS = [
    ("async function _uploadDoc(", "the sidebar attach upload"),
    ("wbDocUpload.addEventListener(", "the Workbench batch upload"),
]


@pytest.mark.parametrize("declaration,label", UPLOAD_PATHS)
def test_upload_path_begins_and_ends_through_the_helpers(source, declaration, label):
    body = _body(source, declaration)
    assert "beginUpload()" in body, f"{label} does not register its start"
    assert "endUpload()" in body, f"{label} does not register its finish"


@pytest.mark.parametrize("declaration,label", UPLOAD_PATHS)
def test_upload_path_does_not_touch_the_handle_directly(source, declaration, label):
    body = _body(source, declaration)
    assert ARM not in body, f"{label} arms the poll itself"
    assert CLEAR not in body, f"{label} clears the poll itself"


# ── The count, not a flag ────────────────────────────────────────────────────

def test_in_flight_uploads_are_counted(source):
    """A boolean cannot express "release when the last upload finishes"."""
    assert "let uploadsInFlight = 0;" in source
    assert "uploadInProgress" not in source
    assert "uploadsInFlight++" in _body(source, "function beginUpload()")


def test_the_poll_stops_only_when_the_last_upload_finishes(source):
    body = _body(source, "function endUpload()")
    assert "uploadsInFlight === 0" in body, "endUpload stops the poll unconditionally"
    assert "stopGpuFastPoll()" in body


@pytest.mark.parametrize(
    "declaration",
    ["async function pollModelStatus()", "async function pollAnalysisModelStatus()"],
)
def test_model_status_polls_defer_while_any_upload_runs(source, declaration):
    """Releasing early let these overwrite the dot mid-upload."""
    assert "if (uploadsInFlight > 0) return;" in _body(source, declaration)


# ── Guards that pass before the fix ──────────────────────────────────────────

def test_sidebar_upload_still_reports_the_model_as_busy(source):
    """Passes pre-fix: the busy dot is wanted behaviour, not the defect."""
    body = _body(source, "async function _uploadDoc(")
    assert "modelDotLabel.textContent = 'Busy…';" in body


def test_the_slow_gpu_heartbeat_is_untouched(source):
    """Passes pre-fix: the 3 s page-lifetime tick is separate from the fast poll."""
    assert "setInterval(pollGpu, 3000);" in source
