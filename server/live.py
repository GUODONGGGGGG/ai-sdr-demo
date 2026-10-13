"""DeepSeek three-agent adapter. No preset result is used on this path."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import requests


class AgentError(Exception):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate(name: str, data: object) -> dict:
    if not isinstance(data, dict):
        raise AgentError(f"{name} 输出不是 JSON 对象")
    requirements = {
        "scoring": {"score": (int, type(None)), "signals": list, "reason": str},
        "dialogue": {"next_question": str, "extracted_fields": dict, "evidence": dict, "missing_fields": list},
        "verification": {"candidate_ids": list, "ambiguity_reason": str, "confirmation_question": str, "recommend_review": bool},
        "intent": {"intent_level": str, "evidence": str, "follow_up_willingness": bool, "reason": str},
    }
    for key, kind in requirements[name].items():
        if key not in data or not isinstance(data[key], kind):
            raise AgentError(f"{name} 缺少或无效字段：{key}")
    if name == "scoring":
        score = data["score"]
        if isinstance(score, bool) or score is not None and not 0 <= score <= 100:
            raise AgentError("scoring.score 必须是 0–100 的整数或 null")
        if len(data["signals"]) > 5 or not all(isinstance(x, dict) and isinstance(x.get("quote"), str) and isinstance(x.get("interpretation"), str) and x["quote"].strip() and len(x["quote"]) <= 120 and len(x["interpretation"]) <= 160 for x in data["signals"]):
            raise AgentError("scoring.signals 格式无效")
        if len(data["reason"]) > 300 or score is not None and not data["signals"]:
            raise AgentError("scoring 缺少可核对的行为依据")
    if name == "dialogue":
        allowed = {"company_name", "industry", "region", "store_count", "designer_count", "customer_need"}
        if set(data["extracted_fields"]) - allowed or set(data["evidence"]) - allowed:
            raise AgentError("dialogue 包含未知 SQL 字段")
        if not all(isinstance(x, str) for x in data["missing_fields"]):
            raise AgentError("dialogue.missing_fields 格式无效")
        if not all(isinstance(v, (str, int, type(None))) and not isinstance(v, bool) for v in data["extracted_fields"].values()):
            raise AgentError("dialogue.extracted_fields 格式无效")
        if not all(isinstance(v, str) for v in data["evidence"].values()):
            raise AgentError("dialogue.evidence 格式无效")
        if not isinstance(data.get("question_goal", ""), str) or not isinstance(data.get("question_reason", ""), str):
            raise AgentError("dialogue 问题目标格式无效")
        stop_value = data.get("stop_decision", "continue")
        if not isinstance(stop_value, (str, bool)):
            raise AgentError("dialogue.stop_decision 格式无效")
        data["stop_decision"] = "finish" if stop_value is True or isinstance(stop_value, str) and stop_value.strip().lower() in {"finish", "stop", "end", "结束", "停止", "terminate"} else "continue"
        if not isinstance(data.get("stop_reason", ""), str):
            raise AgentError("dialogue.stop_reason 格式无效")
        for key in ("discovery_fields", "discovery_evidence"):
            if not isinstance(data.get(key, {}), dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.get(key, {}).items()):
                raise AgentError(f"dialogue.{key} 格式无效")
    if name in {"dialogue", "verification"}:
        example = data.get("reply_example", "")
        if not isinstance(example, str) or len(example) > 500:
            raise AgentError(f"{name}.reply_example 格式无效")
        data["reply_example"] = example.strip()
    if name == "verification" and not all(isinstance(x, str) for x in data["candidate_ids"]):
        raise AgentError("verification.candidate_ids 格式无效")
    if name == "intent" and data["intent_level"] not in {"低", "中", "高", "未知"}:
        raise AgentError("intent.intent_level 格式无效")
    return data


class DeepSeekClient:
    def __init__(self, session: requests.Session | None = None):
        self.custom_http = session is not None
        self.http = session or requests.Session()
        self.model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
        self.endpoint = os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions")
        self.key = os.getenv("DEEPSEEK_API_KEY", "")

    def parallel_peer(self) -> DeepSeekClient | None:
        """Concurrent calls use separate HTTP sessions; test doubles stay sequential."""
        return None if self.custom_http else DeepSeekClient()

    def run(self, name: str, payload: dict, log: list[dict]) -> dict:
        if not self.key:
            log.append({"agent": name, "started_at": now(), "ended_at": now(), "status": "failed", "reason": "服务端未配置 DEEPSEEK_API_KEY"})
            raise AgentError("服务端未配置 DEEPSEEK_API_KEY")
        system = {
            "scoring": "你是初始线索评分 Agent，只分析输入中的来源与客户行为记录，不读取后续通话或场景标签。行为是观察记录，不等于客户购买意向、企业身份、销售联系同意或 SQL 资格。请根据行为的主动性、产品相关性、重复/深入程度和时间信息，对是否值得优先开启 AI 外呼给出 0-100 整数评分；明确主动申请演示可高分，浅层浏览应低分。不得凭空补充行为、预算、采购计划或身份；来源仅作背景，不独立证明意向。行为太模糊、缺失或无法判断时 score 为 null。signals 中 quote 必须是 behavior_text 的逐字连续片段，每条 interpretation 解释其与联系优先级的关系，最多 5 条。reason 简述评分理由及不确定性。仅输出 JSON：{\"score\":null,\"signals\":[],\"reason\":\"\"}。",
            "dialogue": "你是电话外呼中的智能销售助理。只根据客户 replies 和已问过的问题设计下一问；场景 A/B/C 仅是演示标签，不代表客户事实或固定结果，D 是完全自由输入。语气自然、简短、有礼貌，每轮只问一个具体问题。先理解需求，再灵活追问当前做法、痛点、目标、规模、试点范围、时间、预算大致范围、决策参与人和后续联系同意；从 possible_goals 选一个未问且有价值的 question_goal，问题须直接对应目标，不能重复或无意义追问。客户回复最多 6 轮；第 4 轮左右若仍未取得联系同意，应优先询问是否方便销售后续联系。企业全称、需求、意向和明确联系同意均有证据时，可以立即建议结束并礼貌收尾，无需用满 6 轮；客户拒绝、忙碌或无采购计划也应建议立即结束。客户说不清企业全称时接受并转人工核对，不反复追问。不得索取营业执照、照片、证件、统一社会信用代码、手机号、验证码或要求上传发送文件。只能从 replies 原话提取 SQL 字段和 discovery_fields；对应 evidence、discovery_evidence 必须是 replies 中逐字连续出现的片段。不要把示例当成客户事实。reply_example 是对 next_question 的一句虚构客户回答，只回答所问，不抢先给出尚未问及的公司或其他信息。仅输出 JSON：{\"question_goal\":\"\",\"question_reason\":\"\",\"next_question\":\"\",\"reply_example\":\"\",\"stop_decision\":\"continue\",\"stop_reason\":\"\",\"extracted_fields\":{},\"evidence\":{},\"missing_fields\":[],\"discovery_fields\":{},\"discovery_evidence\":{}}。SQL 字段仅为 company_name、industry、region、store_count、designer_count、customer_need；discovery 字段仅为 current_workflow、pain_point、target_outcome、scale、pilot_scope、timeline、budget_range、decision_process。",
            "verification": "你是内部企业核验 Agent，不直接和客户通话。候选来自本地模拟库，非真实工商数据；匹配不等于确认。只能使用给定候选 ID。若企业全称未确认，可建议一次口头询问公司常用全称；客户说不清时标记人工复核，不再追问。confirmation_question 不得索取营业执照、照片、证件、统一社会信用代码，也不得要求发送或上传资料。仅输出 JSON：{\"candidate_ids\":[],\"ambiguity_reason\":\"\",\"confirmation_question\":\"\",\"reply_example\":\"\",\"recommend_review\":true}。",
            "intent": "你是商机判断 Agent。仅依据 replies 中的客户原话判断购买意向与是否同意销售继续联系；线索评分不是购买意向。evidence 必须是 replies 中逐字连续出现的片段，禁止改写或拼接；若没有可引用原话则填空字符串。只有客户明确允许销售联系，follow_up_willingness 才能为 true；需求、预算、上线计划本身不等于联系同意。仅输出 JSON：{\"intent_level\":\"未知\",\"evidence\":\"\",\"follow_up_willingness\":false,\"reason\":\"\"}。意向可为低、中、高、未知。",
        }[name]
        if name == "scoring":
            system += "评分锚点：0–19 无有效产品行为或纯路过；20–39 单次浅层浏览；40–59 重复浏览或查看具体功能/价格但无主动咨询；60–79 主动咨询、提交相关表单或反复深入了解；80–100 明确申请演示且提出具体使用场景。根据原文对相邻档次小幅调整；不得把未留资单独解释成拒绝联系。"
        last_error = ""
        for attempt in (1, 2):
            entry = {"agent": name, "attempt": attempt, "started_at": now(), "ended_at": None, "status": "running"}
            log.append(entry)
            try:
                response = self.http.post(
                    self.endpoint,
                    headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
                    json={"model": self.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], "response_format": {"type": "json_object"}, "thinking": {"type": "disabled"}, "max_tokens": 700 if name == "scoring" else 2500, "stream": False},
                    timeout=(5, 30),
                )
                response.raise_for_status()
                body = response.json()
                choice = body["choices"][0]
                if choice.get("finish_reason") != "stop":
                    raise AgentError(f"模型输出未正常结束：{choice.get('finish_reason', 'unknown')}")
                content = choice["message"]["content"]
                data = _validate(name, json.loads(content))
                entry.update({"ended_at": now(), "status": "success"})
                return data
            except (requests.RequestException, ValueError, KeyError, IndexError, TypeError, AgentError) as exc:
                # Never expose server response bodies or authentication headers in the UI log.
                if isinstance(exc, requests.HTTPError) and exc.response is not None:
                    last_error = f"API 返回 HTTP {exc.response.status_code}"
                elif isinstance(exc, requests.Timeout):
                    last_error = "请求超时"
                elif isinstance(exc, requests.RequestException):
                    last_error = "网络请求失败"
                else:
                    last_error = str(exc)
                entry.update({"ended_at": now(), "status": "failed", "reason": last_error})
        raise AgentError(f"{name} 两次尝试均失败：{last_error}")
