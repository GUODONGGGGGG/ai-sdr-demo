"""预设样例模拟运行的业务核心。这里不调用模型，也不声称实时推理。"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIELD_NAMES = ("company_name", "industry", "region", "store_count", "designer_count", "customer_need")
FIELD_LABELS = dict(zip(FIELD_NAMES, ("企业抬头", "行业", "地区", "门店数", "设计师人数", "客户需求")))
UNKNOWN = {"value": None, "source": None, "evidence": None, "status": "unknown"}
CHINESE_NUMBERS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def read_json(name: str) -> dict:
    with (ROOT / "data" / name).open(encoding="utf-8") as f:
        return json.load(f)


def field(value=None, source=None, evidence=None, status="unknown") -> dict:
    if status not in {"confirmed", "pending", "unknown"}:
        raise ValueError("invalid field status")
    if status == "confirmed" and (value is None or not source or not evidence):
        raise ValueError("confirmed fields require value, source and evidence")
    return {"value": value, "source": source, "evidence": evidence, "status": status}


def score_aql(aql: dict) -> dict:
    """Only fixed behavior examples receive a simulated score."""
    behavior = aql.get("behavior_text", "")
    cases = read_json("scenarios.json")["behavior_cases"]
    sample_scores = {"A": (68, "建议 AI 跟进"), "B": (62, "建议 AI 跟进"), "C": (25, "低优先级孵化"), "E": (85, "优先 AI 跟进")}
    samples = {item["text"]: sample_scores[item["id"]] for item in cases if item["id"] in sample_scores}
    if behavior not in samples:
        return {"score": None, "priority": "自定义行为需实时推理", "signals": [], "method": "simulated", "rule_note": "仅预设行为样例有模拟评分；未调用模型"}
    score, priority = samples[behavior]
    return {"score": score, "priority": priority, "signals": [{"quote": behavior, "interpretation": "预设行为样例评分；不是实时 AI 推理"}], "method": "simulated", "rule_note": "演示样例分数，尚未用真实业务数据校准"}


def search_companies(query: str, region: str | None = None, companies: list[dict] | None = None) -> list[dict]:
    if companies is None:
        companies = read_json("companies.json")["companies"]
    query = query.strip().replace("的", "")
    exact_name_exists = any(query == company["name"] for company in companies)
    results = []
    for company in companies:
        reasons = []
        if query == company["name"]:
            reasons.append("企业全称完全匹配")
        elif not exact_name_exists and query and (query in company["name"] or any(alias in query or query in alias for alias in company["aliases"] if len(query) >= 2)):
            reasons.append("名称或别名匹配")
        if region and region == company["region"]:
            reasons.append("地区匹配（仅供候选排序，不代表经营地区已确认）")
        if reasons and ("企业全称完全匹配" in reasons or "名称或别名匹配" in reasons):
            results.append({"id": company["id"], "name": company["name"], "region": company["region"], "industry": company["industry"], "match_reasons": reasons, "data_notice": "本地模拟企业候选库，非真实工商数据"})
    results.sort(key=lambda c: ("企业全称完全匹配" not in c["match_reasons"], c["id"]))
    return results


def _number(text: str, unit: str) -> int | None:
    match = re.search(r"([0-9]+|[一二三四五六七八九十]+)\s*" + unit, text)
    if not match:
        return None
    raw = match.group(1)
    if raw.isdigit():
        return int(raw)
    if raw == "十":
        return 10
    if raw.startswith("十"):
        return 10 + CHINESE_NUMBERS.get(raw[1:], 0)
    if "十" in raw:
        tens, ones = raw.split("十", 1)
        return CHINESE_NUMBERS.get(tens, 0) * 10 + CHINESE_NUMBERS.get(ones, 0)
    return CHINESE_NUMBERS.get(raw)


def _store_count(text: str) -> int | None:
    direct = _number(text, "家门店")
    if direct is not None:
        return direct
    direct = _number(text, "家店")
    if direct is not None:
        return direct
    match = re.search(r"门店(?:一共|总共|目前|大概|约|有|共)?\s*([0-9]+|[一二三四五六七八九十]+)\s*家", text)
    return _number(match.group(1) + "家", "家") if match else None


def _designer_count(text: str) -> int | None:
    number = r"([0-9]+|[一二三四五六七八九十]+)"
    match = re.search(r"设计师(?:一共|总共|目前|大概|约|有|共)?\s*" + number + r"\s*(?:人|位|名)", text)
    if not match:
        match = re.search(number + r"\s*(?:人|位|名)设计师", text)
    return _number(match.group(1) + "人", "人") if match else None


def evaluate(aql: dict, replies: list[str]) -> dict:
    """Only recognizes the explicitly supplied demo wording; unknown input stays unknown."""
    if not isinstance(replies, list) or not all(isinstance(reply, str) for reply in replies):
        raise ValueError("replies must be text strings")
    fields = {name: dict(UNKNOWN) for name in FIELD_NAMES}
    candidates = []
    region = None
    for reply in replies:
        region_match = re.search(r"(?:在|是)(杭州|宁波)|(?<![\u4e00-\u9fff])(杭州|宁波)的", reply)
        if region_match:
            region = region_match.group(1) or region_match.group(2)
            fields["region"] = field(region, "customer_statement", reply, "confirmed")
        stores = _store_count(reply)
        if stores is not None:
            fields["store_count"] = field(stores, "customer_statement", reply, "confirmed")
        designers = _designer_count(reply)
        if designers is not None:
            fields["designer_count"] = field(designers, "customer_statement", reply, "confirmed")
        if "门店方案设计和团队协同统一" in reply:
            fields["customer_need"] = field("统一门店方案设计与团队协同", "customer_statement", reply, "confirmed")
        elif "设计协同" in reply:
            fields["customer_need"] = field("了解设计协同", "customer_statement", reply, "confirmed")

    text = "\n".join(replies)
    directory = read_json("companies.json")["companies"]
    full_names = [c for c in directory if c["name"] in text]
    if full_names:
        company = full_names[0]
        evidence = next(r for r in replies if company["name"] in r)
        candidates = search_companies(company["name"], region, directory)
        # In this fixed sample, "我们是<完整名称>" is an explicit customer self-identification.
        if "我们是" + company["name"] in evidence:
            fields["company_name"] = field(company["name"], "customer_statement", evidence, "confirmed")
            fields["industry"] = field(company["industry"], "local_demo_directory", f"{company['id']}；客户已确认企业全称；本地模拟企业候选库，非真实工商数据", "confirmed")
        else:
            fields["company_name"] = field(company["name"], "customer_statement", evidence, "pending")
    elif "星桥家居" in text:
        candidates = search_companies("星桥家居", region, directory)
        evidence = next(r for r in replies if "星桥家居" in r)
        fields["company_name"] = field("星桥家居（候选全称待确认）", "customer_statement", evidence, "pending")
    else:
        claim = next(((m.group(1).strip(), reply) for reply in replies if (m := re.search(r"我们是([^，。；]+)", reply))), None)
        if claim:
            claimed_name, evidence = claim
            fields["company_name"] = field(claimed_name, "customer_statement", evidence, "pending")
            candidates = search_companies(claimed_name, region, directory)

    refusal = any("不需要联系" in r or "不要联系" in r or "拒绝" in r for r in replies)
    no_plan = any("没有采购计划" in r for r in replies)
    contact_pattern = r"(?:可以|愿意|方便|同意).{0,12}(?:联系|沟通)|(?:让|请)(?:销售|你们).{0,12}(?:联系|沟通)(?:我|我们)?"
    willingness = any(re.search(contact_pattern, clause) and not re.search(r"(?:不|暂时不|无需|不要).{0,8}(?:联系|沟通)|[？?]", clause) for reply in replies for clause in re.split(r"[，。；\n]", reply)) and not refusal
    if refusal or no_plan:
        intent = {"level": "低", "evidence": next(r for r in replies if "不需要联系" in r or "没有采购计划" in r or "不要联系" in r or "拒绝" in r), "follow_up_willingness": False, "reason": "无近期采购计划或拒绝联系"}
    elif willingness and fields["customer_need"]["status"] == "confirmed":
        intent = {"level": "中", "evidence": next(r for r in replies if re.search(contact_pattern, r)), "follow_up_willingness": True, "reason": "客户提出需求并同意销售联系"}
    else:
        intent = {"level": "未知", "evidence": None, "follow_up_willingness": False, "reason": "尚无足够对话证据"}

    missing = [FIELD_LABELS[name] for name, item in fields.items() if item["status"] != "confirmed"]
    if intent["level"] == "低":
        status, reason = "NURTURE", intent["reason"]
    elif fields["company_name"]["status"] != "confirmed":
        status, reason = "REVIEW", "企业全称待确认；电话中不索取证件，交由人工复核"
    elif fields["customer_need"]["status"] != "confirmed" or intent["level"] not in {"中", "高"} or not willingness:
        status, reason = "REVIEW", "需求原话或继续沟通意愿不足"
    else:
        status, reason = "READY", "抬头、需求和继续沟通意愿均有客户证据"
    sql = {"aql": aql, "fields": fields, "intent": intent, "qualification_note": "演示规则，尚未用真实业务数据校准"} if status == "READY" else None
    return {"mode": "预设样例模拟运行", "directory_notice": "本地模拟企业候选库，非真实工商数据", "aql": aql, "score": score_aql(aql), "replies": replies, "fields": fields, "candidates": candidates, "intent": intent, "status": status, "status_reason": reason, "missing_fields": missing, "follow_up_suggestions": [f"补问{label}" for label in missing], "ai_sql": sql}


def run_scenario(scenario_id: str) -> dict:
    data = read_json("scenarios.json")
    scenario = next((s for s in data["scenarios"] if s["id"] == scenario_id.upper()), None)
    if scenario is None:
        raise ValueError(f"unknown scenario: {scenario_id}")
    result = evaluate(data["aql"], scenario["replies"])
    result["scenario"] = {"id": scenario["id"], "title": scenario["title"]}
    return result
