"""Custom exceptions for Bases parsing and execution.

Mirror hierarchy of dataview/errors.py, adapted to the Bases surface. The
error_type emitted in the result envelope maps to these classes:

    BasesParseError       -> "parse"
    BasesUnsupportedError -> "unsupported"
    BasesLimitError       -> "limit"
    BasesExecutionError   -> "execution"
    (any other Exception) -> "unexpected"
"""


class BasesError(Exception):
    """Base exception for all Bases-related errors."""

    pass


class BasesParseError(BasesError):
    """Raised when YAML loading or schema validation fails."""

    pass


class BasesUnsupportedError(BasesError):
    """Raised when a construct outside the Phase 1 surface is encountered.

    Examples: formulas, summaries, group-by, property chains, lambdas,
    unknown functions, view types other than table/list.
    """

    pass


class BasesLimitError(BasesError):
    """Raised when an anti-DoS bound is exceeded (size, depth, count)."""

    pass


class BasesExecutionError(BasesError):
    """Raised when query execution fails."""

    pass
