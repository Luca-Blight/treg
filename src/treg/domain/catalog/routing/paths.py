"""The adapters' tiny expression language: dotted paths, `coalesce`, `/ N`, predicates, and a few
named transforms. Small on purpose — anything that needs code is a named transform here, not YAML."""

from __future__ import annotations

import re
from typing import Any

_MISSING = object()
_INDEX = re.compile(r"^(\w+)\[(\d+)\]$")


def get_path(doc: Any, path: str) -> Any:
    """`a.b[0].c` → value or None. Missing anywhere → None (never raises)."""
    cur = doc
    for seg in path.split("."):
        if cur is None:
            return None
        if re.fullmatch(r"\[(\d+)\]", seg):  # a root-level list: `[0].followers`
            i = int(seg[1:-1])
            cur = cur[i] if isinstance(cur, list) and i < len(cur) else None
            continue
        m = _INDEX.match(seg)
        if m:
            cur = cur.get(m.group(1)) if isinstance(cur, dict) else None
            i = int(m.group(2))
            cur = cur[i] if isinstance(cur, list) and i < len(cur) else None
        elif isinstance(cur, dict):
            cur = cur.get(seg, None)
        else:
            return None
    return cur


def set_path(doc: dict, path: str, value: Any) -> None:
    """`body.enrichmentType.getWorkEmails` → nested set (creating dicts)."""
    cur = doc
    parts = path.split(".")
    for seg in parts[:-1]:
        nxt = cur.get(seg)
        if not isinstance(nxt, dict):
            nxt = cur[seg] = {}
        cur = nxt
    cur[parts[-1]] = value


# ---- named transforms -----------------------------------------------------------------------

def split_first(name: Any) -> str | None:
    if not isinstance(name, str) or not name.strip():
        return None
    return name.strip().split()[0]


def split_last(name: Any) -> str | None:
    if not isinstance(name, str) or not name.strip():
        return None
    parts = name.strip().split()
    return parts[-1] if len(parts) > 1 else None


def join(*parts: Any) -> str | None:
    words = [str(p).strip() for p in parts if isinstance(p, str) and p.strip()]
    return " ".join(words) if words else None


def has_type(items: Any, kind: Any) -> bool:
    return isinstance(items, list) and any(isinstance(i, dict) and i.get("type") == kind for i in items)


def length(items: Any) -> int | None:
    return len(items) if isinstance(items, (list, dict, str)) else None


TRANSFORMS = {"split_first": split_first, "split_last": split_last, "join": join, "has_type": has_type, "len": length}

_CALL = re.compile(r"^(\w+)\((.*)\)$")
_DIV = re.compile(r"^(.+?)\s*/\s*(\d+(?:\.\d+)?)$")
_CMP = re.compile(r"^(.+?)\s*(==|!=)\s*(.+)$")


def _literal(tok: str):
    t = tok.strip()
    if t == "null":
        return None
    if t == "[]":
        return []
    if t in ("true", "false"):
        return t == "true"
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "'\"":
        return t[1:-1]
    try:
        return float(t) if "." in t else int(t)
    except ValueError:
        return _MISSING


def evaluate(expr: str, doc: Any) -> Any:
    """Evaluate one adapter expression against `doc` (the provider body, or the contract input)."""
    e = expr.strip()
    m = _CMP.match(e)
    if m and not _CALL.match(e):
        left, op, right = m.groups()
        lv = evaluate(left, doc)
        rv = _literal(right)
        if rv is _MISSING:
            rv = evaluate(right, doc)
        return (lv == rv) if op == "==" else (lv != rv)
    m = _DIV.match(e)
    if m and not _CALL.match(e):
        v = evaluate(m.group(1), doc)
        return (float(v) / float(m.group(2))) if isinstance(v, (int, float)) else None
    m = _CALL.match(e)
    if m:
        name, args = m.group(1), _split_args(m.group(2))
        vals = [evaluate(a, doc) for a in args]
        if name == "coalesce":
            return next((v for v in vals if v not in (None, "", [])), None)
        fn = TRANSFORMS.get(name)
        if fn is None:
            raise ValueError(f"unknown transform {name!r}")
        return fn(*vals)
    lit = _literal(e)
    if lit is not _MISSING and not re.match(r"^[A-Za-z_]", e):
        return lit
    return get_path(doc, e)


def _split_args(s: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return [a.strip() for a in out]
