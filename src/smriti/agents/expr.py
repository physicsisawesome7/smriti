"""A small, safe expression language for workflows.

Used by Set state and Condition nodes, and inside ``{{ }}`` templates. It is a
whitelisted subset of Python expression syntax parsed with ``ast``: names read
workflow variables, and only literals, arithmetic, comparisons, boolean logic,
``in``, indexing, attribute-free calls to the functions in FUNCS and
conditional expressions are allowed. There is no ``eval``, no attribute
access and no way to reach builtins.

    len(answer) > 0 and "approved" in decision.lower()   # rejected: attribute
    len(answer) > 0 and contains(lower(decision), "approved")   # fine
"""

from __future__ import annotations

import ast
import json
import operator
import re
import threading
from typing import Any


class ExprError(ValueError):
    pass


MAX_LEN = 2000
MAX_POW = 64

_BIN = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
_CMP = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}


def _num(x: Any) -> float:
    try:
        return float(str(x).replace(",", ""))
    except ValueError as e:
        raise ExprError(f"not a number: {x!r}") from e


FUNCS: dict[str, Any] = {
    "len": len,
    "str": str,
    "int": lambda x: int(_num(x)),
    "float": _num,
    "round": lambda x, n=0: round(_num(x), int(n)),
    "abs": lambda x: abs(_num(x)),
    "min": min,
    "max": max,
    "lower": lambda s: str(s).lower(),
    "upper": lambda s: str(s).upper(),
    "strip": lambda s: str(s).strip(),
    "contains": lambda s, sub: str(sub).lower() in str(s).lower(),
    "startswith": lambda s, p: str(s).startswith(str(p)),
    "split": lambda s, sep=None: str(s).split(sep),
    "join": lambda items, sep=", ": str(sep).join(str(i) for i in items),
    "matches": lambda s, pattern: _safe_search(str(pattern), str(s)),
    "number": lambda s: _first_number(s),
    "json": lambda s: json.loads(s) if isinstance(s, str) else s,
    "default": lambda x, d: d if x in (None, "", [], {}) else x,
    "pluck": lambda items, key: [i.get(key) if isinstance(i, dict) else None for i in items],
    "keys": lambda d: list(d.keys()) if isinstance(d, dict) else [],
    "bool": bool,
}


def _first_number(s: Any) -> float | None:
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(s))
    return _num(m.group(0)) if m else None


def evaluate(src: str, variables: dict[str, Any]) -> Any:
    src = (src or "").strip()
    if not src:
        raise ExprError("empty expression")
    if len(src) > MAX_LEN:
        raise ExprError("expression is too long")
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError as e:
        raise ExprError(f"invalid expression: {e.msg}") from e
    _BUDGET.left = MAX_STEPS
    out = _eval(tree.body, variables)
    if isinstance(out, (str, list, dict)) and len(out) > MAX_RESULT:
        raise ExprError("expression result is too large")
    return out


MAX_STEPS = 100_000  # evaluation steps per expression: nesting and comprehensions cannot run away
MAX_RESULT = 1_000_000
_BUDGET = threading.local()
# a pattern that nests quantifiers ("(a+)+", "(\\w*)*") can take exponential time
_NESTED_QUANT = re.compile(r"\([^)]*[+*][^)]*\)\s*[+*{]")


def _safe_search(pattern: str, s: str) -> bool:
    pattern = pattern[:200]
    if _NESTED_QUANT.search(pattern):
        raise ExprError("pattern has nested repetition; simplify it")
    try:
        return re.search(pattern, s[:20000]) is not None
    except re.error as e:
        raise ExprError(f"invalid pattern: {e}") from e


def _eval(n: ast.AST, v: dict[str, Any]) -> Any:
    left = getattr(_BUDGET, "left", MAX_STEPS) - 1
    _BUDGET.left = left
    if left < 0:
        raise ExprError("expression is too complex")
    if isinstance(n, ast.Constant) and isinstance(n.value, (str, int, float, bool, type(None))):
        return n.value
    if isinstance(n, ast.Name):
        if n.id in ("true", "True"):
            return True
        if n.id in ("false", "False"):
            return False
        if n.id in ("null", "None"):
            return None
        return v.get(n.id)
    if isinstance(n, ast.List | ast.Tuple):
        return [_eval(e, v) for e in n.elts]
    if isinstance(n, ast.Dict):
        return {_eval(k, v): _eval(x, v) for k, x in zip(n.keys, n.values, strict=True) if k is not None}
    if isinstance(n, ast.BoolOp):
        if isinstance(n.op, ast.And):
            out: Any = True
            for e in n.values:
                out = _eval(e, v)
                if not out:
                    return out
            return out
        out = False
        for e in n.values:
            out = _eval(e, v)
            if out:
                return out
        return out
    if isinstance(n, ast.UnaryOp):
        x = _eval(n.operand, v)
        if isinstance(n.op, ast.Not):
            return not x
        if isinstance(n.op, ast.USub):
            return -_num(x)
        if isinstance(n.op, ast.UAdd):
            return _num(x)
    if isinstance(n, ast.BinOp):
        a, b = _eval(n.left, v), _eval(n.right, v)
        if isinstance(n.op, ast.Pow):
            if _num(b) > MAX_POW:
                raise ExprError("exponent too large")
            return _num(a) ** _num(b)
        op = _BIN.get(type(n.op))
        if op is None:
            raise ExprError("operator not allowed")
        if isinstance(n.op, ast.Add) and (isinstance(a, str) or isinstance(b, str)):
            out = str(a) + str(b)
        elif isinstance(n.op, ast.Add) and isinstance(a, list) and isinstance(b, list):
            out = a + b
        else:
            try:
                out = op(_num(a), _num(b))
            except ZeroDivisionError as e:
                raise ExprError("division by zero") from e
        if isinstance(out, str | list) and len(out) > 100_000:
            raise ExprError("result too large")
        return out
    if isinstance(n, ast.Compare):
        left = _eval(n.left, v)
        for op, right_n in zip(n.ops, n.comparators, strict=True):
            right = _eval(right_n, v)
            fn = _CMP.get(type(op))
            if fn is None:
                raise ExprError("comparison not allowed")
            try:
                ok = fn(left, right)
            except TypeError:
                ok = fn(_num(left), _num(right))
            if not ok:
                return False
            left = right
        return True
    if isinstance(n, ast.ListComp) and len(n.generators) == 1:
        g = n.generators[0]
        if not isinstance(g.target, ast.Name) or g.is_async:
            raise ExprError("only simple 'for x in items' comprehensions are allowed")
        items = _eval(g.iter, v)
        if not isinstance(items, list | str | dict):
            raise ExprError("can only loop over a list")
        out_list = []
        for i, item in enumerate(items):
            if i >= 1000:
                raise ExprError("list too long")
            inner = {**v, g.target.id: item}
            if all(_eval(c, inner) for c in g.ifs):
                out_list.append(_eval(n.elt, inner))
        return out_list
    if isinstance(n, ast.IfExp):
        return _eval(n.body, v) if _eval(n.test, v) else _eval(n.orelse, v)
    if isinstance(n, ast.Subscript):
        obj = _eval(n.value, v)
        key = _eval(n.slice, v)
        try:
            return obj[int(key) if isinstance(obj, list | str) else key]
        except (KeyError, IndexError, TypeError, ValueError):
            return None
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and not n.keywords:
        fn = FUNCS.get(n.func.id)
        if fn is None:
            raise ExprError(f"unknown function '{n.func.id}'")
        try:
            return fn(*[_eval(a, v) for a in n.args])
        except ExprError:
            raise
        except Exception as e:
            raise ExprError(f"{n.func.id}(): {e}") from e
    raise ExprError(f"'{type(n).__name__}' is not allowed in expressions")


_TPL = re.compile(r"\{\{\s*(.+?)\s*\}\}", re.S)


def render(template: str, variables: dict[str, Any]) -> str:
    """Fills ``{{ expression }}`` placeholders. Lists and dicts render as JSON."""

    def one(m: re.Match[str]) -> str:
        val = evaluate(m.group(1), variables)
        if val is None:
            return ""
        if isinstance(val, list | dict):
            return json.dumps(val, ensure_ascii=False)
        if isinstance(val, float) and val.is_integer():
            return str(int(val))
        return str(val)

    return _TPL.sub(one, template or "")


def names(src: str) -> set[str]:
    """Variable names an expression reads (for validation hints)."""
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError:
        return set()
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id not in FUNCS}
