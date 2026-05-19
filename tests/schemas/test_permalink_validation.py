"""Tests for Entity `_permalink` validation at write time (BUG-002).

These tests cover the validation layer that rejects invalid permalinks before they
reach the database. The root cause of the 11 corrupted vault notes fixed on
2026-05-19 was the legacy Bear-import numeric-prefix pattern (`3.-foo`, `42-bar`).
Dots inside version-number segments (e.g. `version-2.0.0`) are explicitly allowed
because the existing `generate_permalink()` produces them intentionally.

See: products/Basic Memory/milestones/M-Hardening/bugs/BUG-002 Permalink validation at write.md
"""

import pytest

from basic_memory.schemas import Entity
from basic_memory.schemas.base import validate_permalink_format


# ---------------------------------------------------------------------------
# Module-level validator function tests
# ---------------------------------------------------------------------------


class TestValidatePermalinkFormat:
    """Direct tests for the validate_permalink_format() helper function."""

    # -- Sentinel values pass through unchanged --------------------------------

    def test_none_is_allowed(self):
        """None means 'auto-generate via generate_permalink' — must be accepted."""
        assert validate_permalink_format(None) is None

    def test_empty_string_is_allowed(self):
        """Empty string is the sentinel for 'permalinks disabled' — must be accepted."""
        assert validate_permalink_format("") == ""

    # -- Numeric-prefix rejection (the actual BUG-002 root cause) --------------

    def test_rejects_numeric_prefix_dot_dash(self):
        """The Bear-import legacy `3.-` numeric prefix must be rejected."""
        with pytest.raises(ValueError, match=r"numeric prefix"):
            validate_permalink_format("3.-resources/test/x")

    def test_rejects_numeric_prefix_dash(self):
        """Numeric prefix variant `42-foo` must also be rejected."""
        with pytest.raises(ValueError, match=r"numeric prefix"):
            validate_permalink_format("42-foo/bar")

    def test_rejects_numeric_prefix_dot(self):
        """Numeric prefix variant `7.foo` must also be rejected."""
        with pytest.raises(ValueError, match=r"numeric prefix"):
            validate_permalink_format("7.foo")

    # -- Malformed dot pattern rejection --------------------------------------

    def test_rejects_double_dot(self):
        """Consecutive dots `..` are malformed."""
        with pytest.raises(ValueError, match=r"malformed dot"):
            validate_permalink_format("foo..bar")

    def test_rejects_dot_dash(self):
        """`.-` sequence (mid-string) is malformed."""
        with pytest.raises(ValueError, match=r"malformed dot"):
            validate_permalink_format("foo.-bar")

    def test_rejects_dash_dot(self):
        """`-.` sequence is malformed."""
        with pytest.raises(ValueError, match=r"malformed dot"):
            validate_permalink_format("foo-.bar")

    def test_rejects_dot_slash(self):
        """`./` sequence is malformed (dot adjacent to path separator)."""
        with pytest.raises(ValueError, match=r"malformed dot"):
            validate_permalink_format("foo./bar")

    def test_rejects_slash_dot(self):
        """`/.` sequence is malformed (dot adjacent to path separator)."""
        with pytest.raises(ValueError, match=r"malformed dot"):
            validate_permalink_format("foo/.bar")

    def test_rejects_trailing_dot(self):
        """A trailing `.` is malformed."""
        with pytest.raises(ValueError, match=r"malformed dot"):
            validate_permalink_format("foo.")

    def test_rejects_leading_dot(self):
        """A leading `.` violates the first-char-alphanumeric rule (caught by regex)."""
        with pytest.raises(ValueError, match=r"must match"):
            validate_permalink_format(".foo")

    # -- Dots in version numbers ARE allowed (existing codebase contract) -----

    def test_accepts_version_two_dot_zero(self):
        """`v2.0` style version segments are valid (preserved by generate_permalink)."""
        assert validate_permalink_format("test-3.0-version") == "test-3.0-version"

    def test_accepts_multi_dot_version(self):
        """Multi-dot version segments like `1.2.3` are valid."""
        assert validate_permalink_format("version-1.2.3-release") == "version-1.2.3-release"

    def test_accepts_v_prefixed_version(self):
        """`v2.0` style in compound segments is valid."""
        assert validate_permalink_format("feature-v2.0-update") == "feature-v2.0-update"

    def test_accepts_hierarchical_with_version(self):
        """Version dots inside a hierarchical permalink path segment are valid."""
        assert (
            validate_permalink_format("project/feature-v3.0-update/spec")
            == "project/feature-v3.0-update/spec"
        )

    # -- Valid kebab-case shapes ----------------------------------------------

    def test_accepts_simple_kebab_permalink(self):
        """Standard kebab-case permalinks are valid."""
        assert validate_permalink_format("my-note") == "my-note"

    def test_accepts_hierarchical_permalink(self):
        """Hierarchical permalinks with `/` separators are valid."""
        assert validate_permalink_format("project/foo/bar-baz") == "project/foo/bar-baz"

    def test_accepts_underscore(self):
        """Underscores are allowed in permalinks (legacy compatibility)."""
        assert validate_permalink_format("my_note/sub_dir") == "my_note/sub_dir"

    def test_accepts_digits_after_first_char(self):
        """Digits are allowed after the first char (e.g., `note-42`, `v2-spec`)."""
        assert validate_permalink_format("note-42") == "note-42"
        assert validate_permalink_format("api-v2") == "api-v2"

    # -- Non-kebab character rejection ----------------------------------------

    def test_rejects_uppercase(self):
        """Uppercase letters violate kebab-case strictness."""
        with pytest.raises(ValueError, match=r"must match"):
            validate_permalink_format("MyNote")

    def test_rejects_leading_dash(self):
        """Leading dashes are forbidden — first char must be alphanumeric."""
        with pytest.raises(ValueError, match=r"must match"):
            validate_permalink_format("-foo")

    def test_rejects_leading_slash(self):
        """Leading `/` is forbidden — permalinks are relative."""
        with pytest.raises(ValueError, match=r"must match"):
            validate_permalink_format("/foo/bar")

    def test_rejects_spaces(self):
        """Spaces are forbidden — permalinks must be URL-safe."""
        with pytest.raises(ValueError, match=r"must match"):
            validate_permalink_format("my note")

    def test_rejects_special_chars(self):
        """Special characters (`?`, `&`, `#`, etc.) are forbidden."""
        with pytest.raises(ValueError, match=r"must match"):
            validate_permalink_format("foo?bar")

    def test_error_message_includes_offending_value(self):
        """The ValueError message must include the rejected permalink for debuggability."""
        with pytest.raises(ValueError, match=r"3\.-bad"):
            validate_permalink_format("3.-bad")


# ---------------------------------------------------------------------------
# Entity schema integration tests
# ---------------------------------------------------------------------------


class TestEntityPermalinkValidationAtConstruction:
    """Validate that Entity construction triggers permalink validation on `_permalink`."""

    def test_entity_accepts_none_permalink(self):
        """Default construction (None _permalink) must work — auto-generation kicks in."""
        e = Entity.model_validate({"title": "Test", "directory": "test"})
        assert e._permalink is None
        # permalink property still resolves via generate_permalink
        assert e.permalink == "test/test"

    def test_entity_rejects_numeric_prefix_at_assignment(self):
        """Direct attribute assignment of a Bear-legacy `_permalink` must raise."""
        e = Entity.model_validate({"title": "Test", "directory": "test"})
        with pytest.raises(ValueError, match=r"numeric prefix"):
            e._permalink = "3.-resources/test/x"

    def test_entity_accepts_valid_permalink_at_assignment(self):
        """Direct attribute assignment of a valid `_permalink` must succeed."""
        e = Entity.model_validate({"title": "Test", "directory": "test"})
        e._permalink = "resources/test/x"
        assert e._permalink == "resources/test/x"
        assert e.permalink == "resources/test/x"

    def test_entity_accepts_version_dot_permalink_at_assignment(self):
        """Dots in version numbers (`v2.0`) must be accepted via assignment."""
        e = Entity.model_validate({"title": "Test", "directory": "test"})
        e._permalink = "test/feature-v2.0-update"
        assert e._permalink == "test/feature-v2.0-update"
        assert e.permalink == "test/feature-v2.0-update"

    def test_entity_accepts_empty_string_assignment(self):
        """Empty string sentinel (permalinks disabled) must remain valid via assignment."""
        e = Entity.model_validate({"title": "Test", "directory": "test"})
        e._permalink = ""
        assert e._permalink == ""
        assert e.permalink is None

    def test_entity_rejects_uppercase_at_assignment(self):
        """Uppercase in `_permalink` via direct assignment must be rejected."""
        e = Entity.model_validate({"title": "Test", "directory": "test"})
        with pytest.raises(ValueError, match=r"must match"):
            e._permalink = "Resources/Test/X"

    def test_entity_rejects_leading_dash_at_assignment(self):
        """Leading dash in `_permalink` via direct assignment must be rejected."""
        e = Entity.model_validate({"title": "Test", "directory": "test"})
        with pytest.raises(ValueError, match=r"must match"):
            e._permalink = "-bad-start"

    def test_entity_rejects_malformed_dot_via_assignment(self):
        """End-to-end: a `..` doubled-dot permalink must surface a clear error."""
        e = Entity.model_validate({"title": "Test", "directory": "test"})
        with pytest.raises(ValueError) as exc_info:
            e._permalink = "notes/spec..draft/foo"
        # Error must identify the offending permalink for debuggability
        assert "spec..draft" in str(exc_info.value)
