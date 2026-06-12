"""Detector for ```base``` fenced blocks in markdown content.

Adapted from dataview/detector.py (line-by-line scan), restricted to fenced
``base`` code blocks. Bases has no inline-query form. The regex deliberately
requires the fence info string to be exactly ``base`` so ``dataview``,
``dataviewjs``, ``python`` etc. are never matched.
"""

import re
from dataclasses import dataclass

from basic_memory.bases.schema import MAX_BLOCKS_PER_NOTE


@dataclass
class BasesBlock:
    """A detected ```base``` block.

    Attributes:
        body: the YAML body between the fences (fences excluded).
        start_line: 0-based line index of the opening fence.
        end_line: 0-based line index of the closing fence.
    """

    body: str
    start_line: int
    end_line: int

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"BasesBlock(lines={self.start_line}-{self.end_line})"


class BasesDetector:
    """Detects ```base``` blocks in markdown content."""

    # Exactly ```base (optionally trailing whitespace). Not ```dataview, etc.
    FENCE_START = re.compile(r"^```base\s*$")
    FENCE_END = re.compile(r"^```\s*$")

    @classmethod
    def detect_blocks(cls, content: str) -> list[BasesBlock]:
        """Detect all ```base``` blocks, capped at MAX_BLOCKS_PER_NOTE.

        Blocks beyond the cap are dropped at detection time (surnuméraires
        inertes). An unterminated opening fence is ignored (no crash).
        """
        blocks: list[BasesBlock] = []
        lines = content.split("\n")
        i = 0
        n = len(lines)

        while i < n:
            if cls.FENCE_START.match(lines[i]):
                start_line = i
                body_lines: list[str] = []
                j = i + 1
                closed = False
                while j < n:
                    if cls.FENCE_END.match(lines[j]):
                        blocks.append(
                            BasesBlock(
                                body="\n".join(body_lines),
                                start_line=start_line,
                                end_line=j,
                            )
                        )
                        closed = True
                        i = j
                        break
                    body_lines.append(lines[j])
                    j += 1
                if not closed:
                    # Unterminated fence: stop scanning, ignore the dangling block.
                    break
            i += 1

        return blocks[:MAX_BLOCKS_PER_NOTE]

    @classmethod
    def has_base_blocks(cls, content: str) -> bool:
        """Fast check whether any ```base``` block is present."""
        return bool(cls.detect_blocks(content))
