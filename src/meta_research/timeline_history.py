"""Read-only discovery of existing accepted Quest identities for history browsing."""

from sqlalchemy import text

from meta_research.owners.common import OwnerConflict


def discover_timeline_quests(graph, *, offset: int, limit: int):
    with graph._database.read_snapshot() as connection:
        rows = connection.execute(text(
            'SELECT quest_ref FROM rg_quests ORDER BY accepted_at, quest_ref LIMIT :limit OFFSET :offset'
        ), {'limit': limit + 1, 'offset': offset}).all()
        items = []
        for row in rows[:limit]:
            quest = graph.query_quest_by_ref(row.quest_ref)
            if quest is None:
                raise OwnerConflict('timeline_quest_identity_unavailable')
            current = graph.query_current_quest_goal_revision(quest.quest_ref)
            goal = quest.draft if current is None else current['goal']
            items.append({'quest_ref': quest.quest_ref, 'goal': str(goal.get('goal', ''))})
    return {'schema_ref': 'meta-research/timeline-quests/v1', 'items': items,
        'offset': offset, 'next_offset': offset + limit if len(rows) > limit else None}
