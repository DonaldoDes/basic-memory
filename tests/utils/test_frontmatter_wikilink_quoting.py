"""Tests for BUG-010: write_note round-trip eats inline non-quoted wikilinks.

Root cause: ``yaml.safe_load`` parses an unquoted ``key: [[path|alias]]`` value as a
YAML flow list-of-list (``[['path|alias']]``), which ``dump_frontmatter`` then re-emits
in block style ``- - path|alias`` — silently breaking the relation.

The fix pre-processes the raw YAML text so a bare single inline wikilink value is
treated as a quoted scalar string BEFORE ``yaml.safe_load`` interprets it as a flow
list. This module exercises that fix at the unit level (``parse_frontmatter``) and
guards the non-regression of legitimate YAML lists.

Zone sensible (constitution § "Parsing markdown / frontmatter") — adversarial tests
come FIRST, then nominal tests.
"""

import pytest

from basic_memory.file_utils import (
    ParseError,
    _is_single_inline_wikilink,
    parse_frontmatter,
)


# ---------------------------------------------------------------------------
# [ADVERSARIAL] — hostile / edge YAML that must NOT corrupt or crash
# ---------------------------------------------------------------------------


def test_adv_bare_wikilink_not_parsed_as_nested_list():
    """ADV: the exact BUG-010 trigger — bare inline wikilink stays a string."""
    content = "---\npart_of: [[projects/Mon Projet|Mon Projet]]\n---\n\nbody"
    fm = parse_frontmatter(content)
    assert fm["part_of"] == "[[projects/Mon Projet|Mon Projet]]"
    # Must NOT be a list (let alone a nested list)
    assert not isinstance(fm["part_of"], list)


def test_adv_genuine_flow_list_not_mistaken_for_wikilink():
    """ADV: a real one-line flow list must stay a list, never get string-quoted."""
    content = "---\ntags: [a, b, c]\n---\n\nbody"
    fm = parse_frontmatter(content)
    assert fm["tags"] == ["a", "b", "c"]


def test_adv_genuine_nested_list_preserved():
    """ADV: a real nested YAML list (multi-element) must NOT be flattened to a string."""
    content = "---\nmatrix: [[a, b], [c, d]]\n---\n\nbody"
    fm = parse_frontmatter(content)
    assert fm["matrix"] == [["a", "b"], ["c", "d"]]


def test_adv_wikilink_without_alias():
    """ADV: bare wikilink with no pipe alias is still preserved as a string."""
    content = "---\nrelated: [[Some Note]]\n---\n\nbody"
    fm = parse_frontmatter(content)
    assert fm["related"] == "[[Some Note]]"


def test_adv_wikilink_with_colon_inside():
    """ADV: wikilink containing a colon (URL-like target) preserved as string."""
    content = "---\nref: [[meeting: 2026-06-12 standup|standup]]\n---\n\nbody"
    fm = parse_frontmatter(content)
    assert fm["ref"] == "[[meeting: 2026-06-12 standup|standup]]"


def test_adv_already_quoted_wikilink_untouched():
    """ADV: a properly single-quoted wikilink survives unchanged (idempotence)."""
    content = "---\npart_of: '[[projects/Mon Projet|Mon Projet]]'\n---\n\nbody"
    fm = parse_frontmatter(content)
    assert fm["part_of"] == "[[projects/Mon Projet|Mon Projet]]"


def test_adv_double_quoted_wikilink_untouched():
    """ADV: a double-quoted wikilink survives unchanged."""
    content = '---\npart_of: "[[projects/Mon Projet|Mon Projet]]"\n---\n\nbody'
    fm = parse_frontmatter(content)
    assert fm["part_of"] == "[[projects/Mon Projet|Mon Projet]]"


def test_adv_wikilink_inside_yaml_block_list_preserved():
    """ADV: a wikilink as a block-list ITEM (already valid YAML) is untouched.

    ``- [[x]]`` is a flow list item inside a block sequence; it parses to a list.
    The fix targets scalar ``key: [[..]]`` lines, not list items, so this stays a list.
    """
    content = "---\nrelated:\n- '[[A]]'\n- '[[B]]'\n---\n\nbody"
    fm = parse_frontmatter(content)
    assert fm["related"] == ["[[A]]", "[[B]]"]


def test_adv_wikilink_with_trailing_comment_like_text():
    """ADV: value that merely contains brackets but is not a pure wikilink list."""
    content = "---\nnote: see [[A]] and [[B]] for context\n---\n\nbody"
    fm = parse_frontmatter(content)
    # Plain prose with embedded wikilinks — YAML already treats it as a scalar string.
    assert fm["note"] == "see [[A]] and [[B]] for context"


def test_adv_empty_value_after_key_not_broken():
    """ADV: empty / null value lines are left alone."""
    content = "---\nmilestone: null\nempty:\n---\n\nbody"
    fm = parse_frontmatter(content)
    assert fm["milestone"] is None
    assert fm["empty"] is None


def test_adv_malformed_bracket_value_raises_controlled_error():
    """ADV: unbalanced brackets (not a valid wikilink) fail with a controlled error.

    A value like ``[[unbalanced`` is not a well-formed wikilink, so the quoting fix
    deliberately does NOT touch it. It remains invalid YAML and ``parse_frontmatter``
    raises its documented ``ParseError`` rather than silently corrupting data — the
    correct security posture for untrusted frontmatter input. The file-read path
    (entity_parser) catches this and degrades to plain markdown instead of crashing.
    """
    content = "---\nweird: [[unbalanced\n---\n\nbody"
    with pytest.raises(ParseError):
        parse_frontmatter(content)


def test_adv_wikilink_value_with_indentation():
    """ADV: indented mapping value (nested key) still quoted correctly."""
    content = "---\nrelations:\n  primary: [[projects/X|X]]\n---\n\nbody"
    fm = parse_frontmatter(content)
    assert fm["relations"]["primary"] == "[[projects/X|X]]"


# ---------------------------------------------------------------------------
# Nominal — representative real fields
# ---------------------------------------------------------------------------


def test_nominal_part_of_relation_field():
    content = "---\ntitle: My Note\npart_of: [[projects/Mon Projet|Mon Projet]]\n---\n\nbody"
    fm = parse_frontmatter(content)
    assert fm["title"] == "My Note"
    assert fm["part_of"] == "[[projects/Mon Projet|Mon Projet]]"


def test_nominal_multiple_relation_fields():
    content = "---\npart_of: [[a|A]]\nreferences: [[b|B]]\nimplements: [[c|C]]\n---\n\nbody"
    fm = parse_frontmatter(content)
    assert fm["part_of"] == "[[a|A]]"
    assert fm["references"] == "[[b|B]]"
    assert fm["implements"] == "[[c|C]]"


def test_nominal_relation_alongside_real_list():
    content = "---\npart_of: [[a|A]]\ntags: [x, y]\n---\n\nbody"
    fm = parse_frontmatter(content)
    assert fm["part_of"] == "[[a|A]]"
    assert fm["tags"] == ["x", "y"]


# ---------------------------------------------------------------------------
# BUG-011 Résidu 1 — [ADVERSARIAL] block-sequence wikilink coverage
#
# Root cause: an UNquoted block-sequence item ``- [[x|y]]`` round-trips through
# ``yaml.safe_load`` as ``[[['x|y']]]`` and re-dumps as ``- - - x|y`` — the same
# class of silent corruption as BUG-010 inline, but on list-item lines.
# ---------------------------------------------------------------------------


def test_adv_block_sequence_bare_wikilink_not_parsed_as_nested_list():
    """ADV: the exact BUG-011 trigger — bare block-seq wikilink item stays a string."""
    content = "---\nrefs:\n- [[projects/Mon Projet|Mon Projet]]\n---\n\nbody"
    fm = parse_frontmatter(content)
    assert fm["refs"] == ["[[projects/Mon Projet|Mon Projet]]"]
    # Each item must be a plain string, never a nested list (the corruption signature).
    for item in fm["refs"]:
        assert isinstance(item, str)


def test_adv_block_sequence_multiple_bare_wikilink_items():
    """ADV: several bare block-seq wikilink items each preserved as strings."""
    content = "---\nrefs:\n- [[a|A]]\n- [[b|B]]\n- [[Some Note]]\n---\n\nbody"
    fm = parse_frontmatter(content)
    assert fm["refs"] == ["[[a|A]]", "[[b|B]]", "[[Some Note]]"]


def test_adv_block_sequence_indented_bare_wikilink():
    """ADV: indented block-seq items (2-space indent) quoted correctly."""
    content = "---\nrefs:\n  - [[a|A]]\n  - [[b|B]]\n---\n\nbody"
    fm = parse_frontmatter(content)
    assert fm["refs"] == ["[[a|A]]", "[[b|B]]"]


def test_adv_block_sequence_wikilink_with_colon_inside():
    """ADV: block-seq wikilink containing a colon preserved as string."""
    content = "---\nrefs:\n- [[meeting: 2026-06-12 standup|standup]]\n---\n\nbody"
    fm = parse_frontmatter(content)
    assert fm["refs"] == ["[[meeting: 2026-06-12 standup|standup]]"]


def test_adv_block_sequence_already_quoted_items_untouched():
    """ADV (non-regression): already-quoted block-seq items survive unchanged."""
    content = "---\nrefs:\n- '[[A]]'\n- '[[B]]'\n---\n\nbody"
    fm = parse_frontmatter(content)
    assert fm["refs"] == ["[[A]]", "[[B]]"]


# ---------------------------------------------------------------------------
# BUG-011 Résidu 1 — [ADVERSARIAL] no over-quoting of genuine list items (AC-3)
# ---------------------------------------------------------------------------


def test_adv_block_sequence_plain_string_items_not_quoted():
    """ADV: ordinary block-seq string items (no wikilink) stay plain strings."""
    content = "---\ntags:\n- foo\n- bar\n---\n\nbody"
    fm = parse_frontmatter(content)
    assert fm["tags"] == ["foo", "bar"]


def test_adv_block_sequence_genuine_nested_flow_list_item_preserved():
    """ADV: a real nested flow-list item ``- [a, b]`` must stay a list, not a string."""
    content = "---\nmatrix:\n- [a, b]\n- [c, d]\n---\n\nbody"
    fm = parse_frontmatter(content)
    assert fm["matrix"] == [["a", "b"], ["c", "d"]]


def test_adv_block_sequence_genuine_nested_wikilink_list_item_preserved():
    """ADV: ``- [[a, b], [c, d]]`` is a genuine nested list, not a single wikilink."""
    content = "---\nmatrix:\n- [[a, b], [c, d]]\n---\n\nbody"
    fm = parse_frontmatter(content)
    assert fm["matrix"] == [[["a", "b"], ["c", "d"]]]


# ---------------------------------------------------------------------------
# BUG-011 Résidu 2 — [ADVERSARIAL] depth < 0 guard in _is_single_inline_wikilink
# ---------------------------------------------------------------------------


def test_adv_is_single_inline_wikilink_unbalanced_closing_brackets_false():
    """ADV: surplus ``]`` (depth would go negative) must return False, not True.

    Defensive guard (BUG-011 résidu 2): if the helper is reused without the upstream
    ``startswith('[[')``/``endswith(']]')`` gates, an orphan ``]`` must not yield a
    false positive. ``[[x]]]`` strips to inner ``[x]]`` which drives depth below 0.
    """
    assert _is_single_inline_wikilink("[[x]]]") is False


def test_adv_is_single_inline_wikilink_nominal_still_true():
    """ADV (non-regression): a well-formed single wikilink still returns True."""
    assert _is_single_inline_wikilink("[[a|b]]") is True


def test_adv_is_single_inline_wikilink_genuine_list_false():
    """ADV (non-regression): a genuine multi-element flow list returns False."""
    assert _is_single_inline_wikilink("[[a, b], [c, d]]") is False
