"""Utilities for file operations."""

import asyncio
import hashlib
import shlex
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any, Dict, Optional, Union

import aiofiles
import yaml
import frontmatter
from loguru import logger

from basic_memory.utils import FilePath

if TYPE_CHECKING:  # pragma: no cover
    from basic_memory.config import BasicMemoryConfig


@dataclass
class FileMetadata:
    """File metadata for cloud-compatible file operations.

    This dataclass provides a cloud-agnostic way to represent file metadata,
    enabling S3FileService to return metadata from head_object responses
    instead of mock stat_result with zeros.
    """

    size: int
    created_at: datetime
    modified_at: datetime


class FileError(Exception):
    """Base exception for file operations."""

    pass


class FileWriteError(FileError):
    """Raised when file operations fail."""

    pass


class ParseError(FileError):
    """Raised when parsing file content fails."""

    pass


async def compute_checksum(content: Union[str, bytes]) -> str:
    """
    Compute SHA-256 checksum of content.

    Args:
        content: Content to hash (either text string or bytes)

    Returns:
        SHA-256 hex digest

    Raises:
        FileError: If checksum computation fails
    """
    try:
        if isinstance(content, str):
            content = content.encode()
        return hashlib.sha256(content).hexdigest()
    except Exception as e:  # pragma: no cover
        logger.error(f"Failed to compute checksum: {e}")
        raise FileError(f"Failed to compute checksum: {e}")


# UTF-8 BOM character that can appear at the start of files
UTF8_BOM = "\ufeff"


def strip_bom(content: str) -> str:
    """Strip UTF-8 BOM from the start of content if present.

    BOM (Byte Order Mark) characters can be present in files created on Windows
    or copied from certain sources. They should be stripped before processing
    frontmatter. See issue #452.

    Args:
        content: Content that may start with BOM

    Returns:
        Content with BOM removed if present
    """
    if content and content.startswith(UTF8_BOM):
        return content[1:]
    return content


async def write_file_atomic(path: FilePath, content: str) -> None:
    """
    Write file with atomic operation using temporary file.

    Uses aiofiles for true async I/O (non-blocking).

    Args:
        path: Target file path (Path or string)
        content: Content to write

    Raises:
        FileWriteError: If write operation fails
    """
    # Convert string to Path if needed
    path_obj = Path(path) if isinstance(path, str) else path
    temp_path = path_obj.with_suffix(".tmp")

    try:
        # Trigger: callers hand us normalized Python text, but the final bytes are allowed
        #          to use the host platform's native newline convention during the write.
        # Why: preserving CRLF on Windows keeps local files aligned with editors like
        #      Obsidian, while FileService now hashes the persisted file bytes instead of
        #      the pre-write string.
        # Outcome: this async write stays editor-friendly across platforms without
        #          reintroducing checksum drift in sync or move detection.
        async with aiofiles.open(temp_path, mode="w", encoding="utf-8") as f:
            await f.write(content)

        # Atomic rename (this is fast, doesn't need async)
        temp_path.replace(path_obj)
        logger.debug("Wrote file atomically", path=str(path_obj), content_length=len(content))
    except Exception as e:  # pragma: no cover
        temp_path.unlink(missing_ok=True)
        logger.error("Failed to write file", path=str(path_obj), error=str(e))
        raise FileWriteError(f"Failed to write file {path}: {e}")


async def format_markdown_builtin(path: Path) -> Optional[str]:
    """
    Format a markdown file using the built-in mdformat formatter.

    Uses mdformat with GFM (GitHub Flavored Markdown) support for consistent
    formatting without requiring Node.js or external tools.

    Args:
        path: Path to the markdown file to format

    Returns:
        Formatted content if successful, None if formatting failed.
    """
    try:
        import mdformat
    except ImportError:  # pragma: no cover
        logger.warning(
            "mdformat not installed, skipping built-in formatting",
            path=str(path),
        )
        return None

    try:
        # Read original content
        async with aiofiles.open(path, mode="r", encoding="utf-8") as f:
            content = await f.read()

        # Format using mdformat with GFM and frontmatter extensions
        # mdformat is synchronous, so we run it in a thread executor
        loop = asyncio.get_event_loop()
        formatted_content = await loop.run_in_executor(
            None,
            lambda: mdformat.text(
                content,
                extensions={"gfm", "frontmatter"},  # GFM + YAML frontmatter support
                options={"wrap": "no"},  # Don't wrap lines
            ),
        )

        # Only write if content changed
        if formatted_content != content:
            # Trigger: mdformat may rewrite markdown content, then the host platform
            #          decides the newline bytes for the follow-up async text write.
            # Why: we want formatter output to preserve native newlines instead of
            #      forcing LF, and the authoritative checksum comes from rereading the
            #      stored file bytes later in FileService.
            # Outcome: formatting remains compatible with local editors on Windows while
            #          checksum-based sync logic stays anchored to on-disk bytes.
            async with aiofiles.open(path, mode="w", encoding="utf-8") as f:
                await f.write(formatted_content)

        logger.debug(
            "Formatted file with mdformat",
            path=str(path),
            changed=formatted_content != content,
        )
        return formatted_content

    except Exception as e:  # pragma: no cover
        logger.warning(
            "mdformat formatting failed",
            path=str(path),
            error=str(e),
        )
        return None


async def format_file(
    path: Path,
    config: "BasicMemoryConfig",
    is_markdown: bool = False,
) -> Optional[str]:
    """
    Format a file using configured formatter.

    By default, uses the built-in mdformat formatter for markdown files (pure Python,
    no Node.js required). External formatters like Prettier can be configured via
    formatter_command or per-extension formatters.

    Args:
        path: File to format
        config: Configuration with formatter settings
        is_markdown: Whether this is a markdown file (caller should use FileService.is_markdown)

    Returns:
        Formatted content if successful, None if formatting was skipped or failed.
        Failures are logged as warnings but don't raise exceptions.
    """
    if not config.format_on_save:
        return None

    extension = path.suffix.lstrip(".")
    formatter = config.formatters.get(extension) or config.formatter_command

    # Use built-in mdformat for markdown files when no external formatter configured
    if not formatter:
        if is_markdown:
            return await format_markdown_builtin(path)
        else:
            logger.debug("No formatter configured for extension", extension=extension)
            return None

    # Use external formatter
    # Replace {file} placeholder with the actual path
    cmd = formatter.replace("{file}", str(path))

    try:
        # Parse command into args list for safer execution (no shell=True)
        args = shlex.split(cmd)

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=config.formatter_timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning(
                "Formatter timed out",
                path=str(path),
                timeout=config.formatter_timeout,
            )
            return None

        if proc.returncode != 0:
            logger.warning(
                "Formatter exited with non-zero status",
                path=str(path),
                returncode=proc.returncode,
                stderr=stderr.decode("utf-8", errors="replace") if stderr else "",
            )
            # Still try to read the file - formatter may have partially worked
            # or the file may be unchanged

        # Read formatted content
        async with aiofiles.open(path, mode="r", encoding="utf-8") as f:
            formatted_content = await f.read()

        logger.debug(
            "Formatted file successfully",
            path=str(path),
            formatter=args[0] if args else formatter,
        )
        return formatted_content

    except FileNotFoundError:
        # Formatter executable not found
        logger.warning(
            "Formatter executable not found",
            command=cmd.split()[0] if cmd else "",
            path=str(path),
        )
        return None
    except Exception as e:  # pragma: no cover
        logger.warning(
            "Formatter failed",
            path=str(path),
            error=str(e),
        )
        return None


# A frontmatter fence is a line containing exactly `---`, optionally followed by
# trailing horizontal whitespace. Anchoring to a full line (rather than a bare
# substring/`startswith`) prevents single-line content like
# `---\nstatus: active\n---\nBody` — where `\n` is a literal backslash-n, not a
# newline — from being misread as frontmatter. See issue #972.
_FENCE_RE = re.compile(r"^---[ \t]*$")


def _split_frontmatter(content: str) -> Optional[tuple[str, str]]:
    """Split content into (yaml_block, body) when it opens with a line-anchored fence.

    The opening fence must be the very first line and the closing fence must be a
    later line, each matching exactly `---` (with optional trailing whitespace).

    Returns:
        A `(yaml_block, body)` tuple when a complete fenced block is present, or
        ``None`` when the content does not open with a frontmatter fence.

    Raises:
        ParseError: If the content opens with a fence but has no closing fence.
    """
    lines = content.splitlines(keepends=True)

    # Skip leading blank lines: a document may begin with whitespace before the
    # opening fence (e.g. a heredoc/dedented string starting with a newline). This
    # does NOT relax line-anchoring — the opening fence must still be the first
    # non-blank line, all on its own, so a single-line `---\\nstatus...` (literal
    # backslash-n) is still rejected. See issue #972.
    start = 0
    while start < len(lines) and lines[start].strip() == "":
        start += 1

    if start >= len(lines) or not _FENCE_RE.match(lines[start].rstrip("\r\n")):
        return None

    # Find the closing fence on its own line somewhere after the opening fence.
    for index in range(start + 1, len(lines)):
        if _FENCE_RE.match(lines[index].rstrip("\r\n")):
            yaml_block = "".join(lines[start + 1 : index])
            body = "".join(lines[index + 1 :])
            return yaml_block, body

    raise ParseError("Invalid frontmatter format")


def has_frontmatter(content: str) -> bool:
    """
    Check if content contains valid YAML frontmatter.

    Frontmatter requires `---` fences on their own lines; an inline `---` (such as
    a single-line string that merely starts with the characters `---`) is not
    frontmatter.

    Args:
        content: Content to check

    Returns:
        True if content has line-anchored frontmatter fences, False otherwise
    """
    if not content:
        return False

    # Strip BOM before checking for frontmatter markers
    content = strip_bom(content)
    try:
        return _split_frontmatter(content) is not None
    except ParseError:
        # An opening fence with no closing fence is not usable frontmatter.
        return False


#: Matches a YAML mapping line whose value is a *bare* inline wikilink, e.g.::
#:
#:     part_of: [[projects/Mon Projet|Mon Projet]]
#:       primary: [[X|X]]
#:
#: Capture groups: (1) the ``key:`` prefix incl. indentation and trailing space,
#: (2) the unquoted ``[[...]]`` wikilink value (no surrounding quotes).
#:
#: BUG-010: ``yaml.safe_load`` parses an unquoted ``[[path|alias]]`` value as a YAML
#: flow list-of-list (``[['path|alias']]``), which is then re-emitted in block style
#: ``- - path|alias`` on the next write — silently corrupting the relation. We quote
#: such values as scalar strings *before* parsing so YAML keeps them as plain strings.
_BARE_INLINE_WIKILINK_LINE = re.compile(
    r"^(?P<prefix>\s*[^\s:#][^:]*:\s+)(?P<value>\[\[.*\]\])\s*$"
)


#: Matches a YAML block-sequence item whose value is a *bare* inline wikilink, e.g.::
#:
#:     refs:
#:     - [[x|y]]
#:       - [[projects/X|X]]
#:
#: Capture groups: (1) the ``- `` item prefix incl. indentation and trailing space,
#: (2) the unquoted ``[[...]]`` wikilink value (no surrounding quotes).
#:
#: BUG-011 (résidu 1): an unquoted block-sequence item ``- [[path|alias]]`` is parsed
#: by ``yaml.safe_load`` as a nested flow list (``[['path|alias']]``) and re-emitted as
#: ``- - - path|alias`` — the same silent corruption class as the BUG-010 inline case,
#: on list-item lines. We quote such items as scalar strings *before* parsing.
_BARE_BLOCK_SEQ_WIKILINK_LINE = re.compile(r"^(?P<prefix>\s*-\s+)(?P<value>\[\[.*\]\])\s*$")


def quote_inline_wikilinks(yaml_text: str) -> str:
    """Single-quote bare inline ``[[wikilink]]`` frontmatter values before YAML parse.

    Only lines of the form ``key: [[...]]`` (a single wikilink as the *entire* value,
    not already quoted) are rewritten. This is the BUG-010 fix.

    Deliberately NOT touched (non-regression):
        * Genuine flow lists: ``tags: [a, b]`` (value does not start with ``[[``).
        * Genuine nested lists: ``m: [[a, b], [c, d]]`` (value has a top-level comma
          after the inner list, so it is not a single ``[[...]]`` token).
        * Already-quoted values: ``'[[...]]'`` / ``"[[...]]"`` (value does not begin
          with ``[[`` once the quote is consumed by the regex anchor).
        * Prose containing wikilinks (already a scalar string for YAML).

    Also rewritten (BUG-011, résidu 1): bare block-sequence items of the form
    ``- [[...]]`` (a single wikilink as the *entire* item), which YAML would otherwise
    parse as a nested flow list and re-emit as ``- - - x|y``. Genuine list items
    (``- foo``, ``- [a, b]``, ``- [[a, b], [c, d]]``) and already-quoted items
    (``- '[[x]]'``) are deliberately left untouched.

    Args:
        yaml_text: The raw YAML frontmatter block (between the ``---`` fences).

    Returns:
        The YAML text with bare inline wikilink values single-quoted.
    """
    # keepends=True preserves each line's terminator (and any trailing newline of the
    # block), so the reconstructed YAML is byte-identical except for the quoted values.
    out_lines: list[str] = []
    for raw_line in yaml_text.splitlines(keepends=True):
        # Separate the line body from its terminator so the regex anchors ($) match.
        stripped = raw_line.rstrip("\r\n")
        terminator = raw_line[len(stripped) :]
        # Try the inline mapping form (``key: [[..]]``) first, then the block-sequence
        # item form (``- [[..]]``). Both reuse the same single-wikilink discrimination.
        match = _BARE_INLINE_WIKILINK_LINE.match(stripped) or _BARE_BLOCK_SEQ_WIKILINK_LINE.match(
            stripped
        )
        if match and _is_single_inline_wikilink(match.group("value")):
            # Escape single quotes per YAML single-quoted scalar rules ('' = ').
            escaped = match.group("value").replace("'", "''")
            out_lines.append(f"{match.group('prefix')}'{escaped}'{terminator}")
        else:
            out_lines.append(raw_line)
    return "".join(out_lines)


def _is_single_inline_wikilink(value: str) -> bool:
    """Return True if ``value`` is exactly one ``[[...]]`` token (not a multi-elem list).

    ``[[a|b]]``           -> True  (single wikilink)
    ``[[a, b], [c, d]]``  -> False (genuine nested flow list)
    ``[[a], [b]]``        -> False (two flow-list elements)
    """
    if not (value.startswith("[[") and value.endswith("]]")):
        return False
    # Strip the outer flow-list brackets and scan the inner content for a top-level
    # comma that would indicate multiple elements (a real list, not a wikilink).
    inner = value[1:-1]  # drop one layer: "[a|b]" for a wikilink, "[a, b], [c, d]" for a list
    depth = 0
    for ch in inner:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth < 0:
                # Surplus ``]`` (unbalanced closing bracket): not a well-formed single
                # wikilink. Defensive guard (BUG-011 résidu 2) so the helper is safe to
                # reuse without the upstream startswith/endswith gates.
                return False
        elif ch == "," and depth == 0:
            # A comma outside any inner bracket means multiple flow-list elements.
            return False
    return True


def quote_frontmatter_inline_wikilinks(content: str) -> str:
    """Apply :func:`quote_inline_wikilinks` to the leading ``---`` frontmatter block only.

    The markdown body is left byte-identical. If ``content`` has no frontmatter, it is
    returned unchanged. Used by the file-read parse path (untrusted vault input) so the
    BUG-010 fix also protects manually-edited / Obsidian-synced notes, not just
    ``write_note`` content.

    Args:
        content: Full markdown content (frontmatter + body), BOM already stripped.

    Returns:
        Content with bare inline wikilinks in the frontmatter block single-quoted.
    """
    if not content.startswith("---"):
        return content

    # Split into [pre-empty, frontmatter, body]; pre-empty is "" since content starts "---".
    parts = content.split("---", 2)
    if len(parts) < 3:
        return content

    fixed_block = quote_inline_wikilinks(parts[1])
    return f"---{fixed_block}---{parts[2]}"


def parse_frontmatter(content: str) -> Dict[str, Any]:
    """
    Parse YAML frontmatter from content.

    Args:
        content: Content with YAML frontmatter

    Returns:
        Dictionary of frontmatter values

    Raises:
        ParseError: If frontmatter is invalid or parsing fails
    """
    try:
        # Strip BOM before parsing frontmatter
        content = strip_bom(content)
        split = _split_frontmatter(content)
        if split is None:
            raise ParseError("Content has no frontmatter")
        yaml_block, _ = split

        # Parse YAML
        try:
            # BUG-010: quote bare inline wikilinks so YAML keeps them as scalar strings
            # instead of parsing "[[x|y]]" as a flow list-of-list.
            frontmatter = yaml.safe_load(quote_inline_wikilinks(yaml_block))
            # Handle empty frontmatter (None from yaml.safe_load)
            if frontmatter is None:
                return {}
            if not isinstance(frontmatter, dict):
                raise ParseError("Frontmatter must be a YAML dictionary")
            return frontmatter

        except yaml.YAMLError as e:
            raise ParseError(f"Invalid YAML in frontmatter: {e}")

    except Exception as e:  # pragma: no cover
        if not isinstance(e, ParseError):
            logger.error(f"Failed to parse frontmatter: {e}")
            raise ParseError(f"Failed to parse frontmatter: {e}")
        raise


def remove_frontmatter(content: str) -> str:
    """
    Remove YAML frontmatter from content.

    Args:
        content: Content with frontmatter

    Returns:
        Content with frontmatter removed, or original content if no frontmatter

    Raises:
        ParseError: If content starts with frontmatter marker but is malformed
    """
    # Strip BOM before processing
    content = strip_bom(content)

    split = _split_frontmatter(content)
    # Trigger: content does not open with a line-anchored fence
    # Why: inline `---` is ordinary content, not frontmatter (issue #972)
    # Outcome: return the content untouched (stripped to preserve prior behavior)
    if split is None:
        return content.strip()

    _, body = split
    return body.strip()


def dump_frontmatter(post: frontmatter.Post) -> str:
    """
    Serialize frontmatter.Post to markdown with Obsidian-compatible YAML format.

    This function ensures that:
    1. Tags are formatted as YAML lists instead of JSON arrays
    2. String values are properly quoted to handle special characters (colons, etc.)

    Good (Obsidian compatible):
    ---
    title: "L2 Governance Core (Split: Core)"
    tags:
    - system
    - overview
    - reference
    ---

    Bad (causes parsing errors):
    ---
    title: L2 Governance Core (Split: Core)  # Unquoted colon breaks YAML
    tags: ["system", "overview", "reference"]
    ---

    Args:
        post: frontmatter.Post object to serialize

    Returns:
        String containing markdown with properly formatted YAML frontmatter
    """
    if not post.metadata:
        # No frontmatter, just return content
        return post.content

    # Serialize YAML with block style for lists
    # SafeDumper automatically quotes values with special characters (colons, etc.)
    yaml_str = yaml.dump(
        post.metadata,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        Dumper=yaml.SafeDumper,
    )

    # Construct the final markdown with frontmatter
    if post.content:
        return f"---\n{yaml_str}---\n\n{post.content}"
    else:
        return f"---\n{yaml_str}---\n"


def sanitize_for_filename(text: str, replacement: str = "-") -> str:
    """
    Sanitize string to be safe for use as a note title
    Replaces path separators and other problematic characters
    with hyphens.
    """
    # replace both POSIX and Windows path separators
    text = re.sub(r"[/\\]", replacement, text)

    # replace some other problematic chars
    text = re.sub(r'[<>:"|?*]', replacement, text)

    # compress multiple, repeated replacements
    text = re.sub(f"{re.escape(replacement)}+", replacement, text)

    # Strip trailing periods — they cause "hi-everyone..md" double-dot filenames
    # when ".md" is appended, which triggers path traversal false positives.
    # Trailing periods are also invalid on Windows filesystems.
    text = text.strip(".")

    return text.strip(replacement)


#: Pre-filter normalisation table for ``sanitize_for_directory``.
#:
#: BUG-001: silently stripping non-whitelist characters produced double-spaces
#: when the offending character was surrounded by spaces (e.g. ``" — "`` or
#: ``" & "``), creating a filesystem folder distinct from the one the caller
#: intended. We instead normalise common typographic substitutes BEFORE the
#: whitelist filter, so that ``"Wealth & Finance"`` becomes a single, valid
#: ASCII path.
#:
#: Design choice for ``&`` → ``" and "`` (verbose, not ``-``):
#:   1. Preserves human readability of folder names.
#:   2. Matches the BUG-001 spec "Comportement attendu" example.
#:   3. Folder paths support spaces, so verbosity is harmless.
#:
#: Any character left after normalisation that is not in the whitelist will
#: raise ``ValueError`` (see below) rather than be silently dropped.
_DIRECTORY_NORMALIZATION_MAP = {
    "—": "-",  # em-dash —  → ASCII hyphen
    "–": "-",  # en-dash –  → ASCII hyphen
    "&": " and ",  # ampersand  → " and "
}


def sanitize_for_directory(directory: str) -> str:
    """
    Sanitize a directory path to be safe for use in file system paths.

    Behaviour:
        * Trims leading/trailing whitespace and an optional leading ``./``.
        * Strips filesystem-reserved characters ``<>:"|?*`` silently — these
          are never valid in a folder name on Windows and are aligned with
          the behaviour of ``sanitize_for_filename`` (legacy contract,
          preserved for backward compatibility).
        * Normalises typographic substitutes BEFORE the whitelist check, to
          avoid the double-space corruption documented in BUG-001:

            - em-dash ``—`` (U+2014) → ``-``
            - en-dash ``–`` (U+2013) → ``-``
            - ampersand ``&``        → ``" and "`` (then space-compressed)

        * Accepts: alphanumerics (incl. accented letters via ``str.isalnum()``),
          ``.``, space, ``-``, ``_``, ``\\``, ``/``.
        * Rejects: any other character — raises ``ValueError`` with the
          offending character, its Unicode codepoint and its position. This
          is a deliberate fork choice (Option B) over the previous silent
          strip, which produced filesystem folders distinct from the intended
          path and broke Obsidian wikilinks.
        * Rejects path traversal segments ``..`` AFTER the silent strip
          (BUG-004): inputs like ``"<>../etc"`` would otherwise pass
          ``validate_project_path`` (the segment ``<>..`` is not literally
          ``..``) but the strip turns them into a real ``..`` segment,
          producing end-to-end path traversal. We therefore re-check
          segments here, post-strip, and raise ``ValueError`` mentioning
          "traversal" if any equals ``..``. Note that a segment merely
          *containing* ``..`` (e.g. ``"foo..bar"``) is NOT a traversal and
          remains legitimate.
        * Compresses consecutive spaces and path separators, then trims
          leading/trailing slashes.

    Raises:
        ValueError: When ``directory`` contains a character outside the
            whitelist that cannot be normalised (e.g. emoji, control chars,
            non-Latin scripts not covered by ``isalnum``-friendly codepoints),
            OR when any path segment equals ``..`` after the silent strip of
            filesystem-reserved characters (path traversal — BUG-004).
    """
    if not directory:
        return ""

    sanitized = directory.strip()

    if sanitized.startswith("./"):
        sanitized = sanitized[2:]

    # Strip filesystem-reserved chars silently (Windows-invalid in any path
    # segment). This preserves the legacy contract that callers can pass
    # ``"my<>dir"`` and get back ``"mydir"`` without raising.
    sanitized = re.sub(r'[<>:"|?*]', "", sanitized)

    # BUG-004: block path traversal segments produced by the silent strip.
    # Must run AFTER the strip (otherwise inputs like "<>../etc" slip through
    # since "<>.." != "..") and BEFORE the trailing path-separator
    # compression (which could merge segments and mask the issue). We split
    # on both POSIX and Windows separators so attackers cannot bypass via
    # backslashes (e.g. "<>..\\etc").
    for seg in re.split(r"[\\/]+", sanitized):
        if seg == "..":
            raise ValueError(
                f"Path traversal segment '..' detected in folder {directory!r} "
                f"after sanitization — refusing to construct escape-capable path"
            )

    # Pre-filter normalisation: convert known typographic substitutes BEFORE
    # the whitelist check so they survive sanitisation.
    for src, dst in _DIRECTORY_NORMALIZATION_MAP.items():
        sanitized = sanitized.replace(src, dst)

    # Whitelist check: every remaining character must be alphanumeric (which
    # accepts accents) or one of the allowed punctuation/separator chars.
    # Any other character is a hard error — we no longer strip silently.
    allowed_punct = {".", " ", "-", "_", "\\", "/"}
    for i, c in enumerate(sanitized):
        if not (c.isalnum() or c in allowed_punct):
            raise ValueError(
                f"Invalid character {c!r} (U+{ord(c):04X}) at position {i} in folder {directory!r}"
            )

    # compress multiple, repeated spaces (e.g. " & " → "  and  " → " and ")
    sanitized = re.sub(r" +", " ", sanitized)

    sanitized = sanitized.rstrip()

    # compress multiple, repeated instances of path separators
    sanitized = re.sub(r"[\\/]+", "/", sanitized)

    # trim any leading/trailing path separators
    sanitized = sanitized.strip("\\/")

    return sanitized
