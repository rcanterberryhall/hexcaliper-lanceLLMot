"""
test_web_bubble_cascade.py — Specificity of the chat bubble's state modifiers.

A chat bubble is painted by a contextual rule — `.message.ai .bubble` and
`.message.user .bubble` both set the `border` shorthand — and marked by a state
modifier written beside it, `.bubble.error`. The contextual rules match at three
classes and the modifier at two, and specificity is resolved before source order
is consulted, so an unscoped modifier that sets any `border` property is simply
painted over. Nothing else sets `color` on the bubble, so the state still
applied by halves: red text inside the ordinary purple border (issue #86, and
the same trap on `.bubble.incomplete` in #56).

These tests read the shipped stylesheet and compare class-count specificity
between the two kinds of rule, so a modifier that cannot win fails here rather
than needing a browser to notice it has gone quiet.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB = REPO_ROOT / "web"


def _rules() -> list[tuple[str, str]]:
    """Return every `(selector, declarations)` pair in web/styles.css.

    Comments are stripped first; at-rule wrappers such as `@media` are skipped
    by the brace pattern, which keeps the rules nested inside them.
    """
    css = re.sub(r"/\*.*?\*/", "", (WEB / "styles.css").read_text(), flags=re.S)
    pairs = []
    for selector_list, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        for selector in selector_list.split(","):
            selector = selector.strip()
            if selector:
                pairs.append((selector, body))
    assert pairs, "Parsed no rules out of web/styles.css"
    return pairs


def _class_count(selector: str) -> int:
    """Count the class selectors in `selector` — the specificity axis at issue."""
    return len(re.findall(r"\.[A-Za-z_-][\w-]*", selector))


def _subject(selector: str) -> str:
    """Return the selector's rightmost compound — the element it actually styles."""
    return selector.split()[-1]


def _sets_border(declarations: str) -> bool:
    return re.search(r"(^|;)\s*border[\w-]*\s*:", declarations) is not None


def _bubble_border_rules() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Split the border-setting `.bubble` rules into (contextual, state modifier).

    Contextual rules style a bare `.bubble` from an ancestor; state modifiers
    carry a second class on the bubble itself. Rules whose subject is a
    descendant of the bubble (`.message.ai .bubble td`) style something else and
    are left out of both lists.
    """
    contextual, modifiers = [], []
    for selector, declarations in _rules():
        subject = _subject(selector)
        if ".bubble" not in subject or not _sets_border(declarations):
            continue
        if re.fullmatch(r"\.bubble", subject):
            contextual.append((selector, declarations))
        else:
            modifiers.append((selector, declarations))
    return contextual, modifiers


def test_bubble_state_modifiers_outspecify_the_rules_that_paint_the_border():
    contextual, modifiers = _bubble_border_rules()
    assert contextual, "Found no contextual rule setting a border on .bubble"
    assert modifiers, "Found no .bubble state modifier setting a border"

    strongest = max(_class_count(selector) for selector, _ in contextual)
    for selector, _ in modifiers:
        assert _class_count(selector) > strongest, (
            f"`{selector}` matches at {_class_count(selector)} classes but a "
            f"contextual rule sets the bubble's border at {strongest}; "
            f"specificity decides before source order, so this modifier's "
            f"border declarations never paint. Scope it under the same "
            f"ancestor, as .message.ai .bubble.error is."
        )


def test_the_error_state_marks_both_the_border_and_the_text():
    """Both halves of the error state are live, in the context chat builds it.

    All three paths that produce an error bubble pass `role` `'ai'` to
    `createMessage` or add the class to a `createStreamingBubble` bubble, so
    every one of them lands inside `.message.ai`.
    """
    error_rules = [
        (selector, declarations)
        for selector, declarations in _rules()
        if _subject(selector) == ".bubble.error"
    ]
    assert error_rules, "Found no rule for the .bubble.error state"
    for selector, declarations in error_rules:
        assert selector.startswith(".message.ai "), (
            f"`{selector}` is not scoped to .message.ai, the context every "
            f"error bubble is built in"
        )
        for prop in ("border-color", "color"):
            assert re.search(rf"(^|;)\s*{prop}\s*:", declarations), (
                f"`{selector}` does not set {prop}; the error state marks the "
                f"border and the text, and a half-applied state reads as an "
                f"ordinary answer"
            )
