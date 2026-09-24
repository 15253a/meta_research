"""Pure validation for a bounded, explicitly partial research-history cut."""
from meta_research.owners.common import OwnerConflict


def validate_history_page(binding):
    page = binding.get('history_page')
    fields = {'schema_ref','limit','summary_only','active_total','active_shown',
        'active_next_offset','prior_total','prior_shown','prior_next_offset',
        'parent_shown','parent_has_more','parent_next_question_ref'}
    if not isinstance(page,dict) or set(page)!=fields or page['schema_ref']!='meta-research/research-history-page/v1' or page['limit']!=12 or page['summary_only'] is not True:
        raise OwnerConflict('reasoning_research_context_invalid')
    for key,field in [('active','active_question_refs'),('prior','prior_current_question_outcomes')]:
        total,shown=page[key+'_total'],page[key+'_shown']
        if type(total)is not int or type(shown)is not int or not 0<=shown<=min(total,12) or shown!=len(binding[field]) or page[key+'_next_offset']!=(shown if shown<total else None):
            raise OwnerConflict('reasoning_research_context_invalid')
    if page['parent_shown']!=len(binding['parent_question_bindings']) or not 0<=page['parent_shown']<=12 or type(page['parent_has_more'])is not bool or page['parent_has_more'] != (page['parent_next_question_ref'] is not None):
        raise OwnerConflict('reasoning_research_context_invalid')
