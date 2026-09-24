import {escapeHtml as e, scenarios, scenarioEvents} from './shared.js';
import {renderVariantA} from './variant-a.js';
import {renderVariantB} from './variant-b.js';
import {renderVariantC} from './variant-c.js';
import {renderQuestionContext, renderStatusSummary, renderSupportRail, renderTargetDock, routeBody} from './composition.js';
import {renderTargetTerminal} from './target-terminal.js';

const app=document.getElementById('app');
const dialog=document.getElementById('detail-dialog');
const names={A:'对话流',B:'步骤导航',C:'研究日志'};
const params=new URLSearchParams(location.search);
const state={variant:['A','B','C'].includes(params.get('variant'))?params.get('variant'):'A',cycleNumber:1,state:'waiting',step:2,view:'primary',query:'',replaying:false,follow:true,replayCount:0,rightMode:'panels',panel:'overview',targetDock:'closed',targetPage:1,targetRaw:false,targetFollow:true,mobilePanel:false,chatDraft:'',chatMessages:[],response:null,dispatch:'current',terminalDock:['train','eval'].includes(params.get('terminal'))?'open':'closed',terminalMode:params.get('terminal')==='eval'?'eval':'train',terminalFollow:true,terminalWrap:false,terminalExtra:{train:0,eval:0}};
let replayTimer;

function allEvents(){
  if(state.dispatch==='previous')return [{id:'h1',kind:'message',time:'#01',step:1,source:state.view==='review'?'review':undefined,title:state.view==='review'?'先核对上一调度整理出的证据缺口':'整理本轮研究需要的数据证据',body:state.view==='review'?'上一调度的材料还不足以确认数据纳入条件。我会保留未核验项，交给后续调度继续。':'已把数据访问、许可和标签说明列为需要补齐的材料。下一次调度将据此组织审计任务。'},{id:'h2',kind:'decision',time:'#02',step:3,title:'本次输出结束，等待后续调度',body:'这段记录属于 dispatch-4，仅表示本次输出结束。Bundle 阶段的后续活动请查看当前调度。'}];
  const events=scenarioEvents(state.state,state.view);
  if(state.response==='reject'&&state.view==='primary')return [...events.slice(0,5),{id:'p6-rejected',kind:'decision',time:'#06',step:3,title:'已收到拒绝回应，等待调整执行方案',body:'本次请求已结束。现有材料继续保留，后续执行需要根据你的回应调整方案。'}];
  return events;
}
function currentContext(){
  let events=allEvents();
  if(state.replaying)events=events.slice(0,state.replayCount);
  if(state.query)events=events.filter(ev=>[ev.title,ev.body,ev.command,ev.output].some(x=>x?.toLowerCase().includes(state.query.toLowerCase())));
  const currentStatus=state.response==='reject'?{label:'等待调整',title:'已拒绝本次请求，等待调整方案',detail:'本次请求已结束；已有材料保留，当前没有待处理请求。',tone:'amber'}:scenarios[state.state];
  const reviewStatus={running:{label:'正在核对',title:'正在核对材料与证据边界',detail:'当前显示独立审查输出。',tone:'green'},waiting:{label:'等待证据',title:'审查等待实验任务的证据回报',detail:'尚不能确认数据纳入条件已经满足。',tone:'amber'},needs_input:{label:'等待证据',title:'审查保留未核验项',detail:'具体执行范围请求可从右侧“需要你”处理。',tone:'amber'},done:{label:'本次审查结束',title:'本次审查输出已结束',detail:'审查意见已记录，Bundle 阶段尚未完成。',tone:'green'},offline:scenarios.offline};
  const status=state.dispatch==='previous'?{label:'历史输出',title:'正在查看 dispatch-4 的独立记录',detail:'本次输出已结束。返回当前调度，可查看后续工作。',tone:'neutral'}:state.replaying?{label:'正在回放',title:'正在回放工作过程',detail:'演示输出将逐条出现；可关闭自动跟随，自由阅读。',tone:'green'}:state.view==='review'?reviewStatus[state.state]:currentStatus;
  return {...state,terminalExtra:state.terminalExtra[state.terminalMode],events,status,currentStatus,pendingCount:state.state==='needs_input'&&!state.response?1:0};
}
const terminalPositions={train:{top:0,left:0},eval:{top:0,left:0}};
function rememberTerminalPosition(){
  const output=app.querySelector('.co-terminal-output');
  if(output)terminalPositions[output.dataset.mode||state.terminalMode]={top:output.scrollTop,left:output.scrollLeft};
}
function render({resetPanel=false,resetMain=false,resetReader=false}={}){
  const ctx=currentContext();
  const mainScroll=resetMain?0:app.querySelector('.workspace')?.scrollTop||0;
  const panelScroll=resetPanel?0:app.querySelector('.co-support-body')?.scrollTop||0;
  const readerScroll=resetReader||resetMain?0:app.querySelector('#variant-content')?.scrollTop||0;
  rememberTerminalPosition();
  const detailsKey=el=>[el.closest('[data-event-id]')?.dataset.eventId||el.closest('[data-workspace-panel]')?.dataset.workspacePanel||'',el.className,el.querySelector('summary')?.textContent].join('|');
  const openDetails=new Set([...app.querySelectorAll('details[open]')].map(detailsKey));
  document.body.dataset.composed='true';
  app.innerHTML=`<div class="prototype-top"><div><strong>光谱台 · 对话流组合原型</strong><span class="prototype-divider"></span><span>保留现有风格与组件</span></div><span class="prototype-version">演示数据 · <a href="./gallery.html">查看早期方案 ↗</a></span></div>
  <div class="lumen-shell prototype-shell co-shell">
    <header class="lumen-header"><div class="lumen-brand" aria-label="Meta-research"><span class="lumen-logo" aria-hidden="true">MR</span><div><b>Meta Research</b><small>Lumen workspace</small></div></div><div class="lumen-quest-context"><small>当前研究 · Cycle ${state.cycleNumber} · 第 ${state.cycleNumber} 轮</small><b>跨 MDD、AD 与精神分裂症的可变导联 EEG 基础模型</b></div><div class="lumen-connection connected"><i aria-hidden="true"></i><span>界面预览 · 演示数据</span></div><div class="lumen-research-power"><button data-action="route" data-route="control"><span>Ⅱ</span><b>研究控制</b></button></div></header>
    <nav class="lumen-rail" aria-label="研究工作区导航">${[['⌂','总览','overview'],['树','问题树','tree'],['▤','研究资料','assets'],['✎','写作','writing'],['◷','历史','history'],['!','需要你','requests'],['＋','创建任务','create']].map(([glyph,label,route])=>`<button class="lumen-rail-button ${route==='overview'?'active':''} ${route==='requests'&&ctx.pendingCount?'has-request':''}" data-action="nav" data-route="${route}" aria-label="${label}${route==='requests'&&ctx.pendingCount?' · 1 项待办':''}"><span aria-hidden="true">${glyph}</span><small>${label}</small>${route==='requests'&&ctx.pendingCount?'<i>1</i>':''}</button>`).join('')}</nav>
    <main class="lumen-main workspace">${renderQuestionContext(ctx)}
      <section class="lumen-research-trace research-surface">
        <header><div><small>Stage · ${names[state.variant]}</small><h1>研究正在发生</h1></div><div class="scene-controls"><label for="scene-select">预览状态</label><select id="scene-select" aria-label="预览状态">${Object.entries(scenarios).map(([id,s])=>`<option value="${id}" ${id===state.state?'selected':''}>${s.label}</option>`).join('')}</select><button class="button replay-btn" data-action="replay">${state.replaying?'□ 结束回放':'▷ 回放过程'}</button></div></header>
        ${renderStatusSummary(ctx)}
        <div class="output-toolbar"><div class="output-tabs" role="group" aria-label="输出来源"><button data-action="view" data-view="primary" class="${state.view==='primary'?'selected':''}" aria-pressed="${state.view==='primary'}">主任务输出</button><button data-action="view" data-view="review" class="${state.view==='review'?'selected':''}" aria-pressed="${state.view==='review'}">审查输出</button></div><button class="co-dispatch" data-action="route" data-route="history">${state.dispatch==='previous'?'dispatch-4 · 历史':'dispatch-5 · 当前'}⌄</button><label class="search-output"><span aria-hidden="true">⌕</span><input id="search" type="search" value="${e(state.query)}" placeholder="查找输出" aria-label="查找输出"></label></div>
        <div class="co-current-note"><details class="co-reader-status ${e(ctx.status.tone)}"><summary><span class="live-dot ${state.state==='offline'||state.dispatch==='previous'?'off':''}"></span>${e(ctx.status.title)}<span>详情⌄</span></summary><p>${e(ctx.status.detail)}</p></details>${state.dispatch==='previous'?'<button class="co-link" data-action="history-current">返回当前调度 ↗</button>':''}</div>
        <div id="variant-content">${({A:renderVariantA,B:renderVariantB,C:renderVariantC}[state.variant])(ctx)}</div>
      </section><div class="prototype-footnote">界面原型 · 所有文案和交互均为演示 · 未接入 8767 服务</div>
    </main>${renderSupportRail(ctx)}
  </div>${renderTargetDock(ctx)}${renderTargetTerminal(ctx)}<div class="co-mobile-tools"><button data-action="right-mode" data-mode="panels">研究面板${ctx.pendingCount?' · 1 项待办':''}</button><button data-action="right-mode" data-mode="assistant">研究助手</button><button data-action="route" data-route="menu">更多入口</button></div><div id="screen-state" class="sr-only" role="status" aria-live="polite">Cycle ${state.cycleNumber}，${ctx.status.label}，显示 ${ctx.events.length} 条${state.view==='review'?'审查':'主任务'}输出。</div>`;
  app.querySelectorAll('details').forEach(el=>{if(openDetails.has(detailsKey(el)))el.open=true;});
  app.querySelector('.workspace').scrollTop=mainScroll;
  app.querySelector('.co-support-body').scrollTop=panelScroll;
  app.querySelector('#variant-content').scrollTop=readerScroll;
  const terminal=app.querySelector('.co-terminal-output');
  if(terminal){terminal.dataset.mode=state.terminalMode;terminal.scrollTop=state.terminalFollow?terminal.scrollHeight:terminalPositions[state.terminalMode].top;terminal.scrollLeft=terminalPositions[state.terminalMode].left;}
}
function followStageOutput(){const reader=app.querySelector('#variant-content');reader?.scrollTo({top:reader.scrollHeight,behavior:'smooth'});}
function stopReplay(){clearInterval(replayTimer);state.replaying=false;}
function replay(){
  if(state.replaying){stopReplay();render();return;}
  state.query='';state.replaying=true;state.replayCount=1;state.step=1;render({resetReader:true});
  replayTimer=setInterval(()=>{state.replayCount++;const events=allEvents();state.step=events[Math.min(state.replayCount,events.length)-1].step;if(state.replayCount>=events.length)stopReplay();render();if(state.follow)followStageOutput();},1900);
}
function openPanel(panel){state.panel=panel;state.rightMode='panels';state.mobilePanel=true;render({resetPanel:true});}
function showRoute(route){dialog.innerHTML=`<button class="dialog-close" data-action="close-dialog" aria-label="关闭详情">×</button>${routeBody(route,currentContext())}<p class="dialog-note">界面演示，仅影响本页预览。</p><button class="button" data-action="close-dialog">返回研究活动</button>`;if(!dialog.open)dialog.showModal();}
function setDispatch(value){stopReplay();state.dispatch=value;state.query='';if(dialog.open)dialog.close();render({resetMain:true});}
function sendChat(){const value=state.chatDraft.trim();if(!value)return;state.chatMessages.push({role:'user',text:value},{role:'assistant',text:`这是演示回复：${currentContext().currentStatus.title}。实验、资料和请求可以从“研究面板”查看；你的消息仅保留在本页，不会发送给真实研究服务。`});state.chatDraft='';render();app.querySelector('.co-chat .lumen-message:last-child')?.scrollIntoView({block:'nearest'});}
document.addEventListener('click',ev=>{
  const button=ev.target.closest('[data-action]');if(!button||button.disabled)return;
  const action=button.dataset.action;
  if(action==='nav'){const route=button.dataset.route;if(route==='overview'){openPanel('overview');app.querySelector('.workspace').scrollTo({top:0,behavior:'smooth'});}else if(route==='requests')openPanel('requests');else showRoute(route);}
  if(action==='route')showRoute(button.dataset.route);
  if(action==='panel')openPanel(button.dataset.panel);
  if(action==='right-mode'){state.rightMode=button.dataset.mode;state.mobilePanel=true;render({resetPanel:true});}
  if(action==='mobile-close'){state.mobilePanel=false;render();}
  if(action==='step'){state.step=Number(button.dataset.step);render();}
  if(action==='view'){stopReplay();state.view=button.dataset.view;state.step=2;state.query='';render({resetReader:true});}
  if(action==='replay')replay();
  if(action==='follow'){state.follow=!state.follow;render();if(state.follow)followStageOutput();}
  if(action==='terminal-open'){rememberTerminalPosition();if(button.dataset.mode)state.terminalMode=button.dataset.mode;state.terminalDock='open';if(state.targetDock==='open')state.targetDock='minimized';render();app.querySelector('.co-terminal-output')?.focus({preventScroll:true});}
  if(action==='terminal-mode'){rememberTerminalPosition();state.terminalMode=button.dataset.mode;render();}
  if(action==='terminal-minimize'){state.terminalDock='minimized';render();}
  if(action==='terminal-close'){state.terminalDock='closed';render();}
  if(action==='terminal-follow'){state.terminalFollow=!state.terminalFollow;render();}
  if(action==='terminal-wrap'){state.terminalWrap=!state.terminalWrap;render();}
  if(action==='terminal-append'){state.terminalExtra[state.terminalMode]=Math.min(50,state.terminalExtra[state.terminalMode]+12);render();}
  if(['task','target-open','dialog-task'].includes(action)){if(dialog.open)dialog.close();state.targetDock='open';render();}
  if(action==='stream'){if(button.dataset.stream==='target'){state.targetDock='open';render();}else showRoute('acquisition');}
  if(action==='target-minimize'){state.targetDock='minimized';render();}
  if(action==='target-close'){state.targetDock='closed';render();}
  if(action==='target-page'){state.targetPage=Number(button.dataset.page);state.targetFollow=state.targetPage===1;render();}
  if(action==='target-follow'){state.targetFollow=!state.targetFollow;if(state.targetFollow)state.targetPage=1;render();}
  if(action==='demo-response'&&state.state==='needs_input'&&!state.response){stopReplay();state.response=button.dataset.choice;state.state='waiting';render();}
  if(action==='artifact')showRoute('artifact');
  if(action==='assistant'){state.rightMode='assistant';state.mobilePanel=true;render();}
  if(action==='chat-send')sendChat();
  if(action==='dialog-panel'){dialog.close();openPanel(button.dataset.panel);}
  if(action==='history-current')setDispatch('current');
  if(action==='history-previous')setDispatch('previous');
  if(action==='create-preview'){const goal=document.getElementById('create-goal').value.trim();document.getElementById('create-feedback').textContent=goal?`本页草稿已准备：${goal}。未创建或启动真实研究。`:'请先填写研究目标。';}
  if(action==='close-dialog')dialog.close();
});
document.addEventListener('change',ev=>{if(ev.target.id==='scene-select'){stopReplay();state.state=ev.target.value;state.response=null;state.query='';state.dispatch='current';render();}if(ev.target.id==='target-raw'){state.targetRaw=ev.target.checked;render();}});
document.addEventListener('input',ev=>{if(ev.target.id==='search'){state.query=ev.target.value;render();document.getElementById('search').focus();}if(ev.target.id==='assistant-input'){state.chatDraft=ev.target.value;app.querySelector('[data-action="chat-send"]').disabled=!state.chatDraft.trim();}});
document.addEventListener('keydown',ev=>{if(ev.target.id==='assistant-input'&&ev.key==='Enter'&&!ev.shiftKey&&!ev.isComposing){ev.preventDefault();sendChat();}});
document.addEventListener('scroll',ev=>{
  const output=ev.target;
  if(output instanceof Element&&output.matches('.co-terminal-output')){
    if(state.terminalFollow&&output.scrollHeight-output.clientHeight-output.scrollTop>8){
      state.terminalFollow=false;
      const toggle=app.querySelector('[data-action="terminal-follow"]');
      if(toggle){toggle.setAttribute('aria-pressed','false');toggle.textContent='跟随最新';}
    }
    rememberTerminalPosition();
  }
},true);
window.addEventListener('popstate',()=>{const key=new URLSearchParams(location.search).get('variant');state.variant=['A','B','C'].includes(key)?key:'A';render();});
render();
