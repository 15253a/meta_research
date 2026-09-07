"""Reuse selected verified projections only within one immutable SQLite read cut."""
from copy import deepcopy
from functools import wraps


def snapshot_cached(query):
    @wraps(query)
    def read(owner, *args, **kwargs):
        context = getattr(owner._database, '_read_cache', None)
        cache = context.get() if context is not None else None
        if cache is None:
            return query(owner, *args, **kwargs)
        key = (owner, query, args, tuple(sorted(kwargs.items())))
        if key not in cache:
            # Never retain failures or let callers mutate the cached proof.
            cache[key] = deepcopy(query(owner, *args, **kwargs))
        return deepcopy(cache[key])
    return read
