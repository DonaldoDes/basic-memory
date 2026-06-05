"""Schemas for the V2 bulk edit endpoint.

Implements the request/response contract of
POST /v2/projects/{project_id}/knowledge/entities/bulk-edit
(spec: specs/bulk-edit-notes/spec.md).

Security invariants enforced at the schema layer (global 422, no item processed):
- I-2: per-item content capped at 1 MiB
- I-3: cumulative content capped at 10 MiB
- I-5: 1..100 items per batch
- I-6: memory:// and project-qualified ("::") identifiers rejected — the batch
  is single-project, the project_id in the URL is authoritative. Plain
  multi-segment identifiers stay valid (they are regular deep permalinks);
  cross-project resolution is structurally impossible in the batch path because
  identifiers resolve only against the project-scoped entity repository.

I-1 (path traversal) is intentionally NOT a schema rule: a traversal identifier
fails its own item with error_code SECURITY without interrupting the batch.
The detection helper lives here so the router stays a thin orchestrator.
"""

from typing import Literal
from urllib.parse import unquote

from pydantic import BaseModel, Field, model_validator

from basic_memory.utils import valid_project_path_value

EditOperationType = Literal[
    "append",
    "prepend",
    "find_replace",
    "replace_section",
    "insert_before_section",
    "insert_after_section",
]

_SECTION_OPERATIONS = ("replace_section", "insert_before_section", "insert_after_section")

MAX_CONTENT_BYTES = 1024 * 1024  # 1 MiB per item (I-2)
MAX_TOTAL_CONTENT_BYTES = 10 * 1024 * 1024  # 10 MiB per batch (I-3)


def is_path_traversal_identifier(identifier: str) -> bool:
    """Return True when an identifier attempts to escape the project root (I-1).

    Checks the raw identifier plus up to two percent-decoding rounds (covers
    single- and double-encoded sequences), then applies the same project-path
    rules as the canonical ``valid_project_path_value`` (".." segments,
    absolute paths, drive letters, "~", control characters).

    Security-first tradeoff: an exotic note title containing "~" or a drive
    pattern is flagged too — callers can always use the permalink instead.
    """
    candidates = {identifier}
    decoded = identifier
    for _ in range(2):
        new_value = unquote(decoded)
        if new_value == decoded:
            break
        decoded = new_value
        candidates.add(decoded)

    return any(not valid_project_path_value(value.strip()) for value in candidates)


class BulkEditOperation(BaseModel):
    """One edit operation against one note of the project.

    Same operations as the single-note edit_note tool. Divergence (documented,
    spec § Schema): expected_replacements has a ge=1 bound — in a bulk of up to
    100 items, expected_replacements=0 has no useful semantics and would mask
    silent errors.
    """

    identifier: str = Field(min_length=1, description="Note title or permalink (project-local)")
    operation: EditOperationType
    content: str
    section: str | None = None
    find_text: str | None = None
    expected_replacements: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_operation_contract(self) -> "BulkEditOperation":
        stripped_identifier = self.identifier.strip()
        # I-6: explicit cross-project routes are rejected at the schema.
        if stripped_identifier.startswith("memory://"):
            raise ValueError(
                "memory:// identifiers are not allowed in bulk edits: "
                "the batch is single-project (the project_id in the URL is authoritative)"
            )
        if "::" in stripped_identifier:
            raise ValueError(
                "project-qualified identifiers ('project::note') are not allowed in "
                "bulk edits: the batch is single-project"
            )

        if self.operation == "find_replace":
            if not self.find_text or not self.find_text.strip():
                raise ValueError("find_text is required for find_replace operations")
        if self.operation in _SECTION_OPERATIONS:
            if not self.section or not self.section.strip():
                raise ValueError(f"section is required for {self.operation} operations")

        # I-2: per-item content size, measured in encoded bytes.
        if len(self.content.encode("utf-8")) > MAX_CONTENT_BYTES:
            raise ValueError("content exceeds the 1 MiB per-item limit")

        return self


class BulkEditRequest(BaseModel):
    """Batch of edit operations executed in request order (best-effort per note)."""

    edits: list[BulkEditOperation] = Field(min_length=1, max_length=100)  # I-5
    validate_first: bool = False
    stop_on_error: bool = False

    @model_validator(mode="after")
    def validate_total_content_size(self) -> "BulkEditRequest":
        # I-3: cumulative content size across the batch.
        total_bytes = sum(len(edit.content.encode("utf-8")) for edit in self.edits)
        if total_bytes > MAX_TOTAL_CONTENT_BYTES:
            raise ValueError("cumulative content exceeds the 10 MiB per-batch limit")
        return self


class BulkEditItemResult(BaseModel):
    """Outcome of one edit operation, in the same position as in the request."""

    identifier: str
    status: Literal["success", "failed", "skipped", "validated"]
    permalink: str | None = None
    file_path: str | None = None
    checksum: str | None = None
    error: str | None = None
    error_code: str | None = None


class BulkEditResponse(BaseModel):
    """Batch execution report (returned with HTTP 200 even if all items failed)."""

    total: int
    succeeded: int
    failed: int
    skipped: int
    validated: int
    results: list[BulkEditItemResult]
    vector_sync: Literal["scheduled", "disabled"]
