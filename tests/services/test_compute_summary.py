"""Unit tests for the pure summary calculation (US-006a, Test IDs SUM-01..SUM-06).

`compute_summary(description, content_snippet)` is a pure, deterministic function
(no LLM, no I/O) that derives the short ~200-char summary persisted at indexation.
"""

from basic_memory.services.search_service import (
    SUMMARY_MAX_LENGTH,
    SUMMARY_SENTENCE_WINDOW,
    compute_summary,
)


# ---------------------------------------------------------------------------
# SUM-01 — description present takes priority, bounded to ~200 chars
# ---------------------------------------------------------------------------


def test_sum01_description_priority():
    """SUM-01: a non-empty description is used verbatim (normalized)."""
    assert compute_summary("A short description", "some other content") == "A short description"


def test_sum01_description_wins_over_content():
    assert compute_summary("From frontmatter", "From body lead") == "From frontmatter"


def test_sum01_description_bounded():
    """SUM-01: an oversized description is capped near SUMMARY_MAX_LENGTH."""
    long_desc = "word " * 100  # 500 chars, spaces present
    result = compute_summary(long_desc, None)
    assert result is not None
    # bounded: content part <= max, plus an optional ellipsis
    assert len(result) <= SUMMARY_MAX_LENGTH + 1
    assert result.endswith("…")


# ---------------------------------------------------------------------------
# SUM-02 — fallback on content lead when no description
# ---------------------------------------------------------------------------


def test_sum02_fallback_content_lead():
    """SUM-02: no description → derived from content_snippet lead."""
    assert compute_summary(None, "Lead sentence of the note.") == "Lead sentence of the note."


def test_sum02_empty_description_falls_back():
    """A whitespace-only description is treated as absent."""
    assert compute_summary("   ", "Body lead here.") == "Body lead here."


def test_sum02_fallback_bounded():
    long_body = "alpha " * 100
    result = compute_summary(None, long_body)
    assert result is not None
    assert len(result) <= SUMMARY_MAX_LENGTH + 1


# ---------------------------------------------------------------------------
# SUM-03 — clean cut, never mid-word; ellipsis only if truncated
# ---------------------------------------------------------------------------


def test_sum03_no_ellipsis_when_not_truncated():
    """SUM-03: text under the cap is returned without a truncation suffix."""
    text = "This fits well under the limit."
    assert compute_summary(None, text) == text
    assert "…" not in compute_summary(None, text)


def test_sum03_word_boundary_cut_with_ellipsis():
    """SUM-03: long text without a sentence end cuts on a space and adds `…`."""
    text = "word " * 60  # 300 chars, no sentence punctuation
    result = compute_summary(None, text)
    assert result is not None
    assert result.endswith("…")
    stripped = result[:-1].rstrip()
    # never cut mid-word: the retained prefix is a whole-word prefix of the source
    normalized_source = "word " * 60
    assert normalized_source.startswith(stripped)
    # the character following the retained prefix in the source is a space boundary
    assert normalized_source[len(stripped)] == " "
    assert len(result) <= SUMMARY_MAX_LENGTH + 1


# ---------------------------------------------------------------------------
# SUM-04 — cut on a sentence end within the tolerance window
# ---------------------------------------------------------------------------


def test_sum04_sentence_boundary_cut():
    """SUM-04: a sentence end inside [SENTENCE_WINDOW, MAX] ends the summary cleanly."""
    head = "word " * 30  # 150 chars
    text = head + "Final sentence. " + "tail " * 50
    result = compute_summary(None, text)
    assert result is not None
    assert result.endswith(".")
    assert "…" not in result
    assert SUMMARY_SENTENCE_WINDOW <= len(result) <= SUMMARY_MAX_LENGTH
    assert result.endswith("Final sentence.")


# ---------------------------------------------------------------------------
# SUM-05 — clean degradation to None (no error)
# ---------------------------------------------------------------------------


def test_sum05_none_when_no_source():
    assert compute_summary(None, None) is None


def test_sum05_none_when_all_empty():
    assert compute_summary("", "") is None
    assert compute_summary("   ", "   ") is None


def test_sum05_none_when_content_only_heading():
    """A lead reduced to nothing after cleaning yields None, not an error."""
    assert compute_summary(None, "# Just a heading\n\n") is None


def test_sum05_none_when_content_only_frontmatter():
    assert compute_summary(None, "---\ntitle: x\ntype: note\n---\n") is None


# ---------------------------------------------------------------------------
# SUM-06 — lead cleaning: residual frontmatter + H1 removed, whitespace
#          normalized to one line, NUL stripped
# ---------------------------------------------------------------------------


def test_sum06_strips_residual_frontmatter_and_heading():
    snippet = "---\ntitle: Note\ntype: note\n---\n# Heading\n\nActual body starts here."
    assert compute_summary(None, snippet) == "Actual body starts here."


def test_sum06_normalizes_whitespace_single_line():
    snippet = "# Title\n\nFirst line\nsecond   line\n\nthird line"
    result = compute_summary(None, snippet)
    assert result == "First line second line third line"
    assert "\n" not in result


def test_sum06_strips_nul_bytes():
    snippet = "Body with\x00 embedded nul\x00 bytes."
    result = compute_summary(None, snippet)
    assert result is not None
    assert "\x00" not in result
    assert result == "Body with embedded nul bytes."


def test_sum06_description_normalized_single_line_no_nul():
    result = compute_summary("multi\nline\x00 description", None)
    assert result == "multi line description"
    assert "\n" not in result
    assert "\x00" not in result


# ---------------------------------------------------------------------------
# Adversarial (sensitive zone: values persisted into search_index) — the
# function must never raise and never emit newline/NUL, regardless of hostile
# input. Injection safety of the persisted value is enforced by parameterized
# INSERT (covered in the integration tests), but the computed value itself must
# already be sanitized.
# ---------------------------------------------------------------------------


def test_adversarial_sql_and_fts_metacharacters_do_not_break():
    hostile = '"; DROP TABLE search_index; -- MATCH * AND OR "quoted"'
    result = compute_summary(hostile, None)
    assert result is not None
    assert "\n" not in result and "\x00" not in result
    # value preserved as-is (single line), sanitization happens at the SQL layer
    assert "DROP TABLE" in result


def test_adversarial_only_whitespace_and_control_chars():
    assert compute_summary("\n\t  \x00 ", None) is None


def test_adversarial_very_long_single_token_is_bounded():
    result = compute_summary("A" * 5000, None)
    assert result is not None
    assert len(result) <= SUMMARY_MAX_LENGTH + 1
    assert "\n" not in result


def test_adversarial_crlf_and_tabs_normalized():
    result = compute_summary(None, "line1\r\n\tline2\r\nline3")
    assert result is not None
    assert "\r" not in result and "\n" not in result and "\t" not in result
