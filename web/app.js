const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {company_name:'企业抬头',industry:'行业',region:'地区',store_count:'门店数',designer_count:'设计师人数',customer_need:'客户需求'};
const sources = {customer_statement:'客户原话',local_demo_directory:'本地模拟企业库',human_review:'人工复核'};
const stepNames = [['aql','AQL 输入'],['scoring','线索评分Agent'],['dialogue','外呼对话Agent'],['verification','企业核验'],['intent','意向判断Agent'],['qualification','SQL输出Agent']];
const goalLabels = {company:'公司称呼',need:'核心需求',current_workflow:'当前做法',pain_point:'主要痛点',target_outcome:'期望效果',scale:'使用规模',pilot_scope:'试点范围',timeline:'预计时间',budget_range:'预算范围',decision_process:'决策参与人',consent:'后续联系同意',closing:'礼貌收尾'};
let bootstrap, state, busy=false, currentReplyExample='';
const statusLabels = {NEW:'新线索',SCORED:'已评分',SCORE_FAILED:'评分失败',CONVERSING:'外呼对话中',VERIFYING:'企业核验中',QUALIFYING:'意向判断中',REVIEW:'待人工复核',READY:'可输出 SQL',NURTURE:'暂不跟进'};
const runLabels = {success:'实时推理成功',simulated:'模拟运行',failed:'调用失败',running:'执行中'};
const viewTitles = {workbench:'线索工作台',records:'运行记录',directory:'企业候选库'};
const stepTargets = {aql:'control-section',scoring:'score-section',dialogue:'dialogue-section',verification:'verification-section',intent:'result-section',qualification:'result-section'};
const beijingTime = new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});

function formatTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : beijingTime.format(date).replaceAll('/', '-');
}
function formatDuration(start,end) {
  if (!start || !end) return '执行中';
  const seconds=(new Date(end)-new Date(start))/1000;
  return !Number.isFinite(seconds)||seconds<0?'—':seconds<1?'不足 1 秒':`${seconds.toFixed(seconds<10?1:0)} 秒`;
}
function currentStepKey() {
  if (!state) return 'aql';
  if (state.status==='NURTURE') return 'intent';
  if (state.call_ended || state.status==='READY') return 'qualification';
  const failed=stepNames.find(([key])=>state.steps[key]?.status==='failed');
  if (failed) return failed[0];
  if (state.steps.scoring?.status==='pending') return 'scoring';
  return 'dialogue';
}
function nextAction() {
  if (!state) return '选择场景并创建线索';
  if (state.status==='READY') return '查看 AI SQL 输出';
  if (state.status==='NURTURE') return '查看低意向判断，或创建新线索';
  if (state.call_ended) return '查看判断结果；需要时进行人工复核';
  if (state.steps.scoring?.status==='failed') return '重试评分，或继续外呼对话';
  const failed=stepNames.find(([key])=>state.steps[key]?.status==='failed');
  if (failed) return '打开运行记录，重试实时推理';
  return '按建议话术输入并提交客户回复';
}
function navigate(view,updateHash=true) {
  const selected=viewTitles[view]?view:'workbench';
  Object.keys(viewTitles).forEach(key=>$(key+'-view').classList.toggle('hidden',key!==selected));
  document.querySelectorAll('[data-view]').forEach(button=>{const active=button.dataset.view===selected;button.classList.toggle('active',active);if(active)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current')});
  $('page-title').textContent=viewTitles[selected];
  document.title=`${viewTitles[selected]} · AI SDR DEMO`;
  if(updateHash&&location.hash!==`#${selected}`)history.pushState(null,'',`#${selected}`);
  window.scrollTo(0,0);
}
function showHelp() {$('help-modal').classList.remove('hidden');document.body.classList.add('modal-open');$('help-start').focus()}
function closeHelp() {$('help-modal').classList.add('hidden');document.body.classList.remove('modal-open');try{sessionStorage.setItem('ai_sdr_help_seen','1')}catch(_){}}
function renderDirectory() {
  const companies=bootstrap?.companies||[];
  $('directory-count').textContent=`共 ${companies.length} 条虚构数据`;
  $('directory-rows').innerHTML=companies.map(c=>`<tr><td data-label="编号">${esc(c.id)}</td><td data-label="企业全称"><b>${esc(c.name)}</b></td><td data-label="简称／别名">${esc((c.aliases||[]).join('、')||'—')}</td><td data-label="地区">${esc(c.region||'—')}</td><td data-label="行业">${esc(c.industry||'—')}</td></tr>`).join('')||'<tr><td colspan="5">暂无候选数据</td></tr>';
}
function renderFlow() {
  const current=currentStepKey();
  $('flow').innerHTML=stepNames.map(([key,label],i)=>{const step=state?.steps?.[key],status=key==='qualification'&&state?.status==='NURTURE'?'skipped':key==='qualification'&&state?.status==='REVIEW'?'review':step?.status||'pending',active=key===current,caption=status==='done'&&['dialogue','verification','intent'].includes(key)?'已运行':{done:'已完成',review:'待补充',failed:'需重试',running:'执行中',pending:'待执行',skipped:'不生成 SQL'}[status]||status,detail=key==='intent'&&step?.status==='done'?`<small>意向 ${esc(state.intent.level)} · ${state.intent.follow_up_willingness?'同意联系':'未确认联系'}</small>`:key==='qualification'&&status==='review'?'<small>尚未达到交接条件</small>':key==='qualification'&&status==='skipped'?'<small>低意向，不交接销售</small>':'';return `<button type="button" class="flow-item ${esc(status)} ${active?'current':''}" data-step="${esc(key)}" ${active?'aria-current="step"':''}><div class="flow-top"><span class="flow-no">0${i+1}</span><span>${esc(caption)}</span></div><strong>${esc(label)}</strong>${detail}</button>`}).join('');
  $('step-detail').innerHTML=state?`<b>当前：${esc(stepNames.find(([key])=>key===current)[1])}</b> · ${esc(nextAction())}。点击任一步骤查看输入与输出依据。`:'创建线索后，点击步骤查看执行依据。';
}
function renderEmpty() {
  $('workbench-view').classList.add('pre-session');
  renderFlow();
  $('mode-badge').textContent='等待创建线索';
  $('score-method').textContent='等待评分';
  $('score-detail').innerHTML='<div class="score-empty">创建线索后显示评分依据。</div>';
  $('run-script').disabled=true;$('retry-score').disabled=true;$('retry').disabled=true;
  ['send','end-call','confirm-candidate','correct','return'].forEach(id=>$(id).disabled=true);
  $('reply').disabled=true;
  $('messages').innerHTML='<div class="empty">创建线索后，智能助理将在这里开始文字外呼。</div>';
  $('next-question').textContent='等待创建线索';
  $('question-goal').textContent='';
  $('reply-example-note').textContent='创建线索后显示本轮示例';
  $('reply-examples').innerHTML='<span class="reply-example-empty">暂无本轮示例</span>';
  $('candidates').innerHTML='<div class="empty">创建线索后显示匹配候选。</div>';
  $('fields').innerHTML='';
  $('decision').innerHTML='<div class="sql-empty">创建线索后显示判断依据。</div>';
  $('sql').innerHTML='<div class="sql-empty">达到 READY 后生成 AI SQL。</div>';
  $('record-summary').innerHTML='<div class="record-empty">尚未创建线索，暂无运行记录。</div>';
  $('agent-log').innerHTML='<div class="empty">Agent 尚未执行。</div>';
  $('state-log').innerHTML='<div class="empty">暂无状态变化。</div>';
  $('record-review').innerHTML='<div class="empty">暂无人工复核操作。</div>';
  $('current-stage').textContent='当前阶段：等待创建线索';
  $('next-action').textContent='下一步：选择场景并创建线索';
}

async function api(path, options={}) {
  const response = await fetch(path, {headers:{'Content-Type':'application/json'},...options});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `请求失败 ${response.status}`);
  return data;
}
function error(message='') { $('error-banner').textContent=message; $('error-banner').classList.toggle('hidden',!message); }
function inputs() {
  return {phone:$('phone').value.trim(),source:$('source').value.trim(),behavior_text:$('behavior-text').value.trim()};
}
function setBehaviorCase(id) {
  $('behavior-case').value=id;
  $('behavior-text').value=bootstrap.behavior_cases.find(item=>item.id===id)?.text||'';
  if(id==='custom')$('mode').value='live';
}
function restoreBehaviorCase(text) {
  $('behavior-text').value=text||'';
  $('behavior-case').value=bootstrap.behavior_cases.find(item=>item.text===text)?.id||'custom';
}
function renderReplyExamples() {
  const terminal=state && (state.call_ended||['READY','NURTURE'].includes(state.status));
  const scenario=bootstrap?.scenarios.find(item=>item.id===(state?.scenario_id||$('scenario').value));
  const generated=state?.mode==='live' && state.reply_example?.source==='deepseek_generated' && state.reply_example.question===state.next_question;
  const ruleExample=state?.reply_example?.source==='rule_based' && state.reply_example.question===state.next_question;
  currentReplyExample=terminal?'':generated||ruleExample?state.reply_example.text:state?.scenario_id==='D'?'':!state?.replies.length||state?.mode==='simulation'?scenario?.replies[state?.replies.length||0]||'':'';
  $('reply-example-note').textContent=terminal?'对话已结束':generated?'DeepSeek 根据本轮下一问生成的虚构示例 · 非客户原话 · 点击仅填入':ruleExample?'服务端规则生成的虚构回复示例 · 非模型输出 · 点击仅填入':state?.scenario_id==='D'?'自由输入场景，请输入任意客户原话':state?.mode==='live'&&state?.replies.length?'本轮暂无模型生成的示例，请手动输入':'预设虚构示例 · 非实时 AI 结果 · 点击仅填入';
  $('reply-examples').innerHTML=currentReplyExample?`<button type="button" class="reply-example" aria-label="填入本轮客户原话示例"><span>本轮</span>${esc(currentReplyExample)}</button>`:`<span class="reply-example-empty">${terminal?'已停止追问': '暂无本轮示例'}</span>`;
}
async function create() {
  try {
    error();
    $('reset').disabled=true;
    $('reset').textContent='创建并评分中…';
    state = await api('/api/sessions',{method:'POST',body:JSON.stringify({mode:$('mode').value,scenario_id:$('scenario').value,aql:inputs()})});
    localStorage.setItem('ai_sdr_session',state.id);
    render();
  } catch(e){error(e.message)} finally {$('reset').disabled=false;$('reset').innerHTML='创建 / 重置场景 <span>↗</span>'}
}
async function mutate(path, body={}) {
  if (!state) return error('请先创建线索。');
  try {error(); state=await api(`/api/sessions/${state.id}/${path}`,{method:'POST',body:JSON.stringify(body)});render();return true} catch(e){error(e.message);return false}
}
function render() {
  if (!state) return renderEmpty();
  $('workbench-view').classList.remove('pre-session');
  $('mode-badge').textContent=state.mode_label;
  $('current-stage').textContent=`当前阶段：${stepNames.find(([key])=>key===currentStepKey())[1]}`;
  $('next-action').textContent=`下一步：${nextAction()}`;
  renderReplyExamples();
  $('mode-badge').classList.toggle('live',state.mode==='live');
  $('run-script').disabled=state.mode!=='simulation'||state.scenario_id==='D';
  $('retry').disabled=state.mode!=='live'||!state.replies.length||state.call_ended||['READY','NURTURE'].includes(state.status);
  $('reply').disabled=busy||state.call_ended||['READY','NURTURE'].includes(state.status);
  $('send').disabled=busy||state.call_ended||['READY','NURTURE'].includes(state.status);
  $('end-call').disabled=busy||state.call_ended||['READY','NURTURE'].includes(state.status);
  $('send').textContent=busy?'分析中…':'提交回复 ↗';
  $('score').textContent=state.score.score??'—';
  $('priority').textContent=state.score.priority;
  $('score-method').textContent={deepseek:'DeepSeek 实时',simulated:'模拟样例',failed:'调用失败',insufficient:'信息不足',pending:'等待评分'}[state.score.method]||'待核对';
  $('retry-score').disabled=busy||state.mode!=='live'||state.score.method==='pending';
  const signals=state.score.signals||[];
  $('score-detail').innerHTML=`<div class="score-summary">${esc(state.score.reason||state.score.rule_note||'暂无评分说明')}</div>${signals.length?signals.map(x=>`<div class="score-signal"><b>行为原文：</b>${esc(x.quote)}<br><b>评分解释：</b>${esc(x.interpretation)}</div>`).join(''):'<div class="score-empty">暂无可核对的行为依据</div>'}`;
  $('status').innerHTML=`${esc(statusLabels[state.status]||state.status)} <em>${esc(state.status)}</em>`;
  $('status-reason').textContent=state.status_reason;
  $('confirmed').innerHTML=`${Object.values(state.fields).filter(f=>f.status==='confirmed').length} <em>/ 6</em>`;
  $('intent').textContent=state.intent.level;
  $('intent-reason').textContent=state.intent.reason;
  error(state.error || '');
  renderFlow();
  $('messages').innerHTML=state.messages.length?state.messages.map(m=>`<div class="bubble ${esc(m.role)}"><small>${m.role==='customer'?'客户原话':'智能助理'} · ${esc(formatTime(m.at))}</small>${esc(m.text)}</div>`).join(''):'<div class="empty">等待客户回复。可运行预设脚本，或手动输入回复。</div>';
  $('next-question').innerHTML=`<b>${state.call_ended?(state.replies.length?'收尾话术：':'结束记录：'):state.replies.length?'本轮建议话术：':'开场话术：'}</b> ${esc(state.next_question)}`;
  $('question-goal').textContent=state.mode==='live'?`本轮目标：${goalLabels[state.question_goal]||'待判断'} · ${state.goal_reason||''} · ${state.replies.length}/6 轮客户回复`:'预设样例规则；不调用 DeepSeek';
  $('candidates').innerHTML=state.candidates.length?state.candidates.map(c=>`<div class="candidate"><div><strong>${esc(c.name)}</strong><small>${esc(c.region)} · ${esc(c.industry)}<br>${esc(c.match_reasons.join('；'))}</small></div><span class="candidate-id">${esc(c.id)}</span></div>`).join(''):'<div class="empty">暂无匹配候选。企业抬头须由客户或人工确认。</div>';
  $('candidate-select').innerHTML=state.candidates.length?state.candidates.map(c=>`<option value="${esc(c.id)}">${esc(c.id)} · ${esc(c.name)}</option>`).join(''):'<option value="">无候选</option>';
  ['confirm-candidate','correct','return'].forEach(id=>$(id).disabled=state.status!=='REVIEW');
  $('confirm-candidate').disabled=state.status!=='REVIEW'||!state.candidates.length;
  $('review-history').innerHTML=state.review_actions.slice().reverse().map(x=>`<div>${esc(formatTime(x.at))} · ${esc(x.detail)}${x.note?' · '+esc(x.note):''}</div>`).join('');
  $('fields').innerHTML=Object.entries(labels).map(([key,label])=>{const f=state.fields[key]||{};return `<tr><td>${esc(label)}</td><td>${f.value===null||f.value===undefined?'未知':esc(f.value)}</td><td>${esc(sources[f.source]||'—')}</td><td>${esc(f.evidence||'暂无证据；建议补问')}</td><td><span class="pill ${esc(f.status||'unknown')}">${esc({confirmed:'已确认',pending:'待确认',unknown:'未知'}[f.status]||'未知')}</span></td></tr>`}).join('');
  $('decision').innerHTML=`<div class="decision-status ${esc(state.status)}">${esc(statusLabels[state.status]||state.status)} <small>${esc(state.status)}</small></div><div class="decision-reason">${esc(state.status_reason)}</div><div class="mini-list"><b>SQL 交接条件：</b>${state.handoff_eligible?'已具备；通话收尾后输出':'尚未全部具备'}<br><b>意向证据：</b>${esc(state.intent.evidence||'暂无可逐字核对的意向原话')}<br><b>证据来源：</b>${esc({deepseek_quote:'模型逐字引文',customer_statement_rule:'客户原话规则补证',unverified:'未核实'}[state.intent.evidence_source]||'客户原话')}<br><b>销售联系同意：</b>${esc(state.intent.willingness_evidence||'客户尚未明确同意')}<br><b>已了解：</b>${esc(Object.keys(state.discovery||{}).map(x=>goalLabels[x]).join('、')||'暂无')}<br><b>后续处理建议：</b>${esc(state.follow_up_suggestions.join('、')||'暂无')}</div>`;
  $('sql').innerHTML=state.ai_sql?`<div class="sql-ready">${esc(JSON.stringify(state.ai_sql,null,2))}</div>`:`<div class="sql-empty">${state.handoff_eligible?'已具备交接条件，仍在通话中；结束后生成 AI SQL':'当前未达到 READY'}<br>当前不生成销售可跟进的 AI SQL</div>`;
  $('record-summary').innerHTML=`<span>案例 ${esc(state.scenario_id)}</span><span>${esc(state.mode_label)}</span><span>${state.replies.length} / 6 轮回复</span><span>${esc(statusLabels[state.status]||state.status)}</span>`;
  $('agent-log').innerHTML=state.agent_runs.length?state.agent_runs.slice().reverse().map(x=>`<div class="log-item ${esc(x.status)}"><b>${esc({scoring:'线索评分Agent',dialogue:'外呼对话Agent',verification:'企业核验 Agent',intent:'意向判断Agent'}[x.agent]||x.agent)}</b><span class="run-badge">${esc(runLabels[x.status]||x.status)}</span><time>${esc(formatTime(x.started_at))} → ${esc(formatTime(x.ended_at))} · ${esc(formatDuration(x.started_at,x.ended_at))}</time><p>${esc(x.reason||'结构化输出已校验')}</p></div>`).join(''):'<div class="empty">Agent 尚未执行。</div>';
  $('state-log').innerHTML=state.events.length?state.events.slice().reverse().map(x=>`<div class="event-item"><time>${esc(formatTime(x.at))}</time><span class="event-status">${esc(statusLabels[x.status]||x.status)}</span><p>${esc(x.reason)}</p></div>`).join(''):'<div class="empty">暂无状态变化。</div>';
  $('record-review').innerHTML=state.review_actions.length?state.review_actions.slice().reverse().map(x=>`<div class="event-item"><time>${esc(formatTime(x.at))}</time><p>${esc(x.detail)}${x.note?' · '+esc(x.note):''}</p></div>`).join(''):'<div class="empty">暂无人工复核操作。</div>';
}

$('flow').addEventListener('click',event=>{const node=event.target.closest('[data-step]');if(!node)return;const key=node.dataset.step,step=state?.steps?.[key],label=stepNames.find(x=>x[0]===key)?.[1]||key,display=key==='qualification'&&state?.status==='NURTURE'?'不生成 SQL':key==='qualification'&&state?.status==='REVIEW'?'待补充':step?.status==='done'&&['dialogue','verification','intent'].includes(key)?'已运行':({done:'已完成',review:'待补充',failed:'需重试',running:'执行中',pending:'待执行'}[step?.status]||step?.status);$('step-detail').innerHTML=step?`<b>${esc(label)}</b> · ${esc(display)}<br><b>输入：</b>${esc(step.input)}<br><b>输出：</b>${esc(step.output)}<br><b>时间：</b>${esc(formatTime(step.at))}<br><button type="button" class="step-jump" data-jump="${esc(key)}">前往对应工作区 ↗</button>`:`<b>${esc(label)}</b> · 该步骤尚未执行。<br><button type="button" class="step-jump" data-jump="${esc(key)}">前往对应工作区 ↗</button>`;});
$('step-detail').addEventListener('click',event=>{const button=event.target.closest('[data-jump]');if(!button)return;const target=$(stepTargets[button.dataset.jump]);if(target){navigate('workbench');target.scrollIntoView({behavior:'smooth',block:'start'})}});
document.querySelectorAll('[data-view]').forEach(button=>button.addEventListener('click',()=>navigate(button.dataset.view)));
window.addEventListener('popstate',()=>navigate(location.hash.slice(1),false));
$('open-help').addEventListener('click',showHelp);
$('close-help').addEventListener('click',closeHelp);
$('help-start').addEventListener('click',()=>{closeHelp();navigate('workbench')});
$('help-modal').addEventListener('click',event=>{if(event.target===$('help-modal'))closeHelp()});
document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!$('help-modal').classList.contains('hidden'))closeHelp()});
$('reset').addEventListener('click',create);
$('run-script').addEventListener('click',()=>mutate('script'));
$('send').addEventListener('click',async()=>{if(busy)return;const reply=$('reply').value.trim();if(!reply)return error('请输入客户原话。');busy=true;render();const saved=await mutate('reply',{reply});if(saved)$('reply').value='';busy=false;render();});
$('end-call').addEventListener('click',async()=>{if(busy||!state||state.call_ended)return;busy=true;render();await mutate('end');busy=false;render();});
$('reply-examples').addEventListener('click',event=>{if(!event.target.closest('.reply-example')||!currentReplyExample||$('reply').disabled)return;$('reply').value=currentReplyExample;$('reply').focus();});
$('confirm-candidate').addEventListener('click',()=>mutate('review',{action:'confirm_candidate',candidate_id:$('candidate-select').value,note:$('review-note').value}));
$('correct').addEventListener('click',()=>mutate('review',{action:'correct_name',company_name:$('correct-name').value,note:$('review-note').value}));
$('return').addEventListener('click',()=>mutate('review',{action:'return_to_dialogue',note:$('review-note').value}));
$('retry').addEventListener('click',()=>mutate('retry'));
$('retry-score').addEventListener('click',async()=>{if(busy)return;busy=true;render();await mutate('score');busy=false;render();});
$('behavior-case').addEventListener('change',()=>{if(bootstrap)setBehaviorCase($('behavior-case').value)});
$('behavior-text').addEventListener('input',()=>{const chosen=bootstrap?.behavior_cases.find(item=>item.id===$('behavior-case').value);if(chosen&&$('behavior-text').value!==chosen.text){$('behavior-case').value='custom';$('mode').value='live'}});
$('scenario').addEventListener('change',()=>{if(!bootstrap)return;const a=bootstrap.aql;$('phone').value=a.phone;$('source').value=a.source;setBehaviorCase(({A:'A',B:'B',C:'C',D:'custom'})[$('scenario').value]);if($('scenario').value==='D')$('mode').value='live';renderReplyExamples()});

try{if(sessionStorage.getItem('ai_sdr_help_seen')!=='1')showHelp()}catch(_){showHelp()}
renderEmpty();
navigate(location.hash.slice(1),false);
(async()=>{try{bootstrap=await api('/api/bootstrap');renderDirectory();$('live-hint').textContent=bootstrap.live_available?`已检测到服务端 Key · ${bootstrap.model}；实时评分及对话均调用模型`:'未检测到服务端 Key；实时模式将显示明确错误';if(bootstrap.live_available)$('mode').value='live';$('phone').value=bootstrap.aql.phone;$('source').value=bootstrap.aql.source;setBehaviorCase('A');const id=localStorage.getItem('ai_sdr_session');if(id){try{state=await api(`/api/sessions/${id}`);$('mode').value=state.mode;$('scenario').value=state.scenario_id;$('phone').value=state.aql.phone||'';$('source').value=state.aql.source||'';restoreBehaviorCase(state.aql.behavior_text);render();return}catch(e){localStorage.removeItem('ai_sdr_session')}}renderEmpty()}catch(e){error(e.message)}})();
