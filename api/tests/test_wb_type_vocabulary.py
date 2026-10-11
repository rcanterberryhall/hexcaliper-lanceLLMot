"""
test_wb_type_vocabulary.py — The Workbench's doc-type selectors against DOC_TYPES.

`DOC_TYPES` in `models.py` is the single definition of the document-type
vocabulary, and the Workbench repeats it on the frontend three times: the
`_WB_DOC_TYPE_OPTIONS` list every selector is generated from, the
`_WB_TYPE_ABBREV` map the completeness grid labels badges with, and the
`_WB_ALL_COMPLETENESS_TYPES` list that grid iterates.  A fourth copy used to
live in `web/index.html` as a hand-written `<option>` list for the type filter,
and it was the one that drifted — four types were added to `DOC_TYPES` and the
markup stayed at eleven, so `technical_manual`, `datasheet`, `firmware_notes`
and `app_note` were uploadable and countable but could not be filtered to
(issue #78).

These tests read the shipped assets and compare each frontend copy against
`DOC_TYPES`, so a sixteenth type fails here rather than silently going missing
from a selector.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB = REPO_ROOT / "web"


def _js_block(name: str, opener: str, closer: str) -> str:
    """Return the body of a top-level `const <name> = <opener>…<closer>` in app.js."""
    app_js = (WEB / "app.js").read_text()
    match = re.search(
        rf"const {re.escape(name)} = {re.escape(opener)}(.*?){re.escape(closer)}",
        app_js,
        re.S,
    )
    assert match, f"Could not locate {name} in web/app.js"
    return match.group(1)


def _wb_doc_type_option_values() -> list[str]:
    block = _js_block("_WB_DOC_TYPE_OPTIONS", "[", "];")
    return re.findall(r"\[\s*'([a-z_]+)'\s*,", block)


def _wb_type_abbrev_keys() -> list[str]:
    block = _js_block("_WB_TYPE_ABBREV", "{", "};")
    return re.findall(r"([a-z_]+):\s*'", block)


def _wb_completeness_types() -> list[str]:
    block = _js_block("_WB_ALL_COMPLETENESS_TYPES", "[", "];")
    return re.findall(r"'([a-z_]+)'", block)


def _wb_type_filter_option_values() -> list[str]:
    """Return the `<option>` values declared in the type filter's markup."""
    html = (WEB / "index.html").read_text()
    match = re.search(r'<select id="wb-type-filter">(.*?)</select>', html, re.S)
    assert match, 'Could not locate <select id="wb-type-filter"> in web/index.html'
    return re.findall(r'<option value="([^"]*)"', match.group(1))


# ── Frontend copies of the vocabulary ────────────────────────────────────────

def test_doc_type_options_cover_every_doc_type():
    from models import DOC_TYPES
    values = _wb_doc_type_option_values()
    assert set(values) == set(DOC_TYPES), (
        "_WB_DOC_TYPE_OPTIONS and DOC_TYPES disagree; missing from the frontend: "
        f"{sorted(set(DOC_TYPES) - set(values))}, unknown to the backend: "
        f"{sorted(set(values) - set(DOC_TYPES))}"
    )
    assert len(values) == len(set(values)), f"Duplicate entries: {values}"


def test_type_abbreviations_cover_every_doc_type():
    from models import DOC_TYPES
    keys = _wb_type_abbrev_keys()
    assert set(keys) == set(DOC_TYPES), (
        "_WB_TYPE_ABBREV has no badge label for: "
        f"{sorted(set(DOC_TYPES) - set(keys))}"
    )


def test_completeness_grid_covers_every_doc_type():
    from models import DOC_TYPES
    types = _wb_completeness_types()
    assert set(types) == set(DOC_TYPES), (
        "_WB_ALL_COMPLETENESS_TYPES omits: "
        f"{sorted(set(DOC_TYPES) - set(types))}"
    )


# ── The type filter is generated, not hand-written ───────────────────────────

def test_type_filter_markup_declares_only_the_all_types_entry():
    """A hardcoded type in the markup is a copy of the vocabulary that can drift."""
    assert _wb_type_filter_option_values() == [""], (
        "web/index.html hardcodes type options in #wb-type-filter: "
        f"{[v for v in _wb_type_filter_option_values() if v]}; the entries are "
        "appended from _WB_DOC_TYPE_OPTIONS so the two cannot fall out of step"
    )


def test_type_filter_is_populated_from_the_shared_list():
    app_js = (WEB / "app.js").read_text()
    assert "_appendWbTypeOptions(wbTypeFilter, null)" in app_js, (
        "Nothing appends _WB_DOC_TYPE_OPTIONS to wbTypeFilter, so the filter "
        "would offer only its All-types entry"
    )
    assert "_appendWbTypeOptions(wbUploadType, 'misc')" in app_js, (
        "The upload selector must still be filled with Misc preselected"
    )
