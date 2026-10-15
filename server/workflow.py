"""Session state, manual review, and qualification gates."""

from __future__ import annotations

import copy
import json
import re
import uuid
from concurrent.futures import ThreadPoolExecutor

from .core import FIELD_LABELS, FIELD_NAMES, UNKNOWN, _designer_count, _store_count, evaluate, field, read_json, score_aql, search_companies
from .live import AgentError, DeepSeekClient, now


def _event(state: dict, status: str, reason: str) -> None:
    state["status"] = status
    state["status_reason"] = reason
    state["updated_at"] = now()
    state["events"].append({"at": state["updated_at"], "status": status, "reason": reason})


def _step(state: dict, name: str, status: str, input_summary: str, output_summary: str) -> None:
    state["steps"][name] = {"status": status, "input": input_summary, "output": output_summary, "at": now()}


def _interest_quote(replies: list[str]) -> str | None:
    """Return an exact customer sentence with a concrete plan, budget or need."""
    patterns = (r"预算", r"(?:两|2|下个)个月内", r"准备.{0,30}(?:了解|试点|采购|上线)", r"想上(?:一套|系统)", r"最优先.{0,12}解决", r"想把.{0,60}(?:统一|打通|提升)")
    for pattern in patterns:
        for reply in reversed(replies):
            if re.search(pattern, reply):
                return reply
    return None


def _consent_quote(replies: list[str], contexts: list[dict] | None = None) -> str | None:
    """Accept explicit contact permission, including a short answer to that exact question."""
    pattern = r"(?:可以|愿意|方便|同意).{0,12}(?:联系|沟通)|(?:让|请)(?:销售|你们).{0,12}(?:联系|沟通)(?:我|我们)?"
    for index in range(len(replies) - 1, -1, -1):
        reply = replies[index]
        context = contexts[index] if contexts and index < len(contexts) else {}
        if re.search(r"(?:不|暂时不|无需|不要|别).{0,12}(?:联系|沟通)", reply) or (context.get("goal") == "consent" and re.fullmatch(r"\s*(?:不方便|不行|不愿意|不用了|先不用)[。！!，,\s]*", reply)):
            return None
        for clause in re.split(r"[，。；\n]", reply):
            clause = clause.strip()
            if clause and not re.search(r"(?:不|暂时不|无需|不要).{0,8}(?:联系|沟通)", clause) and not re.search(r"[？?]", clause) and re.search(pattern, clause):
                return clause
        if context.get("goal") == "consent" and re.fullmatch(r"\s*(?:可以(?:的)?|好(?:的)?|行|没问题|方便|同意|愿意|嗯可以)[。！!，,\s]*", reply):
            return reply.strip()
    return None


SENSITIVE_REQUEST = re.compile(r"营业执照|统一社会信用代码|社会信用代码|信用代码|身份证|证件|照片|拍照|截图|上传|发送|传给|发(?:给|来|过来)|提交资料|提供资料|手机号|银行卡|验证码|微信号")

GOALS = {
    "need": ("您目前最想改善哪方面的工作？", "目前想把团队协作做得更顺一些。", r"需求|问题|改善|希望|困扰|解决|工作"),
    "profile": ("方便了解一下，贵公司主要做哪类业务、目前在哪个城市经营？", "主要做家居设计，业务在杭州。", r"行业|业务|经营|城市|地区|哪里"),
    "footprint": ("目前大概有几家门店、多少位设计师？", "目前有五家门店，十八位设计师。", r"门店|店面|设计师"),
    "current_workflow": ("这件事目前在团队里一般是怎么处理的？", "现在主要靠表格和群消息协作。", r"目前|现在|平时|怎么|流程"),
    "pain_point": ("现有做法里，最影响效率的地方是什么？", "反复确认版本比较费时间。", r"影响|痛点|麻烦|难|效率|卡"),
    "target_outcome": ("如果能改善，您最希望先看到什么变化？", "希望交接更清楚、少一些返工。", r"希望|目标|变化|效果|改善"),
    "scale": ("大概有多少人会参与这项工作？", "预计先让十位同事参与。", r"多少|规模|几|参与|团队"),
    "pilot_scope": ("如果先试用，您倾向从哪个团队或环节开始？", "想先在一个团队试一下。", r"试|团队|范围|环节|开始"),
    "timeline": ("您大概希望什么时候开始评估或试用？", "可能下个月开始了解。", r"什么时候|时间|近期|何时|多久"),
    "budget_range": ("目前有考虑过这件事的大致投入范围吗？不方便说也没关系。", "目前还在评估，暂时没有明确预算。", r"预算|投入|费用|成本"),
    "decision_process": ("后续如果觉得合适，通常还会有谁一起参与评估？", "业务负责人也会一起看。", r"谁|评估|决定|决策|负责人"),
    "consent": ("谢谢您介绍这些情况。如果后续有合适方案，方便让销售顾问再联系您吗？", "可以让销售顾问下周联系我。", r"联系|沟通|跟进"),
}
DISCOVERY_LABELS = {"current_workflow": "当前做法", "pain_point": "主要痛点", "target_outcome": "期望效果", "scale": "使用规模", "pilot_scope": "试点范围", "timeline": "预计时间", "budget_range": "预算范围", "decision_process": "决策参与人"}
MAX_LIVE_REPLIES = 8


def _company_uncertain(replies: list[str]) -> bool:
    return any(re.search(r"(?:完整名称|全称|公司名|企业名|抬头).{0,12}(?:不确定|不知道|不清楚|记不清|说不准)|(?:不确定|不知道|不清楚|记不清|说不准).{0,12}(?:完整名称|全称|公司名|企业名|抬头)", reply) for reply in replies)


def _stated_company(replies: list[str]) -> tuple[str, str] | None:
    """Recognize a full legal name only when the customer identifies their company."""
    intro = r"(?:我们(?:公司)?(?:的)?(?:全称|公司名|名称)?(?:是|叫)|公司(?:全称|名称|名)(?:是|叫)|企业(?:全称|名称)(?:是|叫)|我们来自)"
    suffix = r"(?:有限责任公司|股份有限公司|有限公司|合伙企业)"
    for reply in reversed(replies):
        match = re.search(intro + r"\s*([^，。；\n]{2,80}?" + suffix + r")", reply)
        if match:
            return match.group(1).strip(), match.group(0)
    return None


def _stated_company_alias(replies: list[str], companies: list[dict]) -> tuple[str, str] | None:
    """Use an alias only when it occurs in the customer's own company introduction."""
    introductions = (r"我们(?:公司)?(?:是|叫)\s*([^，。；\n]+)",
                     r"我(?:在|来自)\s*([^，。；\n]+)")
    aliases = sorted({alias for company in companies for alias in company.get("aliases", []) if len(alias) >= 2}, key=len, reverse=True)
    for reply in reversed(replies):
        for pattern in introductions:
            match = re.search(pattern, reply)
            if not match:
                continue
            claim = match.group(1)
            for alias in aliases:
                if alias in claim:
                    return alias, match.group(0)
    return None


def _safe_question(question: str, previous: list[dict]) -> bool:
    normalized = re.sub(r"\s|[，。？！?]", "", question)
    return bool(normalized and len(question) <= 100 and question.count("？") + question.count("?") <= 1 and not SENSITIVE_REQUEST.search(question) and all(normalized != re.sub(r"\s|[，。？！?]", "", item["text"]) for item in previous))


def _safe_example(kind: str, example: str) -> bool:
    if not example or SENSITIVE_REQUEST.search(example):
        return False
    if kind == "profile":
        return not re.search(r"有限公司|我们是|我们公司叫|公司名称|公司是|预算|采购计划|门店|设计师", example)
    if kind == "footprint":
        return not re.search(r"有限公司|我们是|我们公司叫|公司名称|公司是|预算|采购计划|想要|想把|希望|需要", example)
    if kind != "company":
        return not re.search(r"有限公司|我们是|我们公司叫|公司名称|公司是|[一二三四五六七八九十\d]+家(?:店|门店)|设计师.{0,8}人", example)
    if kind == "company":
        return not re.search(r"门店|设计师|预算|采购计划", example)
    return True


def _qualify(state: dict, fields: dict, candidates: list, intent: dict, reason_hint: str = "", speak: bool = True) -> None:
    refusal = any(x in "\n".join(state["replies"]) for x in ("不需要联系", "不要联系", "拒绝联系", "没有采购计划"))
    if refusal:
        intent = {"level": "低", "evidence": next((r for r in state["replies"] if any(x in r for x in ("不需要联系", "不要联系", "拒绝联系", "没有采购计划"))), None), "follow_up_willingness": False, "reason": "客户拒绝联系或明确无采购计划"}
    state["fields"] = fields
    state["candidates"] = candidates
    intent.setdefault("willingness_evidence", _consent_quote(state["replies"], state.get("reply_contexts")) if intent["follow_up_willingness"] else None)
    state["intent"] = intent
    state["missing_fields"] = [FIELD_LABELS[name] for name, item in fields.items() if item["status"] != "confirmed"]
    willing = intent["follow_up_willingness"]
    if intent["level"] == "低":
        status, reason = "NURTURE", intent["reason"]
    elif fields["company_name"]["status"] != "confirmed":
        status, reason = "REVIEW", "企业全称尚待确认，转人工核对；不在电话中索取证件资料"
    elif fields["customer_need"]["status"] != "confirmed" or intent["level"] not in {"中", "高"} or not willing:
        blockers = []
        if fields["customer_need"]["status"] != "confirmed":
            blockers.append("客户需求缺少可核对的原话")
        if intent["level"] not in {"中", "高"}:
            blockers.append("购买意向尚未确认")
        if not willing:
            blockers.append("客户尚未明确同意销售联系")
        status, reason = "REVIEW", "；".join(blockers)
    else:
        status, reason = "READY", "企业抬头、需求和跟进意愿均有证据"
    state["handoff_eligible"] = status == "READY"
    history = state["question_history"]
    asked = {item["kind"] for item in history}
    proposed = state["next_question"]
    proposed_example = state["reply_example"]
    kind = "closing"
    if status == "READY":
        question = "好的，感谢您抽空介绍。信息已经记下，销售顾问会按您同意的方式联系您。祝您工作顺利！"
    elif status == "NURTURE":
        question = "好的，明白了。感谢您接听，今天先不打扰了。祝您工作顺利！"
    elif len(state["replies"]) >= 6 or "consent" in asked and not willing:
        question = "好的，感谢您抽空介绍。我先记录您的需求，今天先不打扰了。祝您工作顺利。"
    elif fields["customer_need"]["status"] != "confirmed" and "need" not in asked:
        kind = "need"
        question = proposed if _safe_question(proposed, history) and state["mode"] == "live" and re.search(r"需求|问题|改善|希望|困扰|想解决|效率|业务", proposed) and not re.search(r"公司|企业|全称|抬头", proposed) else "您目前最想解决哪方面的业务问题？"
        example = "想看看设计协同，主要是图纸版本老对不上。" if state["scenario_id"] == "B" else "我们想把门店方案设计和团队协同统一起来。"
    elif fields["company_name"]["status"] != "confirmed" and not _company_uncertain(state["replies"]) and "company" not in asked:
        kind = "company"
        question = proposed if _safe_question(proposed, history) and state["mode"] == "live" and re.search(r"公司|企业|全称|抬头", proposed) else "方便问一下，您平时怎么称呼公司？如果记不清完整名称也没关系。"
        example = "我们一般叫星桥家居，完整名称我现在记不清。"
    elif (fields["store_count"]["status"] != "confirmed" or fields["designer_count"]["status"] != "confirmed") and "scale" not in asked:
        kind = "scale"
        if fields["store_count"]["status"] != "confirmed" and fields["designer_count"]["status"] != "confirmed":
            question = "了解了。方便说一下，目前大概有几家门店、多少位设计师吗？"
            example = "目前在杭州有五家门店，设计师大概十八人。" if state["scenario_id"] == "A" else "目前在杭州有三家门店，设计师人数我暂时不确定。"
        elif fields["store_count"]["status"] != "confirmed":
            question, example = "了解了。方便说一下，目前大概有几家门店吗？", "目前在杭州有五家门店。"
        else:
            question, example = "了解了。方便说一下，目前大概有多少位设计师吗？", "设计师人数我暂时不确定。"
    elif intent["level"] not in {"中", "高"} and "plan" not in asked:
        kind = "plan"
        question = "听起来您在关注这个方向。方便问一下，近期有试点或上线的打算吗？"
        example = "我们准备下周了解企业版。" if state["scenario_id"] == "A" else "我们打算先在两家门店试点。"
    elif not willing and "consent" not in asked:
        kind = "consent"
        question = "谢谢您介绍这些情况。如果后续有合适方案，方便让销售顾问再联系您吗？"
        example = "可以，请让销售下周联系我。" if state["scenario_id"] == "B" else "可以让销售联系我。"
    else:
        question = "好的，感谢您抽空介绍。我先记录今天的情况，企业名称由同事内部核对，不耽误您时间了。祝您工作顺利。"
    state["call_ended"] = kind == "closing"
    state["next_question"] = question
    if kind == "closing":
        state["reply_example"] = None
    elif proposed == question and isinstance(proposed_example, dict) and proposed_example.get("question") == question and _safe_example(kind, proposed_example.get("text", "")):
        state["reply_example"] = proposed_example
    else:
        state["reply_example"] = {"text": example, "source": "rule_based", "question": question}
    if speak:
        history.append({"kind": kind, "text": question})
        state["messages"].append({"role": "agent", "text": question, "at": now()})
    state["follow_up_suggestions"] = [] if status == "NURTURE" else [f"补问{label}" for label in state["missing_fields"]]
    if _company_uncertain(state["replies"]):
        state["follow_up_suggestions"] = ["人工核对企业全称" if item == "补问企业抬头" else item for item in state["follow_up_suggestions"]]
    if status == "REVIEW" and not willing:
        state["follow_up_suggestions"].append("确认是否同意销售联系")
    if status == "REVIEW" and intent["level"] not in {"中", "高"}:
        state["follow_up_suggestions"].append("确认近期采购计划")
    _event(state, status, reason)
    state["ai_sql"] = {"aql": state["aql"], "fields": copy.deepcopy(fields), "intent": copy.deepcopy(intent), "qualification_note": "演示规则，尚未用真实业务数据校准"} if status == "READY" else None
    _step(state, "qualification", "review" if status == "REVIEW" else "done", "六字段、意向和复核记录", f"{status}：{reason}")


def new_session(mode: str, aql: dict | None = None, scenario_id: str = "A") -> dict:
    if mode not in {"simulation", "live"}:
        raise ValueError("mode must be simulation or live")
    data = read_json("scenarios.json")
    scenario = next((x for x in data["scenarios"] if x["id"] == scenario_id), None)
    if scenario is None:
        raise ValueError("unknown scenario")
    if scenario_id == "D" and mode != "live":
        raise ValueError("自由输入场景需要 DeepSeek 实时推理模式")
    aql = copy.deepcopy(aql if aql is not None else data["aql"])
    if not isinstance(aql, dict):
        raise ValueError("aql must be an object")
    for key, limit in (("phone", 100), ("source", 100), ("behavior_text", 1200)):
        value = aql.get(key, "")
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError(f"{key} 必须是不超过 {limit} 字的文字")
        aql[key] = value.strip()
    aql = {key: aql[key] for key in ("phone", "source", "behavior_text")}
    opening = "您好，我是企业版产品的智能销售助理。想简单了解一下您的情况，方便先请教贵公司平时怎么称呼吗？"
    state = {
        "id": uuid.uuid4().hex, "mode": mode, "mode_label": "预设样例模拟运行" if mode == "simulation" else "DeepSeek 实时推理",
        "scenario_id": scenario_id, "aql": aql, "score": score_aql(aql) if mode == "simulation" else {"score": None, "priority": "等待 DeepSeek 评分", "signals": [], "method": "pending", "rule_note": "尚未得到模型评分"}, "replies": [], "messages": [{"role": "agent", "text": opening, "at": now()}], "question_history": [{"kind": "company", "text": opening, "targets": ["company_name"]}], "declined_goals": [], "call_ended": False, "fields": {name: dict(UNKNOWN) for name in FIELD_NAMES},
        "candidates": [], "intent": {"level": "未知", "evidence": None, "follow_up_willingness": False, "reason": "尚未对话"},
        "status": "NEW", "status_reason": "线索已创建", "updated_at": now(), "events": [], "steps": {}, "agent_runs": [], "review_actions": [],
        "manual_company": None, "ai_sql": None, "missing_fields": list(FIELD_LABELS.values()), "follow_up_suggestions": [], "error": None,
        "directory_notice": "本地模拟企业候选库，非真实工商数据", "next_question": opening, "reply_example": None,
        "discovery": {}, "handoff_eligible": False, "question_goal": "company", "goal_reason": "先了解客户公司称呼", "reply_contexts": [],
    }
    _event(state, "NEW", "线索已创建")
    _step(state, "aql", "done", "手机号、来源与行为", "AQL 已保存；来源不等于已完成通话")
    if mode == "simulation":
        _event(state, "SCORED", state["score"]["priority"])
        _step(state, "scoring", "done", "AQL 行为文字", f"模拟评分：{state['score']['score'] if state['score']['score'] is not None else '未知'} 分；{state['score']['priority']}")
        state["agent_runs"].append({"agent": "scoring", "started_at": now(), "ended_at": now(), "status": "simulated", "reason": "预设行为样例规则，未调用模型"})
    else:
        _step(state, "scoring", "pending", "来源与行为文字", "等待 DeepSeek 初始线索评分")
    return state


def score_session(state: dict, client: DeepSeekClient | None = None) -> dict:
    """Score AQL before conversation; never substitute a sample after a live failure."""
    if state["mode"] != "live":
        raise ValueError("仅实时模式可调用评分 Agent")
    behavior = state["aql"]["behavior_text"]
    if not behavior:
        state["score"] = {"score": None, "priority": "行为信息不足", "signals": [], "method": "insufficient", "rule_note": "未提供客户行为；未调用模型"}
        _step(state, "scoring", "done", "行为文字为空", "信息不足，不计算分数")
        if not state["replies"]:
            _event(state, "SCORED", "行为信息不足")
        return state
    client = client or DeepSeekClient()
    try:
        result = client.run("scoring", {"source": state["aql"]["source"], "behavior_text": behavior}, state["agent_runs"])
        if any(item["quote"] not in behavior for item in result["signals"]):
            raise AgentError("评分依据不在客户行为原文中")
        value = result["score"]
        priority = "行为信息不足" if value is None else "优先 AI 跟进" if value >= 80 else "建议 AI 跟进" if value >= 50 else "低优先级孵化"
        state["score"] = {"score": value, "priority": priority, "signals": result["signals"], "reason": result["reason"], "method": "deepseek", "rule_note": "DeepSeek 实时评分；优先级区间为演示规则，尚未校准"}
        _step(state, "scoring", "done", "来源与客户行为原文", f"DeepSeek 评分：{value if value is not None else '未知'}；{priority}；{result['reason']}")
        if not state["replies"]:
            _event(state, "SCORED", priority)
    except AgentError as exc:
        state["score"] = {"score": None, "priority": "评分失败，可重试", "signals": [], "method": "failed", "rule_note": "实时评分失败，未回退到预设样例"}
        _step(state, "scoring", "failed", "来源与客户行为原文", str(exc))
        if not state["replies"]:
            _event(state, "SCORE_FAILED", "初筛评分未完成")
    return state


def _apply_manual(state: dict, fields: dict) -> None:
    manual = state["manual_company"]
    if not manual:
        return
    fields["company_name"] = field(manual["name"], "human_review", manual["evidence"], "confirmed")
    try:
        company = next((x for x in read_json("companies.json")["companies"] if x["name"] == manual["name"]), None)
    except (OSError, ValueError, KeyError):
        company = None
    if company:
        fields["industry"] = field(company["industry"], "local_demo_directory", f"{company['id']}；人工已确认抬头；本地模拟企业候选库，非真实工商数据", "confirmed")
    else:
        fields["industry"] = dict(UNKNOWN)


def _simulation_reply(state: dict) -> None:
    result = evaluate(state["aql"], state["replies"])
    fields = result["fields"]
    _apply_manual(state, fields)
    state["agent_runs"].extend([
        {"agent": "dialogue", "started_at": now(), "ended_at": now(), "status": "simulated", "reason": "固定样例规则，未调用模型"},
        {"agent": "verification", "started_at": now(), "ended_at": now(), "status": "simulated", "reason": "固定样例规则，未调用模型"},
        {"agent": "intent", "started_at": now(), "ended_at": now(), "status": "simulated", "reason": "固定样例规则，未调用模型"},
    ])
    state["next_question"] = ""
    state["reply_example"] = None
    _step(state, "dialogue", "done", "客户文字回复", "按预设样例规则提取字段和原话证据")
    _event(state, "VERIFYING", "查询本地模拟企业候选库")
    _step(state, "verification", "done", "客户抬头与地区", f"找到 {len(result['candidates'])} 个候选；候选本身不代表确认")
    _event(state, "QUALIFYING", "根据客户原话判断意向")
    _step(state, "intent", "done", "客户回复和已知字段", f"意向：{result['intent']['level']}")
    _qualify(state, fields, result["candidates"], result["intent"])


def _evidenced_fields(replies: list[str], extracted: dict, evidence: dict) -> dict:
    fields = {name: dict(UNKNOWN) for name in FIELD_NAMES}
    all_text = "\n".join(replies)
    for name in FIELD_NAMES:
        value, quote = extracted.get(name), evidence.get(name)
        if value in (None, "") or not isinstance(quote, str) or not quote.strip() or quote not in all_text:
            continue
        if name in {"store_count", "designer_count"}:
            if isinstance(value, str) and value.isdigit():
                value = int(value)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                continue
            observed = _store_count(quote) if name == "store_count" else _designer_count(quote)
            if observed != value:
                continue
        elif not isinstance(value, str):
            continue
        elif name in {"company_name", "industry", "region"} and value not in quote:
            continue
        elif name == "industry" and re.search(r"(?:有限责任公司|股份有限公司|有限公司|合伙企业)", quote) and not re.search(r"主营|主要做|业务|行业|从事", quote):
            continue
        elif name == "region" and re.search(r"(?:有限责任公司|股份有限公司|有限公司|合伙企业)", quote) and not re.search(r"(?:在|位于|经营|业务|门店)", quote):
            continue
        fields[name] = field(value, "customer_statement", quote, "confirmed")
    stated = _stated_company(replies)
    if stated and fields["company_name"]["status"] != "confirmed":
        fields["company_name"] = field(stated[0], "customer_statement", stated[1], "confirmed")
    # Small, explicit numeric rules recover quantities the model omitted; evidence is still customer text.
    for name, extractor in (("store_count", _store_count), ("designer_count", _designer_count)):
        if fields[name]["status"] == "unknown":
            for reply in reversed(replies):
                value = extractor(reply)
                if value is not None:
                    fields[name] = field(value, "customer_statement", reply, "confirmed")
                    break
    if fields["industry"]["status"] == "unknown":
        for reply in reversed(replies):
            match = re.search(r"(?:主要做|主营业务是|主营|从事|业务是|行业是)\s*([^，。；\n]{2,24})", reply)
            if match:
                value = re.sub(r"(?:的)?行业$", "", match.group(1).strip())
                if value and not re.search(r"不知道|不清楚|不确定|不方便", value):
                    fields["industry"] = field(value, "customer_statement", match.group(0), "confirmed")
                    break
    if fields["region"]["status"] == "unknown":
        for reply in reversed(replies):
            if re.search(r"可能|考虑|计划|准备|打算|某个|某些|哪座|哪个|不确定", reply):
                continue
            match = re.search(r"(?:业务|门店|我们|公司|主要).{0,8}(?:在|位于)\s*([\u4e00-\u9fff]{2,8}?)(?=经营|开展|设点|有|做|，|。|；|$)", reply)
            if match and match.group(1) not in {"全国", "国内", "海外", "当地"} and len(match.group(1)) <= 5 and not re.search(r"城市|地方|门店|公司|业务", match.group(1)):
                fields["region"] = field(match.group(1), "customer_statement", match.group(0), "confirmed")
                break
    if fields["company_name"]["status"] == "unknown":
        for reply in reversed(replies):
            match = re.search(r"我们是\s*([^，。；\n]{2,60})", reply)
            if not match:
                continue
            claim = re.sub(r"^(?:杭州|宁波)的", "", match.group(1).strip())
            if 2 <= len(claim) <= 40:
                fields["company_name"] = field(claim, "customer_statement", match.group(0), "pending")
                break
    if fields["customer_need"]["status"] == "unknown":
        for reply in reversed(replies):
            for clause in re.split(r"[，。；\n]", reply):
                clause = clause.strip()
                if re.search(r"(?:想把|希望把|需要把|想要|希望|需要|打算).{0,80}(?:统一|打通|整合|提升|改善|优化|解决).{1,80}", clause):
                    fields["customer_need"] = field(clause, "customer_statement", clause, "confirmed")
                    break
            if fields["customer_need"]["status"] == "confirmed":
                break
    return fields


def _live_goals(state: dict, fields: dict, intent: dict) -> list[str]:
    asked = {item["kind"] for item in state["question_history"]}
    declined = set(state.get("declined_goals", []))
    prior_questions = {goal: [item for item in state["question_history"] if item["kind"] == goal] for goal in ("profile", "footprint")}
    unavailable = bool(state["replies"] and re.search(r"不清楚|不知道|不确定|不方便透露|记不清", state["replies"][-1]))
    goals = []
    if fields["customer_need"]["status"] != "confirmed" and "need" not in asked and "need" not in declined:
        goals.append("need")
    profile_missing = sum(fields[name]["status"] != "confirmed" for name in ("industry", "region"))
    footprint_missing = sum(fields[name]["status"] != "confirmed" for name in ("store_count", "designer_count"))
    profile_prior = prior_questions["profile"]
    footprint_prior = prior_questions["footprint"]
    profile_pair_asked = bool(profile_prior and len(profile_prior[0].get("targets", [])) == 2)
    footprint_pair_asked = bool(footprint_prior and len(footprint_prior[0].get("targets", [])) == 2)
    if profile_missing and "profile" not in declined and (not profile_prior or len(profile_prior) == 1 and profile_pair_asked and profile_missing == 1 and not unavailable):
        goals.append("profile")
    if footprint_missing and "footprint" not in declined and (not footprint_prior or len(footprint_prior) == 1 and footprint_pair_asked and footprint_missing == 1 and not unavailable):
        goals.append("footprint")
    consent_asks = sum(item["kind"] == "consent" for item in state["question_history"])
    if not intent["follow_up_willingness"] and consent_asks < 2 and "consent" not in declined:
        if len(state["replies"]) >= 4 and fields["customer_need"]["status"] == "confirmed" and consent_asks == 0:
            goals.insert(0, "consent")
        else:
            goals.append("consent")
    return goals


def _field_question(goal: str, fields: dict) -> tuple[str, str, str]:
    """Ask only for missing facts; a related pair fits one natural phone question."""
    if goal == "profile":
        industry = fields["industry"]["status"] != "confirmed"
        region = fields["region"]["status"] != "confirmed"
        if industry and region:
            return "方便了解一下，贵公司主要做哪类业务、目前在哪个城市经营？", "主要做家居设计，业务在杭州。", r"(?=.*(?:行业|业务|做哪类|主营))(?=.*(?:城市|地区|哪里|经营地))"
        if industry:
            return "方便了解一下，贵公司主要做哪类业务？", "主要做家居设计。", r"行业|业务|做哪类|主营"
        return "方便了解一下，贵公司目前主要在哪个城市经营？", "目前主要在杭州经营。", r"城市|地区|哪里|经营地"
    if goal == "footprint":
        stores = fields["store_count"]["status"] != "confirmed"
        designers = fields["designer_count"]["status"] != "confirmed"
        if stores and designers:
            return "目前大概有几家门店、多少位设计师？", "目前有五家门店，十八位设计师。", r"(?=.*(?:门店|店面))(?=.*设计师)"
        if stores:
            return "目前大概有几家门店？", "目前有五家门店。", r"门店|店面"
        return "目前大概有多少位设计师？", "目前有十八位设计师。", r"设计师"
    return GOALS[goal]


def _live_qualify(state: dict, fields: dict, candidates: list, intent: dict, dialogue: dict) -> None:
    """Keep the call open after SQL eligibility while valuable, unanswered goals remain."""
    replies = state["replies"]
    last = replies[-1]
    declined = bool(re.search(r"不需要联系|不要联系|拒绝联系|没有采购计划|不考虑采购", last))
    busy = bool(re.search(r"现在不方便|正在开会|别再打|先不聊|不想聊|挂了", last))
    if declined:
        intent = {"level": "低", "evidence": last, "evidence_source": "customer_statement_rule", "follow_up_willingness": False, "willingness_evidence": None, "reason": "客户明确表示没有计划或拒绝联系"}
    state["fields"], state["candidates"], state["intent"] = fields, candidates, intent
    state["missing_fields"] = [FIELD_LABELS[name] for name, item in fields.items() if item["status"] != "confirmed"]
    eligible = (fields["company_name"]["status"] == "confirmed" and fields["customer_need"]["status"] == "confirmed"
                and intent["level"] in {"中", "高"} and intent["follow_up_willingness"])
    state["handoff_eligible"] = eligible
    goals = _live_goals(state, fields, intent)
    six_fields_confirmed = all(item["status"] == "confirmed" for item in fields.values())
    end = declined or busy or len(replies) >= MAX_LIVE_REPLIES or not goals or (eligible and six_fields_confirmed)
    if end:
        status = "NURTURE" if declined else "READY" if eligible else "REVIEW"
        if status == "READY":
            reason = "企业抬头、需求、意向和联系同意均有原话或人工证据；通话已礼貌结束"
            question = "好的，感谢您抽空介绍。信息已经记下，销售顾问会按您同意的方式联系您。祝您工作顺利！"
        elif status == "NURTURE":
            reason = intent["reason"]
            question = "好的，明白了。感谢您接听，今天先不打扰了。祝您工作顺利！"
        else:
            reason = "通话已结束；" + ("企业全称待人工核对" if fields["company_name"]["status"] != "confirmed" else "SQL 交接条件尚不完整")
            question = "好的，感谢您抽空介绍。我先记录今天的情况，后续由同事内部核对，今天先不打扰了。祝您工作顺利！"
        state["question_goal"] = "closing"
        state["goal_reason"] = dialogue.get("stop_reason", "通话收尾")[:200]
        state["reply_example"] = None
        state["call_ended"] = True
    else:
        # Six SQL fields and contact consent take precedence over optional discovery.
        goal = goals[0]
        fallback, sample, pattern = _field_question(goal, fields)
        if goal == "consent" and any(item["kind"] == "consent" for item in state["question_history"]):
            fallback = "刚才提到的安排我记下了。方便确认一下，后续可以让销售顾问联系您吗？"
        proposed = dialogue.get("next_question", "").strip()
        valid = _safe_question(proposed, state["question_history"]) and bool(re.search(pattern, proposed))
        if goal == "profile" and fields["industry"]["status"] == "confirmed" and re.search(r"行业|业务|做哪类|主营", proposed):
            valid = False
        if goal == "profile" and fields["region"]["status"] == "confirmed" and re.search(r"城市|地区|哪里|经营地", proposed):
            valid = False
        if goal == "footprint" and fields["store_count"]["status"] == "confirmed" and re.search(r"门店|店面", proposed):
            valid = False
        if goal == "footprint" and fields["designer_count"]["status"] == "confirmed" and "设计师" in proposed:
            valid = False
        question = proposed if valid else fallback
        example = dialogue.get("reply_example", "").strip()
        if valid and _safe_example(goal, example):
            state["reply_example"] = {"text": example, "source": "deepseek_generated", "question": question}
        else:
            state["reply_example"] = {"text": sample, "source": "rule_based", "question": question}
        state["question_goal"] = goal
        state["goal_reason"] = dialogue.get("question_reason", "补齐 SQL 所需字段")[:200] if valid else "上轮联系问题尚未得到回答，补问一次" if goal == "consent" and any(item["kind"] == "consent" for item in state["question_history"]) else "优先补齐 SQL 字段；模型建议未覆盖本轮目标，已使用安全话术"
        targets = {"need": ["customer_need"], "profile": [name for name in ("industry", "region") if fields[name]["status"] != "confirmed"],
                   "footprint": [name for name in ("store_count", "designer_count") if fields[name]["status"] != "confirmed"],
                   "consent": ["follow_up_willingness"]}[goal]
        state["question_history"].append({"kind": goal, "text": question, "targets": targets})
        state["call_ended"] = False
        status = "CONVERSING"
        reason = "已具备 SQL 交接条件；继续补齐六个字段" if eligible else "优先收集六个字段与跟进意愿"
    state["next_question"] = question
    state["messages"].append({"role": "agent", "text": question, "at": now()})
    state["follow_up_suggestions"] = [] if status == "NURTURE" else [f"补问{label}" for label in state["missing_fields"]]
    if _company_uncertain(replies):
        state["follow_up_suggestions"] = ["人工核对企业全称" if x == "补问企业抬头" else x for x in state["follow_up_suggestions"]]
    _event(state, status, reason)
    state["ai_sql"] = {"aql": state["aql"], "fields": copy.deepcopy(fields), "intent": copy.deepcopy(intent), "discovery": copy.deepcopy(state["discovery"]), "qualification_note": "演示规则，尚未用真实业务数据校准"} if status == "READY" else None
    _step(state, "qualification", "review" if status == "REVIEW" else "done" if end else "running", "六字段、意向和通话状态", f"{status}：{reason}")


def _live_reply(state: dict, client: DeepSeekClient) -> None:
    possible = _live_goals(state, state["fields"], state["intent"])
    dialogue = client.run("dialogue", {"aql": state["aql"], "replies": state["replies"], "current_fields": state["fields"],
        "question_history": state["question_history"], "declined_goals": state.get("declined_goals", []), "discovery": state["discovery"], "possible_goals": possible,
        "round": len(state["replies"]), "max_rounds": MAX_LIVE_REPLIES, "scenario": state["scenario_id"],
        "sql_field_priority": [name for name in FIELD_NAMES if state["fields"][name]["status"] != "confirmed"]}, state["agent_runs"])
    fields = _evidenced_fields(state["replies"], dialogue["extracted_fields"], dialogue["evidence"])
    for name, previous in state["fields"].items():
        if fields[name]["status"] != "confirmed" and previous["status"] == "confirmed" and ((previous["source"] == "customer_statement" and previous["evidence"] in "\n".join(state["replies"])) or (previous["source"] == "local_demo_directory" and fields["company_name"]["status"] == "confirmed" and fields["company_name"]["value"] == state["fields"]["company_name"]["value"])):
            fields[name] = copy.deepcopy(previous)
    if fields["customer_need"]["status"] != "confirmed" and state["question_history"][-1]["kind"] == "need":
        last_reply = state["replies"][-1]
        if len(last_reply) >= 7 and re.search(r"想|需要|希望|优化|解决|改进|提高|提升|效率|协同|系统", last_reply) and not re.search(r"不清楚|不知道|不确定|不方便|没需求|不需要", last_reply):
            fields["customer_need"] = field(last_reply, "customer_statement", last_reply, "confirmed")
    all_text = "\n".join(state["replies"])
    for name, value in dialogue.get("discovery_fields", {}).items():
        quote = dialogue.get("discovery_evidence", {}).get(name)
        if name in DISCOVERY_LABELS and isinstance(value, str) and value.strip() and isinstance(quote, str) and quote.strip() and quote in all_text:
            state["discovery"][name] = {"value": quote.strip()[:200], "evidence": quote, "source": "customer_statement"}
    state["next_question"] = dialogue["next_question"]
    state["reply_example"] = {"text": dialogue.get("reply_example", ""), "source": "deepseek_generated", "question": dialogue["next_question"]} if dialogue.get("reply_example") else None
    _step(state, "dialogue", "done", "客户文字回复", "提取字段、引用原话并生成下一问")
    _event(state, "VERIFYING", "搜索本地模拟企业候选库")
    name = fields["company_name"]["value"]
    region = fields["region"]["value"]
    try:
        candidates = search_companies(name or "", region)
        if not candidates and fields["company_name"]["status"] != "confirmed":
            directory = read_json("companies.json")["companies"]
            alias_claim = _stated_company_alias(state["replies"], directory)
            if alias_claim:
                candidates = search_companies(alias_claim[0], region, directory)
                if fields["company_name"]["status"] == "unknown":
                    fields["company_name"] = field(alias_claim[0], "customer_statement", alias_claim[1], "pending")
                name = fields["company_name"]["value"]
    except (OSError, ValueError) as exc:
        raise AgentError("本地模拟企业候选库不可用，需人工复核") from exc
    # Candidate lookup is deterministic and can still be shown if a later model call fails.
    state["candidates"] = candidates
    verification_payload = {"replies": state["replies"], "claimed_name": name, "candidates": candidates}
    intent_payload = {"replies": state["replies"], "fields": fields}
    peer = client.parallel_peer()
    if peer is None:
        verification = client.run("verification", verification_payload, state["agent_runs"])
        judged = client.run("intent", intent_payload, state["agent_runs"])
    else:
        with ThreadPoolExecutor(max_workers=2) as pool:
            verification_future = pool.submit(client.run, "verification", verification_payload, state["agent_runs"])
            intent_future = pool.submit(peer.run, "intent", intent_payload, state["agent_runs"])
            verification = verification_future.result()
            judged = intent_future.result()
    valid_ids = {x["id"] for x in candidates}
    if not set(verification["candidate_ids"]).issubset(valid_ids):
        raise AgentError("抬头确认Agent 返回了不存在的候选 ID")
    exact = next((x for x in candidates if x["name"] == name), None)
    stated = _stated_company(state["replies"])
    explicit = bool(stated and stated[0] == name)
    full_legal_name = bool(name and re.search(r"(?:有限责任公司|股份有限公司|有限公司|合伙企业)$", name))
    if explicit and full_legal_name:
        fields["company_name"] = field(name, "customer_statement", next(r for r in state["replies"] if name in r), "confirmed")
        if exact and fields["industry"]["status"] != "confirmed":
            fields["industry"] = field(exact["industry"], "local_demo_directory", f"{exact['id']}；客户自报完整抬头；本地模拟企业候选库，非真实工商数据", "confirmed")
    elif name:
        fields["company_name"]["status"] = "pending"
    _apply_manual(state, fields)
    # Verification is internal. Its confirmation suggestion must not override
    # the dialogue Agent or repeatedly request a company name on the call.
    _step(state, "verification", "done", "客户自述、候选和地区", f"{len(candidates)} 个候选；{'企业全称待人工核对' if fields['company_name']['status'] != 'confirmed' else '客户已自报完整名称'}")
    _event(state, "QUALIFYING", "意向判断Agent 分析客户原话")
    quote = judged["evidence"].strip()
    evidence_ok = bool(quote and quote in "\n".join(state["replies"]))
    fallback_quote = _interest_quote(state["replies"])
    interest_evidence = quote if evidence_ok else fallback_quote
    consent_evidence = _consent_quote(state["replies"], state.get("reply_contexts"))
    if interest_evidence:
        level = judged["intent_level"] if judged["intent_level"] in {"低", "中", "高"} else "中"
        source = "deepseek_quote" if evidence_ok else "customer_statement_rule"
        reason = judged["reason"] if evidence_ok else "模型引文未逐字命中；已用客户原话中的需求、预算或计划补足证据"
    else:
        level, source, reason = "未知", "unverified", "未找到可逐字核对的意向证据"
    intent = {"level": level, "evidence": interest_evidence, "evidence_source": source, "follow_up_willingness": bool(consent_evidence), "willingness_evidence": consent_evidence, "reason": reason}
    _step(state, "intent", "done", "客户原话与字段", f"意向：{level}；证据：{interest_evidence or '暂无'}；销售联系同意：{consent_evidence or '未确认'}；来源：{'模型逐字引文' if evidence_ok else '客户原话规则补证' if fallback_quote else '未核实'}")
    _live_qualify(state, fields, candidates, intent, dialogue)


def add_reply(state: dict, reply: str, client: DeepSeekClient | None = None) -> dict:
    if state["status"] in {"READY", "NURTURE"} or state.get("call_ended"):
        raise ValueError("本次对话已结束；如需重新演示，请创建新线索")
    if state["mode"] == "live" and len(state["replies"]) >= MAX_LIVE_REPLIES:
        raise ValueError(f"客户回复已达到 {MAX_LIVE_REPLIES} 轮上限，请结束对话或人工复核")
    reply = reply.strip()
    if not reply or len(reply) > 2000:
        raise ValueError("回复需为 1–2000 个字符")
    if state["mode"] == "live" and state["question_goal"] in {"company", "need", "profile", "footprint", "consent"} and re.search(r"不清楚|不知道|不确定|不方便透露|记不清|不能说|不愿透露", reply):
        declined = state.setdefault("declined_goals", [])
        if state["question_goal"] not in declined:
            declined.append(state["question_goal"])
    if state["mode"] == "live" and state["question_goal"] == "consent" and re.fullmatch(r"\s*(?:不方便|不行|不愿意|不用了|先不用)[。！!，,\s]*", reply):
        declined = state.setdefault("declined_goals", [])
        if "consent" not in declined:
            declined.append("consent")
    state.setdefault("reply_contexts", []).append({"goal": state.get("question_goal"), "question": state.get("next_question", "")})
    state["replies"].append(reply)
    state["messages"].append({"role": "customer", "text": reply, "at": now()})
    state["error"] = None
    _event(state, "CONVERSING", "收到客户文字回复")
    try:
        if state["mode"] == "simulation":
            _simulation_reply(state)
        else:
            _live_reply(state, client or DeepSeekClient())
    except (AgentError, OSError, json.JSONDecodeError) as exc:
        state["error"] = str(exc) if state["mode"] == "live" else "本地模拟企业候选库读取失败"
        state["ai_sql"] = None
        state["intent"] = {"level": "未知", "evidence": None, "follow_up_willingness": False, "reason": "自动判断尚未完成"}
        state["next_question"] = "自动提问已暂停；请人工复核或稍后重试。"
        state["reply_example"] = None
        failure_kind = "实时推理失败" if state["mode"] == "live" else "本地模拟企业候选库不可用"
        _event(state, "REVIEW", f"{failure_kind}；可人工录入或稍后重试")
        step = next((x for x in ("dialogue", "verification", "intent") if not state["steps"].get(x) or state["steps"][x]["status"] != "done"), "intent")
        _step(state, step, "failed", "当前回复与候选查询", failure_kind)
    return state


def end_call(state: dict) -> dict:
    """Operator ends the call using the evidence already on record."""
    if state.get("call_ended") or state["status"] in {"READY", "NURTURE"}:
        raise ValueError("本次对话已结束")
    fields, intent = state["fields"], state["intent"]
    eligible = (fields["company_name"]["status"] == "confirmed" and fields["customer_need"]["status"] == "confirmed"
                and intent["level"] in {"中", "高"} and intent["follow_up_willingness"])
    declined = any(re.search(r"不需要联系|不要联系|拒绝联系|没有采购计划|不考虑采购", reply) for reply in state["replies"])
    status = "NURTURE" if declined else "READY" if eligible else "REVIEW"
    state["handoff_eligible"] = eligible
    state["call_ended"] = True
    state["question_goal"] = "closing"
    state["goal_reason"] = "演示操作人主动结束对话"
    state["reply_example"] = None
    state["error"] = None
    closing = ("好的，感谢您抽空介绍。销售顾问会按您同意的方式联系您。祝您工作顺利！" if status == "READY" else
               "好的，明白了。感谢您接听，今天先不打扰了。祝您工作顺利！" if status == "NURTURE" else
               "好的，感谢您抽空介绍。今天先不打扰了，祝您工作顺利！")
    state["next_question"] = closing if state["replies"] else "本次对话已由操作人结束，尚未取得客户回复。"
    if state["replies"]:
        state["messages"].append({"role": "agent", "text": closing, "at": now()})
    reason = ("操作人结束对话；已满足 SQL 交接条件" if status == "READY" else
              "操作人结束对话；客户明确拒绝或无采购计划" if status == "NURTURE" else
              "操作人结束对话；SQL 交接证据尚不完整，需人工复核")
    _event(state, status, reason)
    state["ai_sql"] = {"aql": state["aql"], "fields": copy.deepcopy(fields), "intent": copy.deepcopy(intent),
                       "discovery": copy.deepcopy(state["discovery"]), "qualification_note": "操作人主动结束；演示规则，尚未用真实业务数据校准"} if status == "READY" else None
    _step(state, "qualification", "review" if status == "REVIEW" else "done", "当前已核验字段、意向和主动结束操作", f"{status}：{reason}")
    return state


def retry_live(state: dict, client: DeepSeekClient | None = None) -> dict:
    if state["mode"] != "live" or not state["replies"] or state.get("call_ended") or state["status"] in {"READY", "NURTURE"}:
        raise ValueError("仅已有客户回复的实时会话可重试")
    state["error"] = None
    _event(state, "CONVERSING", "手动重试实时推理")
    try:
        _live_reply(state, client or DeepSeekClient())
    except AgentError as exc:
        state["error"] = str(exc)
        state["ai_sql"] = None
        state["intent"] = {"level": "未知", "evidence": None, "follow_up_willingness": False, "reason": "实时判断尚未完成"}
        state["next_question"] = "自动提问已暂停；请人工复核或稍后重试实时推理。"
        state["reply_example"] = None
        _event(state, "REVIEW", f"实时推理重试失败：{exc}")
    return state


def review(state: dict, action: str, candidate_id: str = "", company_name: str = "", note: str = "") -> dict:
    if state["status"] != "REVIEW":
        raise ValueError("仅 REVIEW 状态可人工复核")
    if action not in {"confirm_candidate", "correct_name", "return_to_dialogue"}:
        raise ValueError("未知复核操作")
    if action == "return_to_dialogue" and state["mode"] == "live" and len(state["replies"]) >= MAX_LIVE_REPLIES:
        raise ValueError(f"客户回复已达到 {MAX_LIVE_REPLIES} 轮上限；可完成内部复核或创建新线索")
    if action == "confirm_candidate":
        company = next((x for x in state["candidates"] if x["id"] == candidate_id), None)
        if not company:
            raise ValueError("只能确认当前候选列表中的企业")
        name = company["name"]
        detail = f"人工确认候选 {candidate_id}：{name}"
    elif action == "correct_name":
        name = company_name.strip()
        if not (2 <= len(name) <= 100):
            raise ValueError("企业名称需为 2–100 个字符")
        detail = f"人工修正企业名称：{name}"
    else:
        name, detail = "", "退回补问企业全称和跟进意愿"
    stamp = now()
    state["review_actions"].append({"action": action, "at": stamp, "detail": detail, "note": note.strip()[:500]})
    if action == "return_to_dialogue":
        state["manual_company"] = None
        state["ai_sql"] = None
        state["call_ended"] = False
        if state["mode"] == "simulation":
            state["question_history"] = []
        if state["mode"] == "simulation":
            try:
                restored = evaluate(state["aql"], state["replies"])
                state["fields"] = restored["fields"]
                state["candidates"] = restored["candidates"]
            except (OSError, ValueError, KeyError):
                state["fields"]["company_name"] = dict(UNKNOWN)
                state["fields"]["industry"] = dict(UNKNOWN)
        elif state["fields"]["company_name"]["source"] == "human_review":
            state["fields"]["company_name"] = dict(UNKNOWN)
            state["fields"]["industry"] = dict(UNKNOWN)
        state["missing_fields"] = [FIELD_LABELS[name] for name, item in state["fields"].items() if item["status"] != "confirmed"]
        state["follow_up_suggestions"] = [f"补问{label}" for label in state["missing_fields"]]
        state["next_question"] = "您好，我是刚才联系您的智能销售助理。方便继续聊聊您目前的需求吗？"
        state["reply_example"] = None
        state["messages"].append({"role": "agent", "text": state["next_question"], "at": now()})
        _event(state, "CONVERSING", "人工退回补问")
        return state
    state["manual_company"] = {"name": name, "evidence": f"{detail}；{stamp}；备注：{note.strip()[:500] or '无'}"}
    fields = copy.deepcopy(state["fields"])
    _apply_manual(state, fields)
    if state["mode"] == "live":
        eligible = fields["company_name"]["status"] == "confirmed" and fields["customer_need"]["status"] == "confirmed" and state["intent"]["level"] in {"中", "高"} and state["intent"]["follow_up_willingness"]
        state["fields"] = fields
        state["handoff_eligible"] = eligible
        status = "READY" if eligible and state.get("call_ended") else "CONVERSING" if eligible else "REVIEW"
        _event(state, status, "人工核对企业抬头后重新判断 SQL 交接条件")
        state["ai_sql"] = {"aql": state["aql"], "fields": copy.deepcopy(fields), "intent": copy.deepcopy(state["intent"]), "discovery": copy.deepcopy(state["discovery"]), "qualification_note": "含人工复核记录；演示规则"} if status == "READY" else None
        _step(state, "qualification", "done" if status == "READY" else "review", "人工复核记录", f"{status}：人工核对后重判")
    else:
        _qualify(state, fields, state["candidates"], state["intent"], speak=False)
    return state
