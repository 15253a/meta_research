"""Bounded incremental observations over the existing raw experiment streams."""
from __future__ import annotations

import base64
import json
import re

from meta_research.experiment_logs import ExperimentLogError, TargetExperimentLogs
from meta_research.owners.common import canonical_hash

_MAX_CURSOR = 32768
_MAX_BYTES = 16384
_MAX_PAGES = 8
_ANOMALY = re.compile(r'traceback|\berror\b|\bfailed\b|\bwarning\b|\bnan\b|out of memory|异常|失败', re.I)


def _decode(cursor, target_ref, run_ref):
    if cursor is None:
        return {'target_ref': target_ref, 'target_run_ref': run_ref, 'streams': {}, 'state': None, 'next_log': None}
    try:
        if not isinstance(cursor, str) or len(cursor) > _MAX_CURSOR:
            raise ValueError()
        value = json.loads(base64.b64decode(cursor.encode('ascii'), altchars=b'-_', validate=True))
        if (not isinstance(value, dict) or set(value) != {'target_ref', 'target_run_ref', 'streams', 'state', 'next_log'}
                or not isinstance(value['streams'], dict) or len(value['streams']) > 64
                or not isinstance(value['state'], str)
                or (value['next_log'] is not None and (not isinstance(value['next_log'], str) or len(value['next_log']) > 128))):
            raise ValueError()
        for key, item in value['streams'].items():
            if (not isinstance(key, str) or len(key) > 128 or not isinstance(item, list) or len(item) != 2
                    or not isinstance(item[0], str) or len(item[0]) > 128
                    or type(item[1]) is not int or item[1] < 0):
                raise ValueError()
    except (ValueError, TypeError, UnicodeError) as error:
        raise ExperimentLogError('experiment_log_cursor_invalid', 422) from error
    if (value['target_ref'], value['target_run_ref']) != (target_ref, run_ref):
        raise ExperimentLogError('experiment_log_reset_required')
    return value


def read_target_progress(logs: TargetExperimentLogs, target_ref: str, *, target_run_ref: str,
                         cursor: str | None = None) -> dict:
    """Return only new fragments; retaining a cursor never deletes raw records."""
    prior = _decode(cursor, target_ref, target_run_ref)
    listed = logs.list(target_ref, target_run_ref=target_run_ref)
    state = canonical_hash({key: listed[key] for key in ('workspace_ref', 'target_status', 'status', 'reason')})
    streams = dict(prior['streams'])
    fragments, notices = [], []
    available = {item['log_ref'] for item in listed['logs']}
    # Discovery can be incomplete: omission in a bounded scan is not deletion.
    if not listed['truncated'] and listed['status'] != 'unavailable':
        for ref in set(streams) - available:
            notices.append({'log_ref': ref, 'kind': 'no_longer_discoverable'})
            del streams[ref]
    remaining, has_more, next_log = _MAX_BYTES, False, None
    ordered = listed['logs']
    if prior['next_log'] in available:
        start = next(index for index, item in enumerate(ordered) if item['log_ref'] == prior['next_log'])
        ordered = ordered[start:] + ordered[:start]
    for item in ordered:
        ref = item['log_ref']
        previous = streams.get(ref)
        reset = previous is not None and previous[0] != item['stream_ref']
        if previous is not None and not reset and previous[1] == item['source_bytes']:
            continue
        if remaining < 12 or len(fragments) >= _MAX_PAGES:
            has_more = True
            next_log = next_log or ref
            continue
        if reset:
            notices.append({'log_ref': ref, 'kind': 'stream_replaced'})
        try:
            page = logs.read(target_ref, ref, target_run_ref=target_run_ref,
                             after=None if previous is None or reset else previous[1],
                             stream_ref=item['stream_ref'], limit=min(2048, remaining // 3))
        except ExperimentLogError as error:
            if error.code not in {'experiment_log_reset_required', 'experiment_log_not_found'}:
                raise
            # Leave the old position intact and rediscover the generation next call.
            notices.append({'log_ref': ref, 'kind': error.code})
            has_more = True
            continue
        if ref not in streams and len(streams) >= 64:
            evicted = next((key for key in streams if key not in available), next(iter(streams)))
            generation, offset = streams.pop(evicted)
            notices.append({'log_ref': evicted, 'kind': 'cursor_tracking_evicted',
                            'stream_ref': generation, 'next_offset': offset})
        streams[ref] = [page['stream_ref'], page['next_offset']]
        fragment = {key: page[key] for key in ('log_ref', 'relative_path', 'kind', 'stream_ref', 'offset',
                    'next_offset', 'source_bytes', 'text', 'has_more', 'pending_utf8_bytes')}
        fragment['anomalies'] = [line[:512] for line in page['text'].splitlines() if _ANOMALY.search(line)][:8]
        fragment['initial_tail'] = previous is None or reset
        fragments.append(fragment)
        remaining -= len(page['text'].encode('utf-8'))
        has_more |= page['has_more']
    value = {'target_ref': target_ref, 'target_run_ref': target_run_ref, 'streams': streams, 'state': state, 'next_log': next_log}
    encoded = base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':')).encode()).decode()
    return {'schema_ref': 'meta-research/target-progress/v1', 'target_ref': target_ref,
            'target_run_ref': target_run_ref, 'target_status': listed['target_status'],
            'availability': listed['status'], 'reason': listed['reason'],
            'state_changed': state != prior['state'], 'fragments': fragments, 'notices': notices,
            'cursor': encoded, 'has_more': has_more, 'discovery_truncated': listed['truncated'],
            'suggested_poll_seconds': 2 if has_more else 15 if fragments or state != prior['state'] else 30,
            'raw_reader': {'operation': 'agent_runtime.target_run.progress', 'mode': 'raw',
                           'target_ref': target_ref, 'required': ['log_ref', 'stream_ref', 'before']}}
