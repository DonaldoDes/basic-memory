"""Fork-local YAML loader forbidding anchors and aliases.

``yaml.safe_load`` expands aliases and remains vulnerable to the billion-laughs
amplification attack despite a byte-size bound. This loader derives from
``yaml.SafeLoader`` and raises immediately when an anchor (``&``) or alias
(``*``) is encountered, before any expansion can occur.

Only ``safe_load_no_aliases`` is exposed. ``yaml.load`` / custom constructors /
``eval`` are never used.
"""

import yaml

from basic_memory.bases.errors import BasesParseError


class _NoAliasSafeLoader(yaml.SafeLoader):
    """SafeLoader that refuses YAML anchors and aliases."""


def _forbid_anchor(loader, node):  # pragma: no cover - exercised via compose
    raise BasesParseError("YAML anchors/aliases are not allowed in base blocks")


# Override anchor handling on the composer. compose_node is where anchors are
# registered and aliases resolved; intercepting both event types is sufficient.
def _compose_node(self, parent, index):
    if self.check_event(yaml.events.AliasEvent):
        raise BasesParseError("YAML aliases (*) are not allowed in base blocks")
    event = self.peek_event()
    anchor = getattr(event, "anchor", None)
    if anchor is not None:
        raise BasesParseError("YAML anchors (&) are not allowed in base blocks")
    return yaml.composer.Composer.compose_node(self, parent, index)


_NoAliasSafeLoader.compose_node = _compose_node


def safe_load_no_aliases(text: str):
    """Load YAML with anchors/aliases forbidden.

    Raises:
        BasesParseError: on any YAML error, or on an anchor/alias.
    """
    try:
        return yaml.load(text, Loader=_NoAliasSafeLoader)
    except BasesParseError:
        raise
    except yaml.YAMLError as e:
        raise BasesParseError(f"Invalid YAML: {e}") from e
