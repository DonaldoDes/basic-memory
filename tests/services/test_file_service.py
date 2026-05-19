"""Tests for file operations service."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from basic_memory import file_utils
from basic_memory.models.knowledge import Entity
from basic_memory.services.exceptions import BinaryFileError, FileOperationError
from basic_memory.services.file_service import FileService


@pytest.mark.asyncio
async def test_exists(tmp_path: Path, file_service: FileService):
    """Test file existence checking."""
    # Test path
    test_path = tmp_path / "test.md"

    # Should not exist initially
    assert not await file_service.exists(test_path)

    # Create file
    test_path.write_text("test content")
    assert await file_service.exists(test_path)

    # Delete file
    test_path.unlink()
    assert not await file_service.exists(test_path)


@pytest.mark.asyncio
async def test_exists_error_handling(tmp_path: Path, file_service: FileService, monkeypatch):
    """Test error handling in exists() method."""
    test_path = tmp_path / "test.md"

    def boom(*args, **kwargs):
        raise PermissionError("Access denied")

    monkeypatch.setattr(Path, "exists", boom)

    with pytest.raises(FileOperationError) as exc_info:
        await file_service.exists(test_path)

    assert "Failed to check file existence" in str(exc_info.value)


@pytest.mark.asyncio
async def test_write_read_file(tmp_path: Path, file_service: FileService):
    """Test basic write/read operations with checksums."""
    test_path = tmp_path / "test.md"
    test_content = "test content\nwith multiple lines"

    # Write file and get checksum
    checksum = await file_service.write_file(test_path, test_content)
    assert test_path.exists()

    # Read back and verify content/checksum
    content, read_checksum = await file_service.read_file(test_path)
    assert content == test_content
    assert read_checksum == checksum


@pytest.mark.asyncio
async def test_write_creates_directories(tmp_path: Path, file_service: FileService):
    """Test directory creation on write."""
    test_path = tmp_path / "subdir" / "nested" / "test.md"
    test_content = "test content"

    # Write should create directories
    await file_service.write_file(test_path, test_content)
    assert test_path.exists()
    assert test_path.parent.is_dir()


@pytest.mark.asyncio
async def test_write_atomic(tmp_path: Path, file_service: FileService, monkeypatch):
    """Test atomic write with no partial files."""
    test_path = tmp_path / "test.md"
    temp_path = test_path.with_suffix(".tmp")

    from basic_memory import file_utils

    async def fake_write_file_atomic(*args, **kwargs):
        raise Exception("Write failed")

    monkeypatch.setattr(file_utils, "write_file_atomic", fake_write_file_atomic)

    # Attempt write that will fail
    with pytest.raises(FileOperationError):
        await file_service.write_file(test_path, "test content")

    # No partial files should exist
    assert not test_path.exists()
    assert not temp_path.exists()


@pytest.mark.asyncio
async def test_delete_file(tmp_path: Path, file_service: FileService):
    """Test file deletion."""
    test_path = tmp_path / "test.md"
    test_content = "test content"

    # Create then delete
    await file_service.write_file(test_path, test_content)
    assert test_path.exists()

    await file_service.delete_file(test_path)
    assert not test_path.exists()

    # Delete non-existent file should not error
    await file_service.delete_file(test_path)


@pytest.mark.asyncio
async def test_checksum_consistency(tmp_path: Path, file_service: FileService):
    """Test checksum remains consistent."""
    test_path = tmp_path / "test.md"
    test_content = "test content\n" * 10

    # Get checksum from write
    checksum1 = await file_service.write_file(test_path, test_content)

    # Get checksum from read
    _, checksum2 = await file_service.read_file(test_path)

    # Write again and get new checksum
    checksum3 = await file_service.write_file(test_path, test_content)

    # All should match
    assert checksum1 == checksum2 == checksum3


@pytest.mark.asyncio
async def test_error_handling_missing_file(tmp_path: Path, file_service: FileService):
    """Test error handling for missing files."""
    test_path = tmp_path / "missing.md"

    with pytest.raises(FileOperationError):
        await file_service.read_file(test_path)


@pytest.mark.asyncio
async def test_error_handling_invalid_path(tmp_path: Path, file_service: FileService):
    """Test error handling for invalid paths."""
    # Try to write to a directory instead of file
    test_path = tmp_path / "test.md"
    test_path.mkdir()  # Create a directory instead of a file

    with pytest.raises(FileOperationError):
        await file_service.write_file(test_path, "test")


@pytest.mark.asyncio
async def test_write_unicode_content(tmp_path: Path, file_service: FileService):
    """Test handling of unicode content."""
    test_path = tmp_path / "test.md"
    test_content = """
    # Test Unicode
    - Emoji: 🚀 ⭐️ 🔥
    - Chinese: 你好世界
    - Arabic: مرحبا بالعالم
    - Russian: Привет, мир
    """

    # Write and read back
    await file_service.write_file(test_path, test_content)
    content, _ = await file_service.read_file(test_path)

    assert content == test_content


@pytest.mark.asyncio
async def test_update_frontmatter_checksum_matches_windows_crlf_persisted_bytes(
    tmp_path: Path, file_service: FileService, monkeypatch
):
    """Windows-style CRLF writes should hash the stored file, not the pre-write string."""
    test_path = tmp_path / "note.md"
    test_path.write_text("# Note\nBody\n", encoding="utf-8")

    async def fake_write_file_atomic(path: Path, content: str) -> None:
        # Trigger: simulate Windows text-mode persistence, where logical LF strings
        #          land on disk as CRLF bytes.
        # Why: the regression happened when the stored bytes diverged from the LF string
        #      used to build the checksum.
        # Outcome: this test proves FileService returns the checksum for the stored file.
        persisted = content.replace("\n", "\r\n").encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(persisted)

    monkeypatch.setattr(file_utils, "write_file_atomic", fake_write_file_atomic)

    result = await file_service.update_frontmatter_with_result(
        test_path,
        {"title": "Note", "type": "note"},
    )

    assert result.checksum == await file_service.compute_checksum(test_path)


@pytest.mark.asyncio
async def test_read_file_content(tmp_path: Path, file_service: FileService):
    """Test read_file_content returns just the content without checksum."""
    test_path = tmp_path / "test.md"
    test_content = "test content\nwith multiple lines"

    # Write file
    await file_service.write_file(test_path, test_content)

    # Read content only
    content = await file_service.read_file_content(test_path)
    assert content == test_content


@pytest.mark.asyncio
async def test_read_file_content_missing_file(tmp_path: Path, file_service: FileService):
    """Test read_file_content raises error for missing files."""
    test_path = tmp_path / "missing.md"

    # FileNotFoundError is preserved so callers can treat missing files specially (e.g. sync).
    with pytest.raises(FileNotFoundError):
        await file_service.read_file_content(test_path)


@pytest.mark.asyncio
async def test_read_file_content_raises_file_operation_error_for_directory(
    tmp_path: Path, file_service: FileService
):
    """read_file_content should wrap non-FileNotFound errors in FileOperationError."""
    # Use a .md "file path" that is actually a directory to exercise the
    # IsADirectoryError branch (a non-.md path now triggers BinaryFileError
    # before reaching the open() call).
    dir_path = tmp_path / "not-a-file.md"
    dir_path.mkdir()

    with pytest.raises(FileOperationError) as exc_info:
        await file_service.read_file_content(dir_path)

    assert "Failed to read file" in str(exc_info.value)


@pytest.mark.asyncio
async def test_read_file_bytes(tmp_path: Path, file_service: FileService):
    """Test read_file_bytes for binary file reading."""
    test_path = tmp_path / "test.bin"
    # Create binary content with non-UTF8 bytes
    binary_content = b"\x00\x01\x02\x03\xff\xfe\xfd"

    # Write binary file directly
    test_path.write_bytes(binary_content)

    # Read back using read_file_bytes
    content = await file_service.read_file_bytes(test_path)
    assert content == binary_content


@pytest.mark.asyncio
async def test_read_file_bytes_image(tmp_path: Path, file_service: FileService):
    """Test read_file_bytes with image-like binary content."""
    test_path = tmp_path / "test.png"
    # PNG header signature
    png_header = b"\x89PNG\r\n\x1a\n"
    fake_image_content = png_header + b"\x00" * 100

    test_path.write_bytes(fake_image_content)

    content = await file_service.read_file_bytes(test_path)
    assert content == fake_image_content
    assert content.startswith(png_header)


@pytest.mark.asyncio
async def test_read_file_bytes_missing_file(tmp_path: Path, file_service: FileService):
    """Test read_file_bytes raises error for missing files."""
    test_path = tmp_path / "missing.bin"

    with pytest.raises(FileOperationError):
        await file_service.read_file_bytes(test_path)


@pytest.mark.asyncio
async def test_read_file_bytes_text_file(tmp_path: Path, file_service: FileService):
    """Test read_file_bytes can read text files as bytes."""
    test_path = tmp_path / "test.txt"
    text_content = "Hello, World!"

    test_path.write_text(text_content)

    content = await file_service.read_file_bytes(test_path)
    assert content == text_content.encode("utf-8")


# -----------------------------------------------------------------------------
# Binary file handling (regression: UnicodeDecodeError on PDF/image entities)
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_entity_content_skips_binary_file(
    tmp_path: Path, file_service: FileService, test_project
):
    """read_entity_content must NOT crash on a binary entity (type='file').

    Regression: previously decoded PDF/JPEG bytes as UTF-8 and raised
    UnicodeDecodeError. Now returns empty string for non-markdown entities.
    """
    pdf_path = file_service.base_path / "fixture.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    # Real PDF header followed by non-UTF8 bytes (mimics actual PDFs)
    pdf_path.write_bytes(b"%PDF-1.4\n%\xd3\xeb\xe9\xe1\nbinary content\xff\xfe\xfd")

    entity = Entity(
        title="fixture.pdf",
        entity_type="file",
        content_type="application/pdf",
        file_path="fixture.pdf",
        project_id=test_project.id,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    content = await file_service.read_entity_content(entity)

    assert content == ""


@pytest.mark.asyncio
async def test_read_entity_content_markdown_still_works(
    tmp_path: Path, file_service: FileService, test_project
):
    """read_entity_content must still return content for markdown entities."""
    md_path = file_service.base_path / "note.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("# Title\n\nbody text\n", encoding="utf-8")

    entity = Entity(
        title="note",
        entity_type="note",
        content_type="text/markdown",
        file_path="note.md",
        project_id=test_project.id,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    content = await file_service.read_entity_content(entity)

    assert "body text" in content


@pytest.mark.asyncio
async def test_read_file_content_raises_binary_file_error(
    tmp_path: Path, file_service: FileService
):
    """read_file_content must raise BinaryFileError for non-markdown paths.

    This forces callers (entity_service, knowledge_router dataview, sync) to
    explicitly handle binary files instead of crashing on UTF-8 decode.
    """
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%\xd3\xeb\xe9\xe1\n")

    with pytest.raises(BinaryFileError) as exc_info:
        await file_service.read_file_content(pdf_path)

    assert "doc.pdf" in str(exc_info.value)


@pytest.mark.asyncio
async def test_read_file_raises_binary_file_error(tmp_path: Path, file_service: FileService):
    """read_file must raise BinaryFileError for non-markdown paths."""
    img_path = tmp_path / "image.jpeg"
    img_path.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00")

    with pytest.raises(BinaryFileError) as exc_info:
        await file_service.read_file(img_path)

    assert "image.jpeg" in str(exc_info.value)


@pytest.mark.asyncio
async def test_read_file_content_markdown_unchanged(
    tmp_path: Path, file_service: FileService
):
    """read_file_content on markdown files keeps its existing behaviour."""
    md_path = tmp_path / "ok.md"
    md_path.write_text("hello", encoding="utf-8")

    content = await file_service.read_file_content(md_path)
    assert content == "hello"


# -----------------------------------------------------------------------------
# Path traversal last-mile defense (BUG-004, Option C) — [ADVERSARIAL]
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_file_raises_on_relative_path_escape(
    tmp_path: Path, file_service: FileService
):
    """[ADVERSARIAL] BUG-004 Option C: a relative path containing `..`
    segments that escapes base_path on resolve must be rejected even if it
    bypassed earlier validation layers. Last-mile defense-in-depth.

    Scenario: a hypothetical earlier-layer bug lets `"../escape.md"` through
    to write_file. base_path is tmp_path, so resolved path is tmp_path's
    parent — outside the project root.
    """
    escape_path = "../escape.md"
    with pytest.raises(FileOperationError) as exc_info:
        await file_service.write_file(escape_path, "pwned")
    # Must mention "traversal" or "escape" — not a generic "failed to write"
    msg = str(exc_info.value).lower()
    assert "traversal" in msg or "escape" in msg or "outside" in msg
    # And the file must not exist anywhere
    assert not (tmp_path.parent / "escape.md").exists()


@pytest.mark.asyncio
async def test_write_file_raises_on_absolute_path_outside_base(
    tmp_path: Path, file_service: FileService
):
    """[ADVERSARIAL] BUG-004 Option C: an absolute path pointing outside
    base_path must be rejected. This catches attacks where the path_obj is
    already absolute (e.g. `/etc/evil.md` injected somewhere upstream).
    """
    # Use a path that is absolute and clearly outside base_path
    outside = tmp_path.parent / "outside_evil.md"
    with pytest.raises(FileOperationError) as exc_info:
        await file_service.write_file(outside, "pwned")
    msg = str(exc_info.value).lower()
    assert "traversal" in msg or "escape" in msg or "outside" in msg
    assert not outside.exists()


@pytest.mark.asyncio
async def test_write_file_raises_on_nested_traversal_to_escape(
    tmp_path: Path, file_service: FileService
):
    """[ADVERSARIAL] BUG-004 Option C: a path with nested `..` segments
    that resolves outside base_path must be rejected. Example:
    `subdir/../../escape.md` — looks innocent at first segment but the
    resolved form is `tmp_path.parent / escape.md`.
    """
    nested = "subdir/../../escape.md"
    with pytest.raises(FileOperationError) as exc_info:
        await file_service.write_file(nested, "pwned")
    msg = str(exc_info.value).lower()
    assert "traversal" in msg or "escape" in msg or "outside" in msg
    assert not (tmp_path.parent / "escape.md").exists()


@pytest.mark.asyncio
async def test_write_file_accepts_legitimate_relative_path(
    tmp_path: Path, file_service: FileService
):
    """[ADVERSARIAL] BUG-004 Option C negative case: a legitimate relative
    path inside base_path must continue to work — the escape check must not
    over-reject valid writes.
    """
    legit = "subdir/note.md"
    checksum = await file_service.write_file(legit, "hello")
    assert checksum is not None
    assert (tmp_path / "subdir" / "note.md").exists()
