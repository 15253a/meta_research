"""Reuse exact resource bytes only within one synchronous preparation.

Each public preparation starts fresh. This never caches a runtime binding or
infers content identity from filesystem metadata.
"""
from contextvars import ContextVar
from functools import wraps
from typing import Callable, ParamSpec, TypeVar

_P = ParamSpec("_P")
_T = TypeVar("_T")

_prepared = ContextVar("research_resource_preparation", default=None)


def resource_preparation(function: Callable[_P, _T]) -> Callable[_P, _T]:
    @wraps(function)
    def prepare(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        if _prepared.get() is not None:
            return function(*args, **kwargs)
        token = _prepared.set({})
        try:
            return function(*args, **kwargs)
        finally:
            _prepared.reset(token)
    return prepare


def prepared_resource(function: Callable[_P, _T]) -> Callable[_P, _T]:
    @wraps(function)
    def read(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        prepared = _prepared.get()
        if prepared is None:
            return function(*args, **kwargs)
        key = (function, args, tuple(sorted(kwargs.items())))
        if key not in prepared:
            prepared[key] = function(*args, **kwargs)
        return prepared[key]
    return read
