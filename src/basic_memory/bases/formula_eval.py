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

import time
from collections.abc import Callable
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
    FThis,
)
from basic_memory.bases.schema import (
    FORMULA_BUDGET_MS,
    MAX_FORMULA_ITERATIONS,
    MAX_FORMULA_RECURSION,
    MAX_FORMULA_RESULT_SIZE,
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
        # The host file's identity strings: both its path and permalink so a row
        # that links by either form matches in hasLink.
        identities = [v for v in (path, permalink) if v]
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

    def __init__(self, row: dict[str, Any], host: dict[str, Any] | None = None):
        self.row = row
        # US-b (ADR-005 §Axe 2): the host-note identity backing ``this``. A plain
        # dict ({"path": ..., "permalink": ..., "title": ...}) injected from the
        # caller via note_metadata. ``None`` when no host is wired → ``this``
        # resolves to None and the block goes inert. NEVER note content, never a
        # filesystem handle — only the identity strings.
        self._host = host
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
        # US-b: the bare ``file`` reference (receiver of ``file.hasLink(...)``)
        # resolves to a _VirtualFile of the row carrying the row's outlinks — read
        # straight from the dataset row, never the OS, never a graph recompute.
        if name == "file":
            return self._row_file()
        # Executor-projected rows carry file.* as a flat top-level key
        # (e.g. row["file.path"]); honour it first so asFile().path reads the
        # dataset value directly (US-003 spec). Then fall back to FieldResolver
        # for nested-shape datasets. Either way: dataset only, never the OS.
        if name in self.row:
            return self.row[name]
        return FieldResolver.resolve_field(self.row, name)

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
    # --- Date (string passthrough in the sandbox; no OS clock access) ---
    "dateformat": lambda a: _to_str(_arg(a, 0)),
    "date": lambda a: _to_str(_arg(a, 0)),
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
    base = receiver.path if isinstance(receiver, _VirtualFile) else _to_str(receiver)
    return _to_str(_arg(args, 0)) in base


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
    node: FormulaNode, row: dict[str, Any], host: dict[str, Any] | None = None
) -> Any:
    """Evaluate a formula AST against a dataset row (raises typed BasesError).

    Use this when the caller wants to handle the typed error explicitly. For the
    inert-block contract that never raises, use :func:`safe_evaluate_formula`.

    ``host`` (US-b / ADR-005 §Axe 2): the host-note identity backing ``this``.
    ``None`` when no host is wired → ``this`` resolves to ``None``.

    Raises:
        BasesUnsupportedError: unknown node, function or property-chain member.
        BasesLimitError: a recursion/iteration/time bound was exceeded.
        BasesExecutionError: an operation failed during evaluation.
    """
    return FormulaEvaluator(row, host=host).eval(node)


def safe_evaluate_formula(
    node: FormulaNode, row: dict[str, Any], host: dict[str, Any] | None = None
) -> tuple[Any, str | None]:
    """Evaluate a formula, mapping any failure to ``(None, error_type)``.

    Pillar (e): this is the inert-block boundary. It NEVER raises — any
    exception (typed BasesError or unexpected) is converted to an ``error_type``
    so the MCP handler renders an inert block and never crashes.

    ``host`` (US-b / ADR-005 §Axe 2): the host-note identity backing ``this``.

    Returns:
        ``(value, None)`` on success, ``(None, error_type)`` on failure, where
        ``error_type`` ∈ {parse, unsupported, limit, execution, unexpected}.
    """
    try:
        return FormulaEvaluator(row, host=host).eval(node), None
    except Exception as exc:  # noqa: BLE001 — inert-block boundary, never crash
        return None, error_type_for(exc)


__all__ = [
    "FormulaEvaluator",
    "evaluate_formula",
    "safe_evaluate_formula",
    "error_type_for",
]
