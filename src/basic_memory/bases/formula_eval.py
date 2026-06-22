"""Sandboxed tree-walk interpreter for Bases Phase 2 formulas (US-003 / ADR-004 §2).

This module evaluates the typed formula AST produced by ``formula_parser`` against
a single dataset row. It is **the security core of Phase 2**: the strong invariant
is that *no arbitrary code from note content can ever be executed during formula
evaluation* (ADR-004 §2, milestone invariant).

How that invariant is guaranteed (the 6 pillars of ADR-004 §2):

(a) **Closed whitelist of operations.** There is NO ``eval`` / ``exec`` /
    ``compile`` / ``__import__`` anywhere, no dynamic ``getattr``/``setattr`` on
    content values, no ``globals()``/``locals()``/``vars()``, no dunder access.
    Dispatch is exclusively ``isinstance`` per AST node type — each of the seven
    ``FormulaNode`` types has an explicit branch; an unknown node type raises
    ``BasesUnsupportedError``, never a dynamic dispatch. Functions are resolved
    through a closed dict ``_FUNCTIONS``; any name outside it is refused.

(a bis) **Closed property-chain dispatch.** Members are resolved through a closed
    dict ``_MEMBERS`` (``{"asFile": ..., "path": ..., ...}``) — never ``getattr``
    on a content value. A member outside the table raises ``BasesUnsupportedError``.

(b) **Lambdas over a closed local environment.** A lambda binds only its declared
    parameters plus the current row values; it never captures the host Python
    scope (builtins, globals, module). An identifier that is neither a bound
    param nor a row field resolves to ``None`` — never to a host symbol.

(c) **Dataset-bounded data source.** ``asFile(x)`` returns a virtual file object
    that only carries the string value of ``x`` (typically ``row["file.path"]``).
    ``.path``/``.name``/``.basename`` read that string — **never the filesystem**,
    never ``open``, never ``os``.

(d) **Anti-DoS bounds.** Recursion depth (``MAX_FORMULA_RECURSION``), total node
    evaluations (``MAX_FORMULA_ITERATIONS``) and wall-clock budget
    (``FORMULA_BUDGET_MS``) are enforced; crossing any of them raises
    ``BasesLimitError`` (the block goes inert).

(e) **Total evaluation, never partial.** Any failure — unknown node, function
    outside the whitelist, member outside the table, bound exceeded, or an
    unexpected exception — raises a ``BasesError`` (typed) so the caller renders
    an inert block; ``safe_evaluate_formula`` maps any failure to
    ``(None, error_type)`` so the MCP handler never crashes.

(f) **YAML SafeLoader unchanged.** Out of scope here (Phase 1, ``yaml_loader``).
"""

import datetime as _dt
import time
from collections.abc import Callable
from pathlib import PurePosixPath as _PurePosixPath
from typing import Any

from basic_memory.bases.errors import (
    BasesError,
    BasesExecutionError,
    BasesLimitError,
    BasesUnsupportedError,
)
from basic_memory.bases.formula_ast import (
    FBinOp,
    FCall,
    FField,
    FLambda,
    FLiteral,
    FormulaNode,
    FPropChain,
    FSubscript,
    FThis,
)
from basic_memory.bases.schema import (
    FORMULA_BUDGET_MS,
    MAX_DURATION_DAYS,
    MAX_FORMULA_ITERATIONS,
    MAX_FORMULA_RECURSION,
    MAX_FORMULA_RESULT_SIZE,
    MAX_ITERATOR_CARDINALITY,
)
from basic_memory.dataview.executor.field_resolver import FieldResolver


# --------------------------------------------------------------------------- #
# Virtual file — pillar (c): asFile() never touches the filesystem.
# --------------------------------------------------------------------------- #
class _VirtualFile:
    """A virtual file object derived from a dataset value (never the OS).

    It carries only the string handed to ``asFile(...)`` (typically the row's
    ``file.path``). ``path``/``name``/``basename`` are computed by pure string
    operations — no ``open``, no ``os`` call, no I/O of any kind.

    US-b (ADR-005 §Axe 2): when this object stands for the *row's own file*
    (``file`` in a formula), it also carries the row's outlinks (``_outlinks``)
    and any extra identity strings (``_identities`` — path + permalink). These
    feed ``hasLink``: a pure membership test of the host identity against the
    row's already-resolved outlinks. NEVER the filesystem, never a graph
    recompute — the outlinks are read straight from the dataset row.
    """

    __slots__ = ("_value", "_outlinks", "_identities")

    def __init__(
        self,
        value: Any,
        outlinks: list | None = None,
        identities: list | None = None,
    ):
        self._value = "" if value is None else str(value)
        # Outlinks are a plain list of strings already present in the row — they
        # are read, never computed. Defensive copy of a bounded list.
        self._outlinks: list[str] = [str(x) for x in outlinks] if outlinks else []
        # The set of strings that identify THIS file (for hasLink membership on
        # the *target* side: a row may reference the host by path or permalink).
        self._identities: list[str] = (
            [str(x) for x in identities if x] if identities else [self._value]
        )

    @property
    def path(self) -> str:
        return self._value

    @property
    def name(self) -> str:
        # Basename with extension — pure string split, never os.path.
        return self._value.rsplit("/", 1)[-1]

    @property
    def basename(self) -> str:
        # Filename without extension — pure string ops.
        leaf = self.name
        if "." in leaf:
            return leaf.rsplit(".", 1)[0]
        return leaf

    def has_link_to(self, target: "_VirtualFile") -> bool:
        """True iff this row's outlinks reference the *target* file identity.

        Pure membership test over the row's already-resolved outlinks against
        the target's identity strings (path + permalink). No filesystem, no
        global graph traversal, no recompute (ADR-005 §Axe 2 invariant). A row
        with no outlinks (key absent) cannot match — it is simply empty.
        """
        if not isinstance(target, _VirtualFile):
            return False
        targets = set(target._identities)
        return any(link in targets for link in self._outlinks)


class _HostRef:
    """The resolved value of ``this`` — a closed handle to the host note.

    It exposes EXACTLY ONE member: ``file`` (the host's :class:`_VirtualFile`
    identity). Any other member (``content``, ``frontmatter``, ...) is refused
    by the closed member table — ``this`` is NOT a handle to reload the host
    from disk nor to reference an arbitrary note (ADR-005 §Axe 2 invariant).
    Constructed only from injected host metadata strings, never from the OS.
    """

    __slots__ = ("file",)

    def __init__(self, host: dict[str, Any]):
        path = host.get("path") or host.get("permalink") or ""
        permalink = host.get("permalink")
        title = host.get("title")
        # The host file's identity strings: path, permalink AND title so a row
        # matches in hasLink whichever form it linked by. US-f live-path fix:
        # the provider populates a row's outlinks from its outgoing relations as
        # the target PERMALINK *and* the raw ``to_name`` — and ``to_name`` is
        # frequently the host's TITLE (a ``[[Title]]`` wikilink). Without the
        # title here, every Activity block whose backlinks were authored by
        # title resolved to 0 (the production failure on the ~169 Activity
        # blocks). Identity stays a bounded set of dataset strings — no FS, no
        # graph recompute (ADR-005 §Axe 2 invariant preserved).
        identities = [v for v in (path, permalink, title) if v]
        self.file = _VirtualFile(path, identities=identities or [path])


# --------------------------------------------------------------------------- #
# Error classification (pillar (e)) — maps an exception to an envelope type.
# --------------------------------------------------------------------------- #
def error_type_for(exc: BaseException) -> str:
    """Map an exception to the result-envelope ``error_type`` (ADR-004 §6).

    BasesUnsupportedError -> "unsupported"
    BasesLimitError       -> "limit"
    BasesExecutionError   -> "execution"
    (any other exception) -> "unexpected"
    """
    if isinstance(exc, BasesUnsupportedError):
        return "unsupported"
    if isinstance(exc, BasesLimitError):
        return "limit"
    if isinstance(exc, BasesExecutionError):
        return "execution"
    return "unexpected"


class FormulaEvaluator:
    """Tree-walk interpreter over a typed formula AST against one dataset row.

    Each node type has an explicit ``isinstance`` branch in :meth:`eval`. There
    is no dynamic dispatch, no ``eval``/``exec``, no ``getattr`` on content.
    """

    def __init__(
        self,
        row: dict[str, Any],
        host: dict[str, Any] | None = None,
        now: _dt.datetime | None = None,
    ):
        self.row = row
        # US-b (ADR-005 §Axe 2): the host-note identity backing ``this``. A plain
        # dict ({"path": ..., "permalink": ..., "title": ...}) injected from the
        # caller via note_metadata. ``None`` when no host is wired → ``this``
        # resolves to None and the block goes inert. NEVER note content, never a
        # filesystem handle — only the identity strings.
        self._host = host
        # US-2 (ADR-006 §Gap #4): the reference instant backing the ``today`` /
        # ``now`` keywords. INJECTED by the caller (tests pass a fixed clock for
        # determinism; production passes the real wall clock). Captured ONCE here
        # so every ``today``/``now`` within a single evaluation is consistent.
        # ``None`` → fall back to the real clock at field-resolution time.
        self._now = now
        # Closed local environment for lambda params (pillar (b)). Empty at the
        # top level; populated only with bound parameters during a lambda call.
        self._env: dict[str, Any] = {}
        self._depth = 0
        self._iterations = 0
        self._start = time.monotonic()

    # ----------------------------------------------------------- bounds (d)
    def _tick(self) -> None:
        """Enforce iteration + time budgets on every node evaluation."""
        self._iterations += 1
        if self._iterations > MAX_FORMULA_ITERATIONS:
            raise BasesLimitError(f"Formula evaluation exceeds {MAX_FORMULA_ITERATIONS} iterations")
        elapsed_ms = (time.monotonic() - self._start) * 1000.0
        if elapsed_ms > FORMULA_BUDGET_MS:
            raise BasesLimitError(f"Formula evaluation exceeds {FORMULA_BUDGET_MS}ms budget")

    # ------------------------------------------- centralized size guard (d)
    @staticmethod
    def _guard_result_size(value: Any) -> Any:
        """Refuse any sequence value whose length exceeds the size bound.

        Pillar (d), allocation-bomb class — the CENTRALIZED net. The pre-
        allocation projections (:meth:`_guard_amplifying_result`,
        :meth:`_project_function_size`) stop the *known* amplifiers before they
        allocate. This post-value guard is the catch-all that closes the WHOLE
        class: every sequence (str/list/bytes/tuple) produced by *any* node —
        operator, function call, or property-chain member, including future
        whitelist entries — is checked here. A value over the bound raises
        ``BasesLimitError`` (type "limit") so the block goes inert.

        Non-sequence values (int/float/bool/None/objects) are returned
        unchanged: they are intrinsically size-bounded and cannot OOM.
        """
        if isinstance(value, (str, bytes, list, tuple)) and len(value) > MAX_FORMULA_RESULT_SIZE:
            raise BasesLimitError(
                f"Formula result size {len(value)} exceeds {MAX_FORMULA_RESULT_SIZE}"
            )
        return value

    # ------------------------------------ pre-allocation projection (funcs)
    @staticmethod
    def _project_function_size(fn_name: str, args: list[Any]) -> None:
        """Project the result size of an amplifying FUNCTION BEFORE it runs.

        Pillar (d): the centralized post-value guard above arrives *after* the
        giant value has been materialised, so for functions that can amplify an
        operand we project the size and refuse first — the huge value is never
        built. Today the only amplifying whitelist function is ``replace``; any
        future amplifying function MUST be added here (see the coverage
        enumeration in the build attestation).

        ``replace(s, old, new)`` grows when ``len(new) > len(old)``. Projected
        result length = ``len(s) + count(old) * (len(new) - len(old))`` where
        ``count(old)`` is the number of non-overlapping occurrences. We use the
        exact projection (``s.count(old)``) — counting scans the existing string
        but allocates nothing new; the amplified result is the only large
        allocation and it is what we are preventing.
        """
        if fn_name != "replace":
            return
        s = _to_str(_arg(args, 0))
        old = _to_str(_arg(args, 1))
        new = _to_str(_arg(args, 2))
        delta = len(new) - len(old)
        if delta <= 0:
            # Shrinking or same-size replacement cannot amplify.
            return
        if old == "":
            # Python inserts ``new`` between every char and at both ends:
            # projected = len(s) + (len(s) + 1) * len(new).
            projected = len(s) + (len(s) + 1) * len(new)
        else:
            occurrences = s.count(old)
            projected = len(s) + occurrences * delta
        if projected > MAX_FORMULA_RESULT_SIZE:
            raise BasesLimitError(
                f"Formula result size {projected} exceeds {MAX_FORMULA_RESULT_SIZE} (replace)"
            )

    # ----------------------------------------------- result-size guard (d)
    @staticmethod
    def _guard_amplifying_result(op: str, left: Any, right: Any) -> None:
        """Refuse an amplifying op whose *result* would exceed the size bound.

        Pillar (d), allocation-bomb class: the iteration/recursion/time bounds
        do NOT cap how large a *single* allocation may be. ``s * 10**6`` (1 KiB
        content × 1e6) allocates ~1 GiB in one node and OOMs the host before any
        other bound fires. So for the only two ops that can amplify an operand
        (``*`` repetition, ``+`` concatenation of str/list), compute the
        PROJECTED result size and raise BasesLimitError BEFORE the operation —
        the giant value is never materialised.

        Numeric ``*``/``+`` (int/float) does not amplify size and is ignored.
        """
        if op == "*":
            # str/list repetition: one operand is the sequence, the other the
            # (possibly attacker-controlled) repeat count.
            seq, count = (left, right) if isinstance(left, (str, list)) else (right, left)
            if (
                isinstance(seq, (str, list))
                and isinstance(count, int)
                and not isinstance(count, bool)
            ):
                # Projected length = len(seq) * count, computed WITHOUT building
                # the result. Negative/zero counts produce an empty sequence.
                if count > 0:
                    projected = len(seq) * count
                    if projected > MAX_FORMULA_RESULT_SIZE:
                        raise BasesLimitError(
                            f"Formula result size {projected} exceeds "
                            f"{MAX_FORMULA_RESULT_SIZE} (repetition)"
                        )
        elif op == "+":
            # str+str / list+list concatenation: projected size = sum of lengths.
            if (isinstance(left, str) and isinstance(right, str)) or (
                isinstance(left, list) and isinstance(right, list)
            ):
                projected = len(left) + len(right)
                if projected > MAX_FORMULA_RESULT_SIZE:
                    raise BasesLimitError(
                        f"Formula result size {projected} exceeds "
                        f"{MAX_FORMULA_RESULT_SIZE} (concatenation)"
                    )

    # ------------------------------------------------------------- dispatch
    def eval(self, node: FormulaNode) -> Any:
        # eval = evaluate AST node, PAS le builtin Python `eval`.
        """Evaluate a node. Explicit per-type dispatch, depth/iter/time bounded."""
        self._tick()
        self._depth += 1
        # Use >= so the guard fires exactly when the counter reaches the bound:
        # depths 1..(MAX-1) are allowed, the MAX-th level is refused, and the
        # message is accurate (no off-by-one between message and reached depth).
        if self._depth >= MAX_FORMULA_RECURSION:
            raise BasesLimitError(
                f"Formula evaluation exceeds recursion depth {MAX_FORMULA_RECURSION}"
            )
        try:
            # Explicit isinstance dispatch — one branch per FormulaNode type.
            if isinstance(node, FLiteral):
                return node.value
            if isinstance(node, FField):
                return self._eval_field(node)
            if isinstance(node, FBinOp):
                return self._eval_binop(node)
            if isinstance(node, FCall):
                return self._eval_call(node)
            if isinstance(node, FPropChain):
                return self._eval_propchain(node)
            if isinstance(node, FLambda):
                return self._eval_lambda(node)
            if isinstance(node, FThis):
                return self._eval_this(node)
            if isinstance(node, FSubscript):
                return self._eval_subscript(node)
            # Unknown node type — never a dynamic fallback.
            raise BasesUnsupportedError(f"Unsupported formula node type: {type(node).__name__}")
        finally:
            self._depth -= 1

    # ---------------------------------------------------------------- FThis
    def _eval_this(self, node: FThis) -> Any:
        """Resolve ``this`` to the host wrapper, or ``None`` with no host.

        US-b (ADR-005 §Axe 2): bounded to the injected host identity. With no
        host wired, ``this`` is ``None`` (the block goes inert downstream) —
        never the current row, never an arbitrary note.
        """
        if not self._host:
            return None
        return _HostRef(self._host)

    # ----------------------------------------------------------- FSubscript
    def _eval_subscript(self, node: FSubscript) -> Any:
        """Evaluate ``receiver[index]`` — bounded indexed access (US-c).

        Security/robustness invariants (ADR-005 §Axe 3, continuity ADR-004 §2):
        - The index MUST be a plain ``int`` (a ``bool`` is NOT an int here, so
          ``true``/``false`` never index 1/0). A non-integer index (str, float,
          bool, None) yields ``None`` — the row degrades gracefully, never an
          exception.
        - The receiver must be an indexable SEQUENCE the sandbox produces:
          ``list``, ``tuple`` or ``str``. Anything else (dict, None, int, a
          ``_VirtualFile``, ...) yields ``None`` — NO ``getattr``/``__getitem__``
          probing on arbitrary content objects, no mapping key access.
        - Out-of-range indices (positive or negative) yield ``None`` — bounded
          access, never an ``IndexError``, never arbitrary memory access. A huge
          index allocates nothing (it is a pure bounds comparison).
        - There is NO slice path here: the parser already rejected ``[a:b]``; the
          index is therefore always a scalar, never a ``slice`` object.
        """
        receiver = self.eval(node.receiver)
        index = self.eval(node.index)
        # Reject non-integer indices (bool excluded — it is an int subclass).
        if not isinstance(index, int) or isinstance(index, bool):
            return None
        # Only sandbox-produced sequences are indexable — never a mapping nor an
        # arbitrary object. This is an explicit type allowlist, not getattr.
        if not isinstance(receiver, (list, tuple, str)):
            return None
        length = len(receiver)
        # Bounds check WITHOUT relying on Python's IndexError: covers positive and
        # negative indices uniformly (Obsidian uses non-negative; negative kept as
        # a bounded Python convenience, still None when out of range).
        if index < -length or index >= length:
            return None
        return self._guard_result_size(receiver[index])

    # --------------------------------------------------------------- FField
    def _eval_field(self, node: FField) -> Any:
        """Resolve a field from the closed lambda env first, then the row only.

        Pillar (b): bound lambda params shadow row fields. An identifier that is
        neither a bound param nor a row field resolves to ``None`` — never a host
        symbol (no globals/builtins access).
        """
        name = node.name
        if name in self._env:
            return self._env[name]
        # US-2 (ADR-006 §Gap #4): the date keywords ``today`` / ``now`` resolve to
        # the INJECTED reference clock (deterministic in tests), NOT a row field
        # and NOT the OS clock read inline. A row field of the same name takes
        # precedence (checked above via env, below via row) so a note that
        # genuinely carries a ``today`` field is never shadowed. ``today`` is the
        # date at midnight; ``now`` the full datetime.
        if name in ("today", "now") and name not in self.row:
            ref = self._now if self._now is not None else _dt.datetime.now().astimezone()
            return ref.date() if name == "today" else ref
        # US-b: the bare ``file`` reference (receiver of ``file.hasLink(...)``)
        # resolves to a _VirtualFile of the row carrying the row's outlinks — read
        # straight from the dataset row, never the OS, never a graph recompute.
        if name == "file":
            return self._row_file()
        # US-3 (ADR-006 §Gap #5): ``file.links`` / ``file.outlinks`` resolve to
        # the row's outlinks list (the union the provider exposes), read straight
        # from the dataset row — never the OS, never a graph recompute. This lets
        # an iterator ``any(file.outlinks, l => …)`` / ``filter(file.links, …)``
        # scan the same outlinks ``file.hasLink`` uses. Resolved here (in the
        # bases sandbox), NOT in the core ``FieldResolver`` (diff confined to
        # ``bases/``). A row field of the same flat key still takes precedence
        # below (so a synthetic flat dataset is honoured first).
        if name in ("file.links", "file.outlinks") and name not in self.row:
            return self._row_file()._outlinks
        # US-8 (ADR-006): ``file.folder`` is a requestable file member. The
        # provider (knowledge_router) already nests an explicit ``folder`` derived
        # from ``file_path``; honour it verbatim. But an executor-projected FLAT
        # row may carry only ``file.path`` and no folder — in that case DERIVE the
        # folder from the path's parent so ``startsWith(file.folder, …)`` and the
        # ``file.folder`` column read the REAL dossier instead of an empty string
        # (the silent-empty trap). Derivation is a pure string parent op on the
        # dataset row (PurePosixPath) — never the filesystem, never getattr on
        # content. Resolved here in the bases sandbox (diff confined to bases/),
        # mirroring the ``file.links`` resolution above.
        if name == "file.folder":
            return self._row_folder()
        # Executor-projected rows carry file.* as a flat top-level key
        # (e.g. row["file.path"]); honour it first so asFile().path reads the
        # dataset value directly (US-003 spec). Then fall back to FieldResolver
        # for nested-shape datasets. Either way: dataset only, never the OS.
        if name in self.row:
            return self.row[name]
        return FieldResolver.resolve_field(self.row, name)

    def _row_folder(self) -> str:
        """Resolve ``file.folder`` from the dataset row — explicit, else derived.

        Order (dataset only, never the OS):
        1. explicit nested ``row["file"]["folder"]`` (provider shape), or flat
           ``row["file.folder"]`` (synthetic/override) — honoured verbatim;
        2. otherwise DERIVE from the row's ``file.path`` parent
           (``PurePosixPath(path).parent``) — a pure string operation. A note at
           the vault root has no parent → empty string (not ``"."``).
        Never opens, stats or lists a path: derivation is string-only.
        """
        file_obj = self.row.get("file")
        if isinstance(file_obj, dict) and file_obj.get("folder"):
            return _to_str(file_obj["folder"])
        flat = self.row.get("file.folder")
        if flat:
            return _to_str(flat)
        path = ""
        if isinstance(file_obj, dict):
            path = file_obj.get("path") or ""
        path = path or self.row.get("file.path") or self.row.get("path") or ""
        if not path:
            return ""
        parent = _PurePosixPath(_to_str(path)).parent
        # ``PurePosixPath("readme.md").parent`` is ``"."`` — a root-level note has
        # no folder, expose it as the empty string (not the cwd marker).
        return "" if str(parent) == "." else str(parent)

    def _row_file(self) -> "_VirtualFile":
        """Build the row's own file object, carrying its dataset outlinks.

        Reads ``file.path``/``file.outlinks`` (nested or flat shape) from the
        row only. The outlinks are whatever the dataset provider already placed
        on the row — this method NEVER opens a file nor recomputes links.
        """
        file_obj = self.row.get("file")
        if isinstance(file_obj, dict):
            path = file_obj.get("path", "")
            outlinks = file_obj.get("outlinks") or []
        else:
            path = self.row.get("file.path") or self.row.get("path") or ""
            outlinks = self.row.get("file.outlinks") or self.row.get("outlinks") or []
        permalink = self.row.get("permalink")
        identities = [v for v in (path, permalink) if v]
        return _VirtualFile(path, outlinks=outlinks, identities=identities or [path])

    # --------------------------------------------------------------- FBinOp
    def _eval_binop(self, node: FBinOp) -> Any:
        op = node.op
        # Short-circuit logical operators.
        if op == "&&":
            return bool(self.eval(node.left)) and bool(self.eval(node.right))
        if op == "||":
            return bool(self.eval(node.left)) or bool(self.eval(node.right))

        left = self.eval(node.left)
        right = self.eval(node.right)
        # US-2 (ADR-006 §Gap #4): when ONE operand is a real date/datetime and the
        # other is an ISO string (the ``file.mtime``/``file.ctime`` provider form),
        # coerce the string to a date so ``file.mtime > date(today) - dur("7 days")``
        # compares dates, not str-vs-date (which TypeErrors → inert). Coercion only
        # fires for comparison/arithmetic ops and only when the OTHER side is
        # already a date — it never turns two strings into dates (non-regression:
        # ``title == "foo"`` stays a string compare).
        left, right = self._coerce_date_operands(op, left, right)
        # Bound the PROJECTED result size of amplifying ops BEFORE allocating
        # (pillar (d) — allocation-bomb class). Raises BasesLimitError on excess.
        self._guard_amplifying_result(op, left, right)
        try:
            if op == "+":
                return self._guard_result_size(left + right)
            if op == "-":
                return left - right
            if op == "*":
                return self._guard_result_size(left * right)
            if op == "/":
                if right == 0:
                    raise BasesExecutionError("Division by zero in formula")
                return left / right
            if op == "==":
                return left == right
            if op == "!=":
                return left != right
            if op == "<":
                return left < right
            if op == ">":
                return left > right
            if op == "<=":
                return left <= right
            if op == ">=":
                return left >= right
        except BasesError:
            raise
        except Exception as exc:  # noqa: BLE001 — typed degradation, never a crash
            raise BasesExecutionError(f"Operator '{op}' failed on operands") from exc
        # Operator outside the whitelist — explicit refusal.
        raise BasesUnsupportedError(f"Unsupported binary operator: {op!r}")

    # ----------------------------------------------- date coercion (US-2)
    _DATE_OPS = frozenset({"-", "+", "<", ">", "<=", ">=", "==", "!="})

    @classmethod
    def _coerce_date_operands(cls, op: str, left: Any, right: Any) -> tuple[Any, Any]:
        """Coerce a string operand to a date when the other side is a date.

        Fires ONLY for date-relevant ops and ONLY when exactly one side is a real
        ``date``/``datetime`` and the other is an ISO string — turning a provider
        ``file.mtime`` string into a comparable date. When the string is not a
        valid date it is left unchanged (the op then degrades to None via the
        binop's typed-exception handler). To avoid the Python ``date`` vs
        ``datetime`` TypeError, a ``datetime`` is narrowed to its ``.date()`` when
        compared/combined with a plain ``date``. Two non-dates pass through
        untouched (non-regression: ordinary string/number ops unchanged).
        """
        if op not in cls._DATE_OPS:
            return left, right
        left_is_date = isinstance(left, (_dt.date, _dt.datetime))
        right_is_date = isinstance(right, (_dt.date, _dt.datetime))
        # Coerce the bare string side to a date when the other is a date.
        if left_is_date and isinstance(right, str):
            coerced = _coerce_to_date(right)
            if coerced is not None:
                right, right_is_date = coerced, True
        elif right_is_date and isinstance(left, str):
            coerced = _coerce_to_date(left)
            if coerced is not None:
                left, left_is_date = coerced, True
        # Normalise date vs datetime mismatch to plain dates (only when both are
        # date-like; never touches a timedelta on the RHS of date ± dur).
        if left_is_date and right_is_date:
            l_is_dt = isinstance(left, _dt.datetime)
            r_is_dt = isinstance(right, _dt.datetime)
            if l_is_dt != r_is_dt:
                if l_is_dt:
                    left = left.date()
                if r_is_dt:
                    right = right.date()
        return left, right

    # ---------------------------------------------------------------- FCall
    def _eval_call(self, node: FCall) -> Any:
        fn = _FUNCTIONS.get(node.fn)
        if fn is None:
            # Function outside the closed whitelist — refused, never executed.
            raise BasesUnsupportedError(
                f"Function '{node.fn}' is outside the closed formula whitelist"
            )
        args = [self.eval(a) for a in node.args]
        # Project amplifying functions (e.g. replace) BEFORE they allocate
        # (pillar (d)). Raises BasesLimitError on excess; the giant value is
        # never built.
        self._project_function_size(node.fn, args)
        try:
            result = fn(args)
        except BasesError:
            raise
        except Exception as exc:  # noqa: BLE001 — typed degradation
            raise BasesExecutionError(f"Function '{node.fn}' failed during evaluation") from exc
        # Centralized net (pillar (d)): any sequence produced by ANY function —
        # including future whitelist entries not individually projected — is
        # size-bounded here.
        return self._guard_result_size(result)

    # ----------------------------------------------------------- FPropChain
    def _eval_propchain(self, node: FPropChain) -> Any:
        member = _MEMBERS.get(node.member)
        if member is None:
            # Member outside the closed table (incl. any dunder) — refused.
            # getattr is NEVER called on the receiver value.
            raise BasesUnsupportedError(
                f"Property-chain member '{node.member}' is outside the closed table"
            )
        receiver = self.eval(node.receiver)
        args = [self.eval(a) for a in node.args]
        try:
            result = member(receiver, args)
        except BasesError:
            raise
        except Exception as exc:  # noqa: BLE001 — typed degradation
            raise BasesExecutionError(
                f"Property-chain member '{node.member}' failed during evaluation"
            ) from exc
        # Centralized net (pillar (d)): a member returning a sequence (path /
        # name / basename) is size-bounded here.
        return self._guard_result_size(result)

    # -------------------------------------------------------------- FLambda
    def _eval_lambda(self, node: FLambda) -> Callable[..., Any]:
        """Compile a lambda into a Python callable over a CLOSED environment.

        The returned callable, when invoked with positional args, binds them to
        the declared params and evaluates the body with that closed env layered
        over the current row. It captures ONLY this evaluator's row/env — never
        the host Python scope (pillar (b)).
        """
        params = node.params
        body = node.body

        def _invoke(*call_args: Any) -> Any:
            saved = self._env
            new_env = dict(saved)
            for i, p in enumerate(params):
                new_env[p] = call_args[i] if i < len(call_args) else None
            self._env = new_env
            try:
                return self.eval(body)
            finally:
                self._env = saved

        return _invoke


# --------------------------------------------------------------------------- #
# Closed function whitelist — pillar (a). Each entry takes the already-evaluated
# argument list and returns a value. No name outside this dict is callable.
# Surface figée par ADR-004 §2.(a) / US-001.
# --------------------------------------------------------------------------- #
def _to_str(v: Any) -> str:
    return "" if v is None else str(v)


def _arg(args: list[Any], i: int, default: Any = None) -> Any:
    return args[i] if i < len(args) else default


# --------------------------------------------------------------------------- #
# Date / duration sandbox helpers (US-2 / ADR-006 §Gap #4).
#
# These are pure, closed objects audited like every other whitelist entry: they
# build/parse stdlib ``datetime`` values from already-evaluated arguments and
# never touch the OS clock directly (the clock is the INJECTED ``now``, resolved
# through the ``today``/``now`` field keywords — see ``_eval_field``). No eval,
# no exec, no getattr; ``dateformat`` maps a closed set of Luxon/Dataview tokens
# to ``strftime`` directives. Anti-DoS: a duration whose magnitude exceeds
# ``MAX_DURATION_DAYS`` is refused (BasesLimitError → inert block) so a
# pathological ``dur(1e12, "days")`` allocates nothing and never overflows.
# --------------------------------------------------------------------------- #

# Unit → (timedelta kwarg, days-per-unit) for the duration magnitude bound.
_DUR_UNITS = {
    "day": ("days", 1.0),
    "days": ("days", 1.0),
    "week": ("weeks", 7.0),
    "weeks": ("weeks", 7.0),
    "hour": ("hours", 1.0 / 24.0),
    "hours": ("hours", 1.0 / 24.0),
    "minute": ("minutes", 1.0 / 1440.0),
    "minutes": ("minutes", 1.0 / 1440.0),
    "second": ("seconds", 1.0 / 86400.0),
    "seconds": ("seconds", 1.0 / 86400.0),
}

# Closed Luxon/Dataview → strftime token map for dateformat. Ordered longest
# first so multi-char tokens are matched before their single-char prefixes.
_DATEFORMAT_TOKENS = [
    ("yyyy", "%Y"),
    ("MM", "%m"),
    ("dd", "%d"),
    ("HH", "%H"),
    ("mm", "%M"),
    ("ss", "%S"),
]


def _make_dur(args: list[Any]) -> _dt.timedelta:
    """Build a ``timedelta`` from ``dur("7 days")`` or ``dur(7, "days")``.

    Both Dataview-faithful forms are accepted. The magnitude is bounded
    (``MAX_DURATION_DAYS``): a huge or hugely-negative count raises
    BasesLimitError so the block goes inert — the timedelta is never built.
    An unknown unit or unparseable spec raises BasesExecutionError (inert).
    """
    a0 = _arg(args, 0)
    a1 = _arg(args, 1)
    if a1 is not None:
        # Two-arg form: dur(<number>, "<unit>").
        try:
            count = float(a0)
        except (TypeError, ValueError) as exc:
            raise BasesExecutionError(f"dur() count is not numeric: {a0!r}") from exc
        unit = _to_str(a1).strip().lower()
    else:
        # One-arg string form: dur("7 days").
        spec = _to_str(a0).strip()
        parts = spec.split()
        if len(parts) != 2:
            raise BasesExecutionError(f"dur() spec must be '<n> <unit>': {spec!r}")
        try:
            count = float(parts[0])
        except ValueError as exc:
            raise BasesExecutionError(f"dur() count is not numeric: {parts[0]!r}") from exc
        unit = parts[1].lower()
    if unit not in _DUR_UNITS:
        raise BasesExecutionError(f"dur() unknown unit: {unit!r}")
    kwarg, days_per_unit = _DUR_UNITS[unit]
    # Bound the magnitude BEFORE constructing the timedelta (anti-DoS).
    if abs(count * days_per_unit) > MAX_DURATION_DAYS:
        raise BasesLimitError(f"Duration magnitude exceeds {MAX_DURATION_DAYS} days bound")
    return _dt.timedelta(**{kwarg: count})


def _coerce_to_date(value: Any) -> _dt.date | _dt.datetime | None:
    """Coerce an already-evaluated value into a date/datetime, or None.

    Accepts a ``date``/``datetime`` as-is (the ``today``/``now`` keywords and
    nested ``date()`` calls already produce these) and parses an ISO-8601 string
    (the ``file.mtime``/``file.ctime`` provider form). Anything else → None.
    Never raises — the caller decides whether None is inert or falsy.
    """
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value
    if isinstance(value, str) and value:
        text = value.strip()
        # A date-only string (no time component) → a plain ``date`` so that
        # ``date("2026-06-10")`` is a date, not a midnight datetime. A string
        # carrying a time ("T" or ":") → a full ``datetime``.
        has_time = "T" in text or ":" in text
        if not has_time:
            try:
                return _dt.date.fromisoformat(text)
            except ValueError:
                return None
        try:
            return _dt.datetime.fromisoformat(text)
        except ValueError:
            return None
    return None


def _make_date(args: list[Any]) -> _dt.date | _dt.datetime:
    """``date(x)`` — build a real date from ``today``/``now`` (already resolved)
    or an ISO string. An unparseable value raises BasesExecutionError (inert)."""
    value = _arg(args, 0)
    coerced = _coerce_to_date(value)
    if coerced is None:
        raise BasesExecutionError(f"date() cannot parse value: {value!r}")
    return coerced


def _dateformat(args: list[Any]) -> str:
    """``dateformat(value, "pattern")`` — format a date with a closed token map.

    ``value`` is coerced to a date/datetime (a ``date()`` result or a raw ISO
    string). Tokens (yyyy/MM/dd/HH/mm/ss) map to ``strftime`` directives; any
    other literal text in the pattern is preserved verbatim (an unknown token is
    therefore rendered as-is — graceful degradation, never a crash). A value that
    is not a date raises BasesExecutionError (inert)."""
    value = _arg(args, 0)
    pattern = _to_str(_arg(args, 1))
    coerced = _coerce_to_date(value)
    if coerced is None:
        raise BasesExecutionError(f"dateformat() value is not a date: {value!r}")
    # Translate the closed token set to strftime, leaving every other character
    # verbatim. Longest tokens first (see _DATEFORMAT_TOKENS order).
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        matched = False
        for token, directive in _DATEFORMAT_TOKENS:
            if pattern.startswith(token, i):
                out.append(coerced.strftime(directive))
                i += len(token)
                matched = True
                break
        if not matched:
            out.append(pattern[i])
            i += 1
    return "".join(out)


# --------------------------------------------------------------------------- #
# Lambda iterators OUTSIDE an aggregate (US-3 / ADR-006 §Gap #5).
#
# ``any(list, lambda)`` and ``filter(list, lambda)`` are closed, audited entries
# of ``_FUNCTIONS`` like every other whitelist function. They receive the already
# evaluated argument list: ``args[0]`` is the iterable (resolved from the dataset
# row by the evaluator — never the filesystem) and ``args[1]`` is the predicate,
# which is the EXACT callable produced by ``FormulaEvaluator._eval_lambda`` (the
# P2 closed-env closure) — there is no second evaluator. The predicate runs over
# the host Python ``bool``; no eval/exec/getattr is introduced.
#
# Anti-DoS (ADR-006 §"Bornes anti-DoS"): the iterable length is bounded by
# ``MAX_ITERATOR_CARDINALITY`` BEFORE any predicate runs. Over the bound the
# whole block is inert (BasesLimitError → error_type "limit") with NO partial
# evaluation — a hostile note must not silently drop data.
# --------------------------------------------------------------------------- #
def _as_iter_list(value: Any) -> list[Any]:
    """Coerce the iterator's first arg to a bounded list (no FS, no generator).

    ``None``/scalar → empty list (a non-list receiver yields no elements, never a
    crash). A list/tuple is length-checked against the cardinality bound first.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_ITERATOR_CARDINALITY:
            raise BasesLimitError(
                f"Iterator over {len(value)} elements exceeds the bound of "
                f"{MAX_ITERATOR_CARDINALITY}"
            )
        return list(value)
    # A bare string/scalar is not an iterable-of-elements for our purposes.
    return []


def _iter_predicate(args: list[Any]) -> Callable[[Any], Any]:
    """Return the predicate callable (the P2 ``_eval_lambda`` closure).

    A non-callable second argument makes the iterator inert via a typed error
    (never a getattr/exec fallback).
    """
    pred = _arg(args, 1)
    if not callable(pred):
        raise BasesExecutionError("Iterator predicate is not a lambda")
    return pred


def _fn_any(args: list[Any]) -> bool:
    items = _as_iter_list(_arg(args, 0))
    predicate = _iter_predicate(args)
    return any(bool(predicate(element)) for element in items)


def _fn_filter(args: list[Any]) -> list[Any]:
    items = _as_iter_list(_arg(args, 0))
    predicate = _iter_predicate(args)
    return [element for element in items if bool(predicate(element))]


_FUNCTIONS = {
    # --- Text ---
    "lower": lambda a: _to_str(_arg(a, 0)).lower(),
    "upper": lambda a: _to_str(_arg(a, 0)).upper(),
    "length": lambda a: len(_arg(a, 0)) if _arg(a, 0) is not None else 0,
    "contains": lambda a: _to_str(_arg(a, 1)) in _to_str(_arg(a, 0)),
    "startsWith": lambda a: _to_str(_arg(a, 0)).startswith(_to_str(_arg(a, 1))),
    "endsWith": lambda a: _to_str(_arg(a, 0)).endswith(_to_str(_arg(a, 1))),
    "replace": lambda a: _to_str(_arg(a, 0)).replace(_to_str(_arg(a, 1)), _to_str(_arg(a, 2))),
    "string": lambda a: _to_str(_arg(a, 0)),
    # --- Numeric ---
    # ndigits is capped to a sane range: a huge ndigits cannot drive an
    # unbounded internal allocation (allocation-bomb class, ADR-004 §2.(d)).
    "round": lambda a: round(_arg(a, 0), max(-323, min(323, int(_arg(a, 1, 0))))),
    "number": lambda a: float(_arg(a, 0)),
    "min": lambda a: min(a) if a else None,
    "max": lambda a: max(a) if a else None,
    # --- Date / duration (US-2 / ADR-006 §Gap #4). Real datetime arithmetic;
    # the clock is the INJECTED `now` (today/now keywords), never os.time. ---
    "dateformat": _dateformat,
    "date": _make_date,
    "dur": _make_dur,
    # --- Logic / default ---
    "default": lambda a: _arg(a, 0) if _arg(a, 0) is not None else _arg(a, 1),
    "if": lambda a: _arg(a, 1) if _arg(a, 0) else _arg(a, 2),
    # --- Links ---
    "link": lambda a: f"[[{_to_str(_arg(a, 0))}]]",
    # --- Aggregates (used by GROUP BY — US-005; defined here, closed) ---
    "sum": lambda a: sum(x for x in a if isinstance(x, (int, float))),
    "average": (
        lambda a: (
            (
                sum(x for x in a if isinstance(x, (int, float)))
                / len([x for x in a if isinstance(x, (int, float))])
            )
            if any(isinstance(x, (int, float)) for x in a)
            else None
        )
    ),
    "count": lambda a: len(a),
    # --- Lambda iterators OUTSIDE an aggregate (US-3 / ADR-006 §Gap #5) ---
    # any(list, lambda) -> bool ; filter(list, lambda) -> sub-list. The lambda is
    # the P2 _eval_lambda closure; the list is bounded by MAX_ITERATOR_CARDINALITY
    # (block inert over the bound, no partial eval). No second evaluator.
    "any": _fn_any,
    "filter": _fn_filter,
    # --- Property-chain converter usable in call position ---
    "asFile": lambda a: _VirtualFile(_arg(a, 0)),
}


# --------------------------------------------------------------------------- #
# Closed property-chain member table — pillar (a bis). Each entry takes the
# resolved receiver + evaluated args. NEVER getattr on the receiver value.
# Table figée par ADR-004 §2.(a bis) / US-001 (7 members).
# --------------------------------------------------------------------------- #
def _member_asfile(receiver: Any, args: list[Any]) -> _VirtualFile:
    return _VirtualFile(receiver)


def _member_path(receiver: Any, args: list[Any]) -> str:
    # Pillar (c): read the dataset value, never the filesystem.
    if isinstance(receiver, _VirtualFile):
        return receiver.path
    return _to_str(receiver)


def _member_name(receiver: Any, args: list[Any]) -> str:
    if isinstance(receiver, _VirtualFile):
        return receiver.name
    return _to_str(receiver).rsplit("/", 1)[-1]


def _member_basename(receiver: Any, args: list[Any]) -> str:
    if isinstance(receiver, _VirtualFile):
        return receiver.basename
    return _VirtualFile(receiver).basename


def _member_startswith(receiver: Any, args: list[Any]) -> bool:
    base = receiver.path if isinstance(receiver, _VirtualFile) else _to_str(receiver)
    return base.startswith(_to_str(_arg(args, 0)))


def _member_endswith(receiver: Any, args: list[Any]) -> bool:
    base = receiver.path if isinstance(receiver, _VirtualFile) else _to_str(receiver)
    return base.endswith(_to_str(_arg(args, 0)))


def _member_contains(receiver: Any, args: list[Any]) -> bool:
    """``receiver.contains(arg)`` — membership on a list, substring on a string.

    US-1 / ADR-006 §Gap #1 (NC-1): when the receiver is a LIST-OF-LINKS (a body
    relation exposed by the provider, e.g. ``part_of`` / ``member_of``), this is
    an identity-aware MEMBERSHIP test — the tested element (a ``_VirtualFile``
    host identity from ``this.file``, or a bare link string) matches iff one of
    its identity strings is an exact element of the list. NEVER a substring of
    the stringified list (which would false-match a permalink prefix). Pure set
    membership over already-resolved dataset strings — no FS, no getattr, no
    graph recompute (ADR-006 §Invariants).

    For a STRING receiver the substring semantics is UNCHANGED (non-regression:
    ``title.contains("foo")`` stays a substring test).
    """
    arg = _arg(args, 0)
    # List-of-links receiver → identity-aware membership.
    if isinstance(receiver, (list, tuple)):
        # The set of strings that identify the element being tested.
        if isinstance(arg, _VirtualFile):
            needles = set(arg._identities)
        else:
            needles = {_to_str(arg)}
        # Each list element is a link string already in the dataset. Membership
        # is exact-string equality between an element and one of the needles.
        return any(_to_str(elem) in needles for elem in receiver)
    # String (or _VirtualFile path) receiver → substring, unchanged.
    base = receiver.path if isinstance(receiver, _VirtualFile) else _to_str(receiver)
    return _to_str(arg) in base


def _member_file(receiver: Any, args: list[Any]) -> Any:
    """``this.file`` — the host file identity (US-b / ADR-005 §Axe 2).

    Only a ``_HostRef`` (the resolved ``this``) exposes ``.file``. Applied to
    anything else (e.g. a None when no host is wired, or a content value) it
    returns None — never ``getattr`` on the receiver, never a filesystem read.
    """
    if isinstance(receiver, _HostRef):
        return receiver.file
    return None


def _member_haslink(receiver: Any, args: list[Any]) -> bool:
    """``file.hasLink(target)`` — boolean, bounded to the row outlinks (US-b).

    Pure membership test of the target identity against the row's
    already-resolved outlinks (carried on the row's :class:`_VirtualFile`). No
    filesystem access, no global graph traversal, no recompute (ADR-005 §Axe 2
    invariant). A ``None`` target (e.g. ``this.file`` with no host wired) never
    matches.
    """
    target = _arg(args, 0)
    if not isinstance(receiver, _VirtualFile):
        return False
    return receiver.has_link_to(target)


_MEMBERS = {
    "asFile": _member_asfile,
    "path": _member_path,
    "name": _member_name,
    "basename": _member_basename,
    "startsWith": _member_startswith,
    "endsWith": _member_endswith,
    "contains": _member_contains,
    # US-b (ADR-005 §Axe 2): self-reference member + backlinks-as-boolean.
    "file": _member_file,
    "hasLink": _member_haslink,
}


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def evaluate_formula(
    node: FormulaNode,
    row: dict[str, Any],
    host: dict[str, Any] | None = None,
    now: _dt.datetime | None = None,
) -> Any:
    """Evaluate a formula AST against a dataset row (raises typed BasesError).

    Use this when the caller wants to handle the typed error explicitly. For the
    inert-block contract that never raises, use :func:`safe_evaluate_formula`.

    ``host`` (US-b / ADR-005 §Axe 2): the host-note identity backing ``this``.
    ``None`` when no host is wired → ``this`` resolves to ``None``.

    ``now`` (US-2 / ADR-006 §Gap #4): the reference instant for ``today``/``now``.
    ``None`` → the real wall clock. Tests inject a fixed clock for determinism.

    Raises:
        BasesUnsupportedError: unknown node, function or property-chain member.
        BasesLimitError: a recursion/iteration/time bound was exceeded.
        BasesExecutionError: an operation failed during evaluation.
    """
    return FormulaEvaluator(row, host=host, now=now).eval(node)


def safe_evaluate_formula(
    node: FormulaNode,
    row: dict[str, Any],
    host: dict[str, Any] | None = None,
    now: _dt.datetime | None = None,
) -> tuple[Any, str | None]:
    """Evaluate a formula, mapping any failure to ``(None, error_type)``.

    Pillar (e): this is the inert-block boundary. It NEVER raises — any
    exception (typed BasesError or unexpected) is converted to an ``error_type``
    so the MCP handler renders an inert block and never crashes.

    ``host`` (US-b / ADR-005 §Axe 2): the host-note identity backing ``this``.
    ``now`` (US-2 / ADR-006 §Gap #4): the reference instant for ``today``/``now``.

    Returns:
        ``(value, None)`` on success, ``(None, error_type)`` on failure, where
        ``error_type`` ∈ {parse, unsupported, limit, execution, unexpected}.
    """
    try:
        return FormulaEvaluator(row, host=host, now=now).eval(node), None
    except Exception as exc:  # noqa: BLE001 — inert-block boundary, never crash
        return None, error_type_for(exc)


__all__ = [
    "FormulaEvaluator",
    "evaluate_formula",
    "safe_evaluate_formula",
    "error_type_for",
]
