"""US-002 — Detection of ```base``` fenced blocks (BASES-01)."""

from basic_memory.bases.detector import BasesDetector, BasesBlock


class TestBasesDetector:
    def test_detects_single_base_block(self):
        content = """# Note

```base
views:
  - type: table
```

More text.
"""
        blocks = BasesDetector.detect_blocks(content)
        assert len(blocks) == 1
        assert isinstance(blocks[0], BasesBlock)
        assert "views:" in blocks[0].body
        assert blocks[0].start_line >= 0
        assert blocks[0].end_line > blocks[0].start_line

    def test_does_not_confuse_dataview_block(self):
        content = """```dataview
LIST FROM "projects"
```
"""
        assert BasesDetector.detect_blocks(content) == []

    def test_does_not_confuse_dataviewjs_block(self):
        content = """```dataviewjs
dv.list([])
```
"""
        assert BasesDetector.detect_blocks(content) == []

    def test_does_not_confuse_plain_code_block(self):
        content = """```python
print("base")
```
"""
        assert BasesDetector.detect_blocks(content) == []

    def test_detects_multiple_base_blocks(self):
        content = """```base
views:
  - type: table
```

text

```base
views:
  - type: list
```
"""
        blocks = BasesDetector.detect_blocks(content)
        assert len(blocks) == 2

    def test_has_base_blocks(self):
        assert BasesDetector.has_base_blocks("```base\nviews: []\n```") is True
        assert BasesDetector.has_base_blocks("no blocks here") is False

    def test_unterminated_block_ignored(self):
        # opening fence without closing fence must not crash
        content = "```base\nviews:\n  - type: table\n"
        blocks = BasesDetector.detect_blocks(content)
        assert blocks == []
