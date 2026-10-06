"""
test_web_table_markup.py — Column-span consistency for the Workbench document table.

The table's width is declared once, in the `<thead>` of `web/index.html`; every
full-width row in `web/app.js` has to repeat that number by hand.  A row that
spans fewer columns than the table has gets no filler cell — the browser leaves
that column's slot empty, so the row's background and borders stop short of the
right edge (issue #64, where the inline edit row spanned 6 of 7 columns).

These tests read the shipped assets and compare the emitted spans against the
header count, so adding or removing a column fails here rather than in a
browser.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB = REPO_ROOT / "web"


def _wb_table_column_count() -> int:
    """Count the `<th>` cells in the Workbench document table's header row."""
    html = (WEB / "index.html").read_text()
    thead = re.search(
        r'<table class="wb-table">.*?<thead>(.*?)</thead>', html, re.S
    )
    assert thead, "Could not locate the wb-table <thead> in web/index.html"
    columns = re.findall(r"<th[\s>]", thead.group(1))
    assert columns, "Found no <th> cells in the wb-table header row"
    return len(columns)


# ── Full-width rows emitted by web/app.js ────────────────────────────────────

def test_inline_edit_row_spans_every_column():
    expected = _wb_table_column_count()
    app_js = (WEB / "app.js").read_text()
    match = re.search(r'editTr\.innerHTML = `<td colspan="(\d+)">', app_js)
    assert match, "Could not locate the inline edit row's <td> in web/app.js"
    assert int(match.group(1)) == expected, (
        f'Inline edit row spans {match.group(1)} of the table\'s {expected} '
        f"columns; it sits between document rows and must span all of them"
    )


def test_rendered_empty_state_row_spans_every_column():
    expected = _wb_table_column_count()
    app_js = (WEB / "app.js").read_text()
    match = re.search(
        r'wbDocTbody\.innerHTML = `<tr class="wb-empty-row"><td colspan="(\d+)">',
        app_js,
    )
    assert match, "Could not locate the rendered empty-state row in web/app.js"
    assert int(match.group(1)) == expected


# ── Static markup in web/index.html ──────────────────────────────────────────

def test_initial_empty_state_row_spans_every_column():
    expected = _wb_table_column_count()
    html = (WEB / "index.html").read_text()
    match = re.search(
        r'<tr class="wb-empty-row"><td colspan="(\d+)">', html
    )
    assert match, "Could not locate the initial empty-state row in web/index.html"
    assert int(match.group(1)) == expected


def test_rendered_document_row_cell_count_matches_header():
    """A document row's own `<td>`s define what the full-width rows must span."""
    expected = _wb_table_column_count()
    app_js = (WEB / "app.js").read_text()
    row = re.search(
        r'tr\.innerHTML = `\s*(<td.*?)`;\s*\n\s*wbDocTbody\.appendChild\(tr\)',
        app_js,
        re.S,
    )
    assert row, "Could not locate the document row template in web/app.js"
    cells = re.findall(r"<td[\s>]", row.group(1))
    assert len(cells) == expected, (
        f"Document row emits {len(cells)} cells for a {expected}-column table"
    )
