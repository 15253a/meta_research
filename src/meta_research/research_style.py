"""Persistent research preferences, independent of conditions and evidence."""

from meta_research.owners.common import OwnerConflict


RESEARCH_STYLES = {
    'focus': '聚焦攻关：集中推进当前研究目标的关键验证，探索以帮助攻关为主。',
    'balanced': '均衡探索：优先推进当前研究目标，同时适度探索有价值的新线索。',
    'open': '开放探索：主动探索新方向、替代路线和偶然发现，联系已有研究积累，并根据证据调整研究关注。',
}


def validate_research_style(value: str) -> str:
    if not isinstance(value, str) or value not in RESEARCH_STYLES:
        raise OwnerConflict('research_style_invalid')
    return value


def render_research_style(value: str) -> str:
    return (RESEARCH_STYLES[validate_research_style(value)]
        + '这是持续研究倾向，不是单条指导的力度或科学结论；不取消或覆盖人的持续条件。')
