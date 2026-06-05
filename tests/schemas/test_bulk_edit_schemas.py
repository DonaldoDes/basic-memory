"""Tests for bulk edit Pydantic schemas.

Covers spec test IDs (specs/bulk-edit-notes/spec.md):
- BULK-01: batch bounds (1..100 items)
- BULK-09: memory:// and workspace/project-qualified identifiers rejected (I-6)
- BULK-12: size limits — 1 MiB per content (I-2), 10 MiB cumulative (I-3)
- BULK-13 (schema part): expected_replacements >= 1 (documented divergence
  vs single-note edit_note which accepts 0)

Also covers the path traversal helper used by the batch endpoint (I-1).
"""

import pytest
from pydantic import ValidationError

from basic_memory.schemas.v2.bulk_edit import (
    MAX_CONTENT_BYTES,
    MAX_TOTAL_CONTENT_BYTES,
    BulkEditItemResult,
    BulkEditOperation,
    BulkEditRequest,
    BulkEditResponse,
    is_path_traversal_identifier,
)


def make_edit(**overrides) -> dict:
    """Build a valid edit operation payload, overridable per test."""
    edit = {
        "identifier": "notes/test-note",
        "operation": "append",
        "content": "appended content",
    }
    edit.update(overrides)
    return edit


# --- BULK-01: batch bounds ---


def test_empty_edits_rejected():
    with pytest.raises(ValidationError):
        BulkEditRequest(edits=[])


def test_101_edits_rejected():
    edits = [make_edit(identifier=f"notes/note-{i}") for i in range(101)]
    with pytest.raises(ValidationError):
        BulkEditRequest(edits=edits)


def test_single_edit_accepted():
    request = BulkEditRequest(edits=[make_edit()])
    assert len(request.edits) == 1


def test_100_edits_accepted():
    edits = [make_edit(identifier=f"notes/note-{i}") for i in range(100)]
    request = BulkEditRequest(edits=edits)
    assert len(request.edits) == 100


def test_request_defaults():
    request = BulkEditRequest(edits=[make_edit()])
    assert request.validate_first is False
    assert request.stop_on_error is False
    assert request.edits[0].expected_replacements == 1


# --- BULK-09: identifier scoping (I-6, single-project) ---


@pytest.mark.parametrize(
    "identifier",
    [
        "memory://notes/test-note",
        "memory://workspace/project/notes/test-note",
        "  memory://notes/test-note",
    ],
)
def test_memory_url_identifier_rejected(identifier):
    with pytest.raises(ValidationError, match="memory://"):
        BulkEditOperation(**make_edit(identifier=identifier))


@pytest.mark.parametrize(
    "identifier",
    [
        "other-project::notes/test-note",
        "workspace/project::note",
    ],
)
def test_project_qualified_identifier_rejected(identifier):
    with pytest.raises(ValidationError, match="qualified"):
        BulkEditOperation(**make_edit(identifier=identifier))


def test_empty_identifier_rejected():
    with pytest.raises(ValidationError):
        BulkEditOperation(**make_edit(identifier=""))


def test_plain_deep_permalink_accepted():
    """Multi-segment permalinks inside the project remain valid identifiers."""
    op = BulkEditOperation(**make_edit(identifier="products/basic-memory/notes/deep-note"))
    assert op.identifier == "products/basic-memory/notes/deep-note"


# --- BULK-12: size limits (I-2 / I-3) ---


def test_content_over_1mib_rejected():
    oversized = "x" * (MAX_CONTENT_BYTES + 1)
    with pytest.raises(ValidationError, match="1 MiB"):
        BulkEditOperation(**make_edit(content=oversized))


def test_content_exactly_1mib_accepted():
    content = "x" * MAX_CONTENT_BYTES
    op = BulkEditOperation(**make_edit(content=content))
    assert len(op.content) == MAX_CONTENT_BYTES


def test_content_size_measured_in_bytes_not_chars():
    """Multibyte characters must count by encoded size (I-2)."""
    # 'é' encodes to 2 bytes in UTF-8: this exceeds the byte limit while
    # staying under the char-count limit.
    content = "é" * ((MAX_CONTENT_BYTES // 2) + 1)
    with pytest.raises(ValidationError, match="1 MiB"):
        BulkEditOperation(**make_edit(content=content))


def test_cumulative_content_over_10mib_rejected():
    chunk = "x" * MAX_CONTENT_BYTES  # 1 MiB each
    edits = [make_edit(identifier=f"notes/note-{i}", content=chunk) for i in range(11)]
    with pytest.raises(ValidationError, match="10 MiB"):
        BulkEditRequest(edits=edits)


def test_cumulative_content_exactly_10mib_accepted():
    chunk = "x" * MAX_CONTENT_BYTES
    edits = [make_edit(identifier=f"notes/note-{i}", content=chunk) for i in range(10)]
    request = BulkEditRequest(edits=edits)
    assert len(request.edits) == 10
    assert MAX_TOTAL_CONTENT_BYTES == 10 * MAX_CONTENT_BYTES


# --- BULK-13 (schema part): expected_replacements >= 1 ---


@pytest.mark.parametrize("value", [0, -1])
def test_expected_replacements_below_one_rejected(value):
    with pytest.raises(ValidationError):
        BulkEditOperation(
            **make_edit(
                operation="find_replace",
                find_text="old",
                expected_replacements=value,
            )
        )


def test_expected_replacements_one_accepted():
    op = BulkEditOperation(
        **make_edit(operation="find_replace", find_text="old", expected_replacements=1)
    )
    assert op.expected_replacements == 1


# --- operation parameter requirements ---


@pytest.mark.parametrize("find_text", [None, "", "   "])
def test_find_replace_requires_find_text(find_text):
    with pytest.raises(ValidationError, match="find_text"):
        BulkEditOperation(**make_edit(operation="find_replace", find_text=find_text))


@pytest.mark.parametrize(
    "operation",
    ["replace_section", "insert_before_section", "insert_after_section"],
)
@pytest.mark.parametrize("section", [None, "", "   "])
def test_section_operations_require_section(operation, section):
    with pytest.raises(ValidationError, match="section"):
        BulkEditOperation(**make_edit(operation=operation, section=section))


def test_unsupported_operation_rejected():
    with pytest.raises(ValidationError):
        BulkEditOperation(**make_edit(operation="delete_everything"))


# --- response models ---


def test_item_result_and_response_models():
    item = BulkEditItemResult(identifier="notes/a", status="success", checksum="abc123")
    response = BulkEditResponse(
        total=1,
        succeeded=1,
        failed=0,
        skipped=0,
        validated=0,
        results=[item],
        vector_sync="disabled",
    )
    assert response.results[0].identifier == "notes/a"
    assert response.vector_sync == "disabled"


# --- I-1: path traversal helper ---


@pytest.mark.parametrize(
    "identifier",
    [
        "../outside-note",
        "../../etc/passwd",
        "notes/../../../etc/passwd",
        "/etc/passwd",
        "C:\\windows\\system32",
        "~/.ssh/id_rsa",
        "..%2F..%2Fetc%2Fpasswd",
        "%2e%2e/secret",
        "%252e%252e/secret",  # double-encoded
        "..\\windows-style",
    ],
)
def test_traversal_identifier_detected(identifier):
    assert is_path_traversal_identifier(identifier) is True


@pytest.mark.parametrize(
    "identifier",
    [
        "notes/test-note",
        "Plain Title",
        "deep/path/to/note",
        "hi-everyone..md",  # ".." substring inside a filename is legitimate
        "notes/test-note.md",
    ],
)
def test_legitimate_identifier_not_flagged(identifier):
    assert is_path_traversal_identifier(identifier) is False
