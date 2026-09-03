"""Dependency-free (de)serialization for the harness dataclasses.

Everything the harness persists is a plain dataclass tree.  ``to_jsonable`` turns
one into JSON-safe primitives; ``from_jsonable`` rebuilds it using the annotated
types, so the on-disk event log stays inspectable text while the in-memory model
stays typed.
"""

from __future__ import annotations

import contextlib
import dataclasses
import enum
import types
import typing
from typing import Any, TypeVar, Union, cast, get_args, get_origin, get_type_hints

T = TypeVar("T")

_NONE = type(None)


def to_dict(value: Any) -> dict[str, Any]:
    """:func:`to_jsonable` for something known to be a dataclass or a mapping.

    `to_jsonable` returns `Any` because it genuinely can return anything -- a
    list, a string, a number. Every caller that hands it a dataclass gets a
    dict, and this says so once rather than each of them casting.
    """
    return cast("dict[str, Any]", to_jsonable(value))


def to_jsonable(value: Any) -> Any:
    """Recursively convert dataclasses, enums and containers to JSON primitives."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    return value


def _is_optional(tp: Any) -> bool:
    return get_origin(tp) in (Union, types.UnionType) and _NONE in get_args(tp)


def _strip_optional(tp: Any) -> Any:
    args = [a for a in get_args(tp) if a is not _NONE]
    return args[0] if len(args) == 1 else tp


def _coerce(value: Any, tp: Any) -> Any:
    if tp is Any or tp is None:
        return value

    if _is_optional(tp):
        if value is None:
            return None
        tp = _strip_optional(tp)

    origin = get_origin(tp)

    if origin in (list, set, tuple):
        (inner,) = get_args(tp) or (Any,)
        items = [_coerce(v, inner) for v in (value or [])]
        return set(items) if origin is set else items

    if origin is dict:
        args = get_args(tp) or (str, Any)
        kt, vt = args[0], args[1]
        return {_coerce(k, kt): _coerce(v, vt) for k, v in (value or {}).items()}

    if origin in (Union, types.UnionType):
        # Best effort: try each member, first success wins.
        for candidate in get_args(tp):
            if candidate is _NONE:
                continue
            # Union probing is inherently lossy: a candidate that does not fit
            # is the normal case, and falling out of the `with` tries the next.
            with contextlib.suppress(Exception):
                return _coerce(value, candidate)
        return value

    if isinstance(tp, type) and issubclass(tp, enum.Enum):
        return tp(value)

    # `is_dataclass` is true of instances as well as classes; `from_jsonable`
    # takes a class, so the isinstance check is narrowing rather than noise.
    if isinstance(tp, type) and dataclasses.is_dataclass(tp) and isinstance(value, dict):
        return from_jsonable(value, tp)

    return value


def from_jsonable(data: dict[str, Any], cls: type[T]) -> T:
    """Rebuild ``cls`` from a plain dict, ignoring unknown keys."""
    if not dataclasses.is_dataclass(cls):
        raise TypeError(f"{cls!r} is not a dataclass")
    hints = get_type_hints(cls, include_extras=False)
    kwargs: dict[str, Any] = {}
    for field in dataclasses.fields(cls):
        if field.name not in data:
            continue
        kwargs[field.name] = _coerce(data[field.name], hints.get(field.name, Any))
    return typing.cast(T, cls(**kwargs))
