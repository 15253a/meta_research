"""Bind discovery continuations to the query and its existing Owner facts.

No stored cursor or historical cache: callers read revision identities and the
page in one Owner snapshot. Unrelated scopes do not invalidate continuation.
"""
from meta_research.owners.common import OwnerConflict, canonical_hash


def bind_page(scope, revision, *, offset=0, query_revision=None):
    current = canonical_hash({'scope': scope, 'owner_revision': revision})
    if offset and query_revision is None:
        raise OwnerConflict('research_query_revision_required', {
            'path': ['query_revision'], 'message': 'A continuation needs the first page query_revision.',
            'correction': 'Keep that query_revision, filters and limit; otherwise restart at offset=0.'})
    if query_revision is not None and query_revision != current:
        raise OwnerConflict('research_query_changed', {
            'path': ['offset'], 'message': 'The query or source revision no longer matches.',
            'correction': 'Restart at offset=0 without query_revision; use the new page revision for continuation.'})
    return current


def page_state(items, next_offset, *, offset=0, query=''):
    # A filtered scan can have an empty intermediate page. A later empty page
    # cannot establish that earlier pages contained no matching research.
    state = ('matches' if items else 'page_pending' if next_offset is not None
             else 'exhausted' if offset else 'no_match' if query else 'no_records')
    return {'result_state': state, 'scan_complete': next_offset is None}
