"""Request-local read timings; never include SQL parameters or research content."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import logging
from time import perf_counter
from functools import wraps

LOGGER = logging.getLogger(__name__)


@dataclass
class QueryTiming:
    started: float = field(default_factory=perf_counter)
    attempts: int = 0
    sql_calls: int = 0
    sql_seconds: float = 0.0
    sections: dict[str, float] = field(default_factory=dict)

    def public(self):
        return {
            'elapsed_ms': round((perf_counter() - self.started) * 1000, 2),
            'attempts': self.attempts,
            'retries': max(0, self.attempts - 1),
            'sql_calls': self.sql_calls,
            'sql_ms': round(self.sql_seconds * 1000, 2),
            'modules_ms': {name: round(value * 1000, 2) for name, value in self.sections.items()},
        }


_current: ContextVar[QueryTiming | None] = ContextVar('public_query_timing', default=None)


@contextmanager
def measure_query(name):
    timing = QueryTiming()
    token = _current.set(timing)
    failed = True
    try:
        yield timing
        failed = False
    finally:
        _current.reset(token)
        LOGGER.info('public_query %s', json.dumps({'query': name, 'failed': failed, **timing.public()}))


def measured_owner_operation(name):
    """Measure an Owner boundary with the existing request-local SQL hooks."""
    def decorate(function):
        @wraps(function)
        def measured(*args, **kwargs):
            with measure_query(name):
                return function(*args, **kwargs)
        return measured
    return decorate


@contextmanager
def query_section(name):
    timing = _current.get()
    began = perf_counter()
    try:
        yield
    finally:
        if timing is not None:
            timing.sections[name] = timing.sections.get(name, 0.0) + perf_counter() - began


def record_attempt():
    timing = _current.get()
    if timing is not None:
        timing.attempts += 1


def before_sql(_connection, _cursor, _statement, _parameters, context, _many):
    if _current.get() is not None:
        context.public_query_started = perf_counter()


def after_sql(_connection, _cursor, _statement, _parameters, context, _many):
    timing = _current.get()
    started = getattr(context, 'public_query_started', None)
    if timing is not None and started is not None:
        timing.sql_calls += 1
        timing.sql_seconds += perf_counter() - started
