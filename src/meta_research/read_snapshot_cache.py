"""Reuse selected verified projections only within one immutable SQLite read cut."""
from copy import deepcopy
from contextvars import ContextVar
from functools import wraps

_file_checks = ContextVar('snapshot_file_checks', default=None)


def snapshot_file_check(check):
    """Keep a successful file check live when its containing proof is reused."""
    @wraps(check)
    def verify(*args, **kwargs):
        result = check(*args, **kwargs)
        checks = _file_checks.get()
        if checks is not None:
            key = (check, args, tuple(sorted(kwargs.items())))
            try:
                hash(key)
            except TypeError:
                # Unhashable check inputs still need replay, without deduplication.
                key = object()
            checks[key] = (check, deepcopy(args), deepcopy(kwargs))
        return result
    return verify


def snapshot_cached(query):
    @wraps(query)
    def read(owner, *args, **kwargs):
        context = getattr(owner._database, '_read_cache', None)
        cache = context.get() if context is not None else None
        if cache is None:
            return query(owner, *args, **kwargs)
        key = (owner, query, args, tuple(sorted(kwargs.items())))
        try:
            hash(key)
        except TypeError:
            # Preserve the original query behavior for unhashable inputs.
            return query(owner, *args, **kwargs)
        if key not in cache:
            # Never retain failures or let callers mutate the cached proof.
            checks = {}
            token = _file_checks.set(checks)
            try:
                value = query(owner, *args, **kwargs)
            finally:
                _file_checks.reset(token)
            cache[key] = (deepcopy(value), checks)
        else:
            # SQLite freezes rows, not managed files. Replay only this proof's
            # transitive file custody checks, without rebuilding its SQL graph.
            for check, check_args, check_kwargs in cache[key][1].values():
                check(*check_args, **check_kwargs)
        value, checks = cache[key]
        parent_checks = _file_checks.get()
        if parent_checks is not None:
            parent_checks.update(checks)
        return deepcopy(value)
    return read
