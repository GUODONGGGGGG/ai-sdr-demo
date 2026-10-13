const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {company_name:'企业抬头',industry:'行业',region:'地区',store_count:'门店数',designer_count:'设计师人数',customer_need:'客户需求'};
const sources = {customer_statement:'客户原话',local_demo_directory:'本地模拟企业库',human_review:'人工复核'};
const stepNames = [['aql','AQL 输入'],['scoring','初始评分 Agent'],['dialogue','对话 Agent'],['verification','企业核验'],['intent','商机判断'],['qualification','SQL 校验']];
const goalLabels = {company:'公司称呼',need:'核心需求',current_workflow:'当前做法',pain_point:'主要痛点',target_outcome:'期望效果',scale:'使用规模',pilot_scope:'试点范围',timeline:'预计时间',budget_range:'预算范围',decision_process:'决策参与人',consent:'后续联系同意',closing:'礼貌收尾'};
let bootstrap, state, busy=false, currentReplyExample='';

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
  if (!state) return;
  $('mode-badge').textContent=state.mode_label;
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
  $('status').textContent=state.status;
  $('status-reason').textContent=state.status_reason;
  $('confirmed').innerHTML=`${Object.values(state.fields).filter(f=>f.status==='confirmed').length} <em>/ 6</em>`;
  $('intent').textContent=state.intent.level;
  $('intent-reason').textContent=state.intent.reason;
  error(state.error || '');
  $('flow').innerHTML=stepNames.map(([key,label],i)=>{const step=state.steps[key],status=key==='qualification'&&state.status==='REVIEW'?'review':step?.status||'pending',caption=status==='done'&&['dialogue','verification','intent'].includes(key)?'已运行':{done:'已完成',review:'待补充',failed:'失败',running:'执行中',pending:'待处理'}[status]||status,detail=key==='intent'&&step?.status==='done'?`<small>意向 ${esc(state.intent.level)} · ${state.intent.follow_up_willingness?'同意联系':'未确认联系'}</small><small>${esc(state.intent.evidence?`证据：${state.intent.evidence.slice(0,24)}${state.intent.evidence.length>24?'…':''}`:'暂无可核对证据')}</small>`:key==='qualification'&&status==='review'?'<small>尚未达到交接条件</small>':'';return `<button class="flow-item ${esc(status)}" data-step="${esc(key)}"><div class="flow-top"><span class="flow-no">0${i+1}</span><span>${esc(caption)}</span></div><strong>${esc(label)}</strong>${detail}</button>`}).join('');
  $('messages').innerHTML=state.messages.length?state.messages.map(m=>`<div class="bubble ${esc(m.role)}"><small>${m.role==='customer'?'客户原话':'智能助理'} · ${esc(m.at)}</small>${esc(m.text)}</div>`).join(''):'<div class="empty">等待客户回复。可运行预设脚本，或手动输入回复。</div>';
  $('next-question').innerHTML=`<b>${state.call_ended?(state.replies.length?'收尾话术：':'结束记录：'):state.replies.length?'本轮建议话术：':'开场话术：'}</b> ${esc(state.next_question)}`;
  $('question-goal').textContent=state.mode==='live'?`本轮目标：${goalLabels[state.question_goal]||'待判断'} · ${state.goal_reason||''} · ${state.replies.length}/6 轮客户回复`:'预设样例规则；不调用 DeepSeek';
  $('candidates').innerHTML=state.candidates.length?state.candidates.map(c=>`<div class="candidate"><div><strong>${esc(c.name)}</strong><small>${esc(c.region)} · ${esc(c.industry)}<br>${esc(c.match_reasons.join('；'))}</small></div><span class="candidate-id">${esc(c.id)}</span></div>`).join(''):'<div class="empty">暂无匹配候选。企业抬头须由客户或人工确认。</div>';
  $('candidate-select').innerHTML=state.candidates.length?state.candidates.map(c=>`<option value="${esc(c.id)}">${esc(c.id)} · ${esc(c.name)}</option>`).join(''):'<option value="">无候选</option>';
  ['confirm-candidate','correct','return'].forEach(id=>$(id).disabled=state.status!=='REVIEW');
  $('confirm-candidate').disabled=state.status!=='REVIEW'||!state.candidates.length;
  $('review-history').innerHTML=state.review_actions.slice().reverse().map(x=>`<div>${esc(x.at)} · ${esc(x.detail)}${x.note?' · '+esc(x.note):''}</div>`).join('');
  $('fields').innerHTML=Object.entries(labels).map(([key,label])=>{const f=state.fields[key]||{};return `<tr><td>${esc(label)}</td><td>${f.value===null||f.value===undefined?'未知':esc(f.value)}</td><td>${esc(sources[f.source]||'—')}</td><td>${esc(f.evidence||'暂无证据；建议补问')}</td><td><span class="pill ${esc(f.status||'unknown')}">${esc({confirmed:'已确认',pending:'待确认',unknown:'未知'}[f.status]||'未知')}</span></td></tr>`}).join('');
  $('decision').innerHTML=`<div class="decision-status ${esc(state.status)}">${esc(state.status)}</div><div class="decision-reason">${esc(state.status_reason)}</div><div class="mini-list"><b>SQL 交接条件：</b>${state.handoff_eligible?'已具备；通话收尾后输出':'尚未全部具备'}<br><b>意向证据：</b>${esc(state.intent.evidence||'暂无可逐字核对的意向原话')}<br><b>证据来源：</b>${esc({deepseek_quote:'模型逐字引文',customer_statement_rule:'客户原话规则补证',unverified:'未核实'}[state.intent.evidence_source]||'客户原话')}<br><b>销售联系同意：</b>${esc(state.intent.willingness_evidence||'客户尚未明确同意')}<br><b>已了解：</b>${esc(Object.keys(state.discovery||{}).map(x=>goalLabels[x]).join('、')||'暂无')}<br><b>后续处理建议：</b>${esc(state.follow_up_suggestions.join('、')||'暂无')}</div>`;
  $('sql').innerHTML=state.ai_sql?`<div class="sql-ready">${esc(JSON.stringify(state.ai_sql,null,2))}</div>`:`<div class="sql-empty">${state.handoff_eligible?'已具备交接条件，仍在通话中；结束后生成 AI SQL':'当前未达到 READY'}<br>当前不生成销售可跟进的 AI SQL</div>`;
  $('agent-log').innerHTML=state.agent_runs.length?state.agent_runs.slice(-6).reverse().map(x=>`<div class="log-item ${esc(x.status)}"><b>${esc({scoring:'初始评分 Agent',dialogue:'对话 Agent',verification:'企业核验 Agent',intent:'商机判断 Agent'}[x.agent]||x.agent)} · ${esc(x.status)}</b>${esc(x.started_at)} → ${esc(x.ended_at||'执行中')}<br>${esc(x.reason||'结构化输出已校验')}</div>`).join(''):'<div class="empty">Agent 尚未执行</div>';
  $('state-log').innerHTML=state.events.slice().reverse().map(x=>`<div>${esc(x.at)} · ${esc(x.status)} · ${esc(x.reason)}</div>`).join('');
}

$('flow').addEventListener('click',event=>{const node=event.target.closest('[data-step]');if(!node||!state)return;const key=node.dataset.step,step=state.steps[key];$('step-detail').innerHTML=step?`<b>${esc(stepNames.find(x=>x[0]===key)[1])}</b> · ${key==='qualification'&&state.status==='REVIEW'?'待补充':step.status==='done'&&['dialogue','verification','intent'].includes(key)?'已运行':esc(step.status)}<br><b>输入：</b>${esc(step.input)}<br><b>输出：</b>${esc(step.output)}<br><b>时间：</b>${esc(step.at)}`:'该步骤尚未执行。'});
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

(async()=>{try{bootstrap=await api('/api/bootstrap');$('live-hint').textContent=bootstrap.live_available?`已检测到服务端 Key · ${bootstrap.model}；实时评分及对话均调用模型`:'未检测到服务端 Key；实时模式将显示明确错误';if(bootstrap.live_available)$('mode').value='live';$('phone').value=bootstrap.aql.phone;$('source').value=bootstrap.aql.source;setBehaviorCase('A');const id=localStorage.getItem('ai_sdr_session');if(id){try{state=await api(`/api/sessions/${id}`);$('mode').value=state.mode;$('scenario').value=state.scenario_id;$('phone').value=state.aql.phone||'';$('source').value=state.aql.source||'';restoreBehaviorCase(state.aql.behavior_text);render();return}catch(e){localStorage.removeItem('ai_sdr_session')}}await create()}catch(e){error(e.message)}})();
