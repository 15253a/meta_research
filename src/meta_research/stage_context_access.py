"""Authenticated, paged reading of a stage's frozen inputs and scoped history."""
from meta_research.context_presentation import context_read_page, literature_reference, reasoning_handoff_reference
from meta_research.owners.common import OwnerConflict
from meta_research.semantic_mcp import SemanticMcpError, SemanticOperation
from meta_research.target_research_note_access import read_target_research_notes

STAGE_CONTEXT_OPERATION_ID = 'research_memory.stage_context.read'


def _predecessor(advancement_engine, request, source_ref):
    """Only the exact accepted predecessor frozen for this successor may cross Questions."""
    frozen = request.context_pack.get('prior_accepted_bindings', [])
    matches = [item for item in frozen if isinstance(item, dict) and source_ref in
               {item.get('outcome_ref'), item.get('commit_ref'),
                (item.get('outcome_receipt') or {}).get('subject_ref')}]
    if not matches:
        return None
    successor = advancement_engine.query_reasoning_successor_context(request.cycle_ref)
    if not isinstance(successor, dict):
        raise SemanticMcpError('stage_predecessor_source_unbound')
    for exact in successor.get('prior_accepted_bindings', []):
        expected = reasoning_handoff_reference(exact) if matches[0].get('kind') == 'ReasoningHandoffReference' else exact
        if matches[0] == expected:
            transition = exact.get('closure', {}).get('transition', {})
            if transition.get('source_quest_ref') != request.accepted_question.quest_ref:
                raise SemanticMcpError('stage_predecessor_source_unbound')
            return exact
    raise SemanticMcpError('stage_predecessor_source_unbound')


def _request(advancement_engine, agent_runtime, context):
    stage=context.root_kind
    if stage not in {'idea','plan','bundle','reasoning'}:
        raise SemanticMcpError('semantic_call_scope_stale')
    try:
        agent_runtime.verify_root_agent_runtime_scope(root_kind=stage,
            run_ref=context.run_ref,attempt_ref=context.attempt_ref,
            root_session_ref=context.root_session_ref,fence_ref=context.fence_ref,
            runtime_binding_hash=context.capability_binding_hash)
        managed=agent_runtime.query_managed_run(context.run_ref)
        if managed is None or managed.get('run_kind')!=stage+'_stage':
            raise SemanticMcpError('semantic_call_scope_stale')
        request=getattr(advancement_engine,'query_'+stage+'_stage_request')(managed['cycle_ref'])
        run=None if request is None else getattr(agent_runtime,'query_'+stage+'_stage_run')(request.request_ref)
        if run is None or (run.run_ref,run.attempt_ref,run.root_session_ref,run.fence_ref,run.runtime_binding_hash)!=(context.run_ref,context.attempt_ref,context.root_session_ref,context.fence_ref,context.capability_binding_hash):
            raise SemanticMcpError('semantic_call_scope_stale')
        return request
    except OwnerConflict as error:
        raise SemanticMcpError('semantic_call_scope_stale') from error


def read_stage_context(advancement_engine, agent_runtime, research_graph, research_memory, context, arguments):
    request=_request(advancement_engine,agent_runtime,context)
    if arguments.get('context_pack_ref')!=request.context_pack_ref:
        raise SemanticMcpError('stage_context_ref_unbound')
    source=arguments.get('source')
    pack=request.context_pack
    question=request.accepted_question
    source_ref=request.context_pack_ref
    index_page=None
    try:
        note_question_ref=question.question_ref
        if source in {'research_notes','research_note_body'} and 'predecessor_ref' in arguments:
            predecessor_ref=arguments['predecessor_ref']
            if not isinstance(predecessor_ref,str) or not predecessor_ref:
                raise SemanticMcpError('research_note_predecessor_unbound')
            predecessor=_predecessor(advancement_engine,request,predecessor_ref)
            if predecessor is None:
                raise SemanticMcpError('research_note_predecessor_unbound')
            note_question_ref=predecessor['closure']['transition'].get('source_question_ref')
            if not isinstance(note_question_ref,str) or not note_question_ref:
                raise SemanticMcpError('research_note_predecessor_unbound')
        if source=='context_pack':
            value=pack
        elif source=='question':
            source_ref=question.content_ref
            value=research_memory.read_question_content(question.content_ref,question.content_hash)
        elif source=='research_notes':
            value=research_memory.query_question_research_notes(quest_ref=question.quest_ref,
                question_ref=note_question_ref,offset=arguments.get('index_offset',0))
            source_ref=note_question_ref
            index_page={key:value[key] for key in ('offset','limit','next_offset')}
        elif source=='research_note_body':
            source_ref=arguments.get('source_ref')
            if not isinstance(source_ref,str) or not source_ref:
                raise SemanticMcpError('research_note_source_unbound')
            value=research_memory.read_question_research_note(quest_ref=question.quest_ref,
                question_ref=note_question_ref,version_ref=source_ref)
        elif source=='literature_records':
            binding=pack.get('literature_binding')
            if binding is None:
                binding=pack.get('question_literature_input',{}).get('binding')
            if not isinstance(binding,dict) or not isinstance(binding.get('revision_ref'),str):
                raise SemanticMcpError('stage_literature_revision_unbound')
            source_ref=binding['revision_ref']
            exact=research_memory.query_question_literature_revision_ref(question_ref=question.question_ref,revision_ref=source_ref)
            if exact is None or (literature_reference(exact) if binding.get('kind')=='QuestionLiteratureReference' else exact)!=binding:
                raise SemanticMcpError('stage_literature_revision_unbound')
            value=exact['records']
        elif source in {'scientific_outcome','predecessor_closure'}:
            source_ref=arguments.get('source_ref')
            if not isinstance(source_ref,str) or not source_ref:
                raise SemanticMcpError('stage_scientific_source_unbound')
            predecessor=_predecessor(advancement_engine,request,source_ref)
            if source=='predecessor_closure':
                if predecessor is None:
                    raise SemanticMcpError('stage_predecessor_source_unbound')
                value=predecessor['closure']
            else:
                source_question_ref=question.question_ref if predecessor is None else predecessor['closure']['transition']['source_question_ref']
                value=research_graph.read_question_scientific_outcome(quest_ref=question.quest_ref,question_ref=source_question_ref,outcome_ref=source_ref)
        elif source in {'question_history','question_index','evidence_index'}:
            index_offset=arguments.get('index_offset',0)
            if type(index_offset)is not int or index_offset<0:
                raise SemanticMcpError('stage_context_page_invalid')
            if source=='question_history':
                value=research_graph.query_question_research_history(quest_ref=question.quest_ref,question_ref=question.question_ref,offset=index_offset,limit=12)
            elif source=='question_index':
                value=research_graph.query_active_question_page(quest_ref=question.quest_ref,offset=index_offset,limit=12,focus_question_ref=question.question_ref)
            else:
                page,refs=research_graph.query_evidence_reference_page(question.quest_ref,offset=index_offset,limit=32)
                value={**page,'items':list(refs)}
            index_page={k:v for k,v in value.items() if k in {'total_count','shown_count','offset','next_offset'}}
            source_ref=question.question_ref if source=='question_history' else question.quest_ref
        else:
            raise SemanticMcpError('stage_context_source_invalid')
        page=context_read_page(value,path=arguments.get('path',[]),offset=arguments.get('offset',0),limit=arguments.get('limit',8192))
    except ValueError as error:
        raise SemanticMcpError(str(error)) from error
    except OwnerConflict as error:
        raise SemanticMcpError(str(error)) from error
    return {**page,'context_pack_ref':request.context_pack_ref,'context_pack_hash':request.context_pack_hash,
        'source':source,'source_ref':source_ref,'index_page':index_page}


def stage_context_operations(*, advancement_engine, agent_runtime, research_graph, research_memory):
    return (SemanticOperation(semantic_operation_id=STAGE_CONTEXT_OPERATION_ID,
        owning_module='research_memory',
        description='Read exact frozen stage inputs or accepted history within the authenticated current Question/Quest. JSON path is not a filesystem path. Return <=16384 UTF-8 text bytes with exact source hash, next_offset and complete; history index_offset pages 12 outcomes/Questions or 32 evidence references.',
        input_schema={'type':'object','properties':{
            'context_pack_ref':{'type':'string','minLength':1,'maxLength':256},
            'source':{'type':'string','enum':['context_pack','question','literature_records','scientific_outcome','predecessor_closure','question_history','question_index','evidence_index']},
            'source_ref':{'type':'string','maxLength':256},
            'path':{'type':'array','maxItems':16,'items':{'type':'string','maxLength':256}},
            'offset':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':16384},
            'index_offset':{'type':'integer','minimum':0}},
            'required':['context_pack_ref','source','path','offset','limit'],'additionalProperties':False},
        output_schema={'type':'object'},
        handler=lambda context,arguments:read_stage_context(advancement_engine,agent_runtime,research_graph,research_memory,context,arguments)),
        SemanticOperation(semantic_operation_id='research_memory.research_notes.read',
            owning_module='research_memory',
            description='Read bounded research-note summaries or an exact immutable note version. Stage calls require context_pack_ref; optional predecessor_ref selects an accepted frozen predecessor within the same Quest. Target calls omit both and read only this Target, its frozen upstream Targets and selected note versions. index_offset pages 12 versions for Targets; source_ref selects an exact version for research_note_body. Explanations do not replace formal metrics or input bindings.',
            input_schema={'type':'object','properties':{
                'context_pack_ref':{'type':'string','minLength':1,'maxLength':256},
                'source':{'type':'string','enum':['research_notes','research_note_body']},
                'source_ref':{'type':'string','maxLength':256},
                'predecessor_ref':{'type':'string','minLength':1,'maxLength':256},
                'path':{'type':'array','maxItems':16,'items':{'type':'string','maxLength':256}},
                'offset':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':16384},
                'index_offset':{'type':'integer','minimum':0}},
                'required':['source','path','offset','limit'],'additionalProperties':False},
            output_schema={'type':'object'},
            handler=lambda context,arguments:read_target_research_notes(agent_runtime,research_memory,context,arguments)
                if context.root_kind == 'target' else read_stage_context(advancement_engine,agent_runtime,research_graph,research_memory,context,arguments)))
