"""Formula parser + evaluator for dashboard calculation columns.

A calculation column on a table chart stores a bare-name arithmetic
formula like ``conversions / reach`` or ``(a + b) / 2``. This module:

1. Parses the formula via :func:`ast.parse` (mode=``"eval"``) and walks
   the resulting AST to validate that only a small whitelist of node
   types appears. Anything else — function calls, attribute access,
   subscripts, comparisons, comprehensions, names like ``__import__`` —
   is rejected at parse time. This is the authoritative gate; the
   browser-side parser in ``builder.js`` is a UX nicety only.

2. Evaluates a parsed AST against a ``{field_name: float | int}`` map.
   Division by zero returns ``None`` (the renderer maps this to
   ``"—"``). Any other arithmetic error also returns ``None`` so a bad
   row never crashes the response.

Why ``ast.parse`` and not a hand-rolled recursive-descent parser:

* ``ast.parse`` is a battle-tested CPython component. The hand-rolled
  alternative would need its own tokenizer, parser, and node tree —
  ~100 lines easy to get wrong.
* The whitelist walk is the security boundary, not the parser. So long
  as we reject every node type we don't explicitly allow, we get the
  same safety as the JS parser without re-deriving its grammar.

Field names with dots (``meta.baptism_count``) are supported by
parsing them as :class:`ast.Attribute` nodes and reassembling the
dotted path during the walk.
"""

from __future__ import annotations

import ast
from typing import Any

from lib.user_errors import UserFacingError


class FormulaError(UserFacingError):
    """Raised when a formula fails parse-time validation."""


# Node types the formula walker accepts. Anything else is rejected.
_ALLOWED_NODE_TYPES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.USub,
    ast.UAdd,
    ast.Constant,
    ast.Name,
    # Allow Attribute so dotted field names like meta.baptism_count
    # parse as a single identifier — the walker reassembles them.
    ast.Attribute,
    ast.Load,
)


def _flatten_attribute_chain(node: ast.AST) -> str | None:
    """Reduce ``ast.Attribute`` chains to a dotted name string, or
    ``None`` if the chain isn't a pure ``Name (.Attribute)*`` shape."""
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        return None
    parts.append(cur.id)
    return ".".join(reversed(parts))


def _walk_and_collect_fields(node: ast.AST, fields: list[str], seen: set[str]) -> None:
    """Recursive validator + field collector.

    Raises :class:`FormulaError` on the first disallowed node type.
    Adds unique field names (dotted paths preserved) to ``fields`` in
    document order.
    """
    if not isinstance(node, _ALLOWED_NODE_TYPES):
        raise FormulaError(f"Disallowed expression element: {type(node).__name__}")

    if isinstance(node, ast.Constant):
        # Only numeric constants are useful here; reject strings / None
        # / True / False so a future evaluator extension can't be
        # surprised by truthy short-circuiting.
        if not isinstance(node.value, (int, float)) or isinstance(node.value, bool):
            raise FormulaError("Only numeric constants are allowed in formulas.")
        return

    if isinstance(node, ast.Name):
        name = node.id
        if name not in seen:
            seen.add(name)
            fields.append(name)
        return

    if isinstance(node, ast.Attribute):
        dotted = _flatten_attribute_chain(node)
        if dotted is None:
            raise FormulaError("Attribute access on a non-name is not allowed.")
        if dotted not in seen:
            seen.add(dotted)
            fields.append(dotted)
        return

    # BinOp / UnaryOp / Expression — recurse on every child node.
    for child in ast.iter_child_nodes(node):
        _walk_and_collect_fields(child, fields, seen)


def parse_formula(formula: str) -> dict[str, Any]:
    """Parse ``formula`` and return its referenced field list.

    Returns:
        ``{"ok": True, "fields": [...], "tree": <ast.Expression>}`` on
        success.
        ``{"ok": False, "error": "<message>"}`` when the formula is
        empty, syntactically invalid, or contains a disallowed node.
    """
    if not isinstance(formula, str) or not formula.strip():
        return {"ok": False, "error": "Formula is empty."}
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as e:
        return {"ok": False, "error": f"Syntax error: {e.msg}"}

    fields: list[str] = []
    seen: set[str] = set()
    try:
        _walk_and_collect_fields(tree, fields, seen)
    except FormulaError as e:
        return {"ok": False, "error": e.public_message}
    return {"ok": True, "fields": fields, "tree": tree}


def _eval_node(node: ast.AST, values: dict[str, float]) -> float | None:
    """Evaluate a validated AST node. Returns ``None`` when arithmetic
    fails (div-by-zero, missing field, non-numeric value)."""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, values)
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.Name):
        v = values.get(node.id)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    if isinstance(node, ast.Attribute):
        dotted = _flatten_attribute_chain(node)
        if dotted is None:
            return None
        v = values.get(dotted)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand, values)
        if operand is None:
            return None
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return operand
        return None
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, values)
        right = _eval_node(node.right, values)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            if right == 0:
                return None  # div-by-zero → renderer shows "—"
            return left / right
        return None
    return None


def evaluate(tree: ast.Expression, values: dict[str, float]) -> float | None:
    """Evaluate a tree returned by :func:`parse_formula` against
    ``values``. Returns ``None`` for any arithmetic failure
    (div-by-zero, missing field, non-numeric value)."""
    if tree is None:
        return None
    try:
        return _eval_node(tree, values)
    except (TypeError, ValueError):
        # Non-numeric values reach BinOp / UnaryOp and fail with
        # TypeError or ValueError — return None so the renderer shows
        # ``—`` rather than crashing the response. ZeroDivisionError
        # is intentionally NOT caught here: the inner BinOp branch
        # short-circuits before division so a propagated
        # ZeroDivisionError indicates the inner guard was removed —
        # that's a real bug worth surfacing in tests.
        return None
