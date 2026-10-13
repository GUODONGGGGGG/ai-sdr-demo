import copy
import json
import os
import threading
import unittest
from unittest.mock import patch
import requests

from server.app import app
from server.core import read_json
from server.live import AgentError, DeepSeekClient, _validate
from server.workflow import _safe_example, add_reply, end_call, new_session, review, retry_live, score_session


class FakeResponse:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"finish_reason": "stop", "message": {"content": self.content}}]}


class FakeHTTP:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(next(self.responses))


class TimeoutHTTP:
    def __init__(self):
        self.calls = 0

    def post(self, url, **kwargs):
        self.calls += 1
        raise requests.Timeout("synthetic timeout")


class WorkflowTests(unittest.TestCase):
    def test_live_scoring_uses_behavior_quote_and_does_not_grant_sql(self):
        behavior = "主动提交演示申请，希望了解五家门店的协同方案。"
        response = {"score": 84, "signals": [{"quote": "主动提交演示申请", "interpretation": "主动表达了解产品的需求"}], "reason": "适合优先开启外呼，但尚无通话意向证据"}
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-only"}):
            fake = FakeHTTP([json.dumps(response, ensure_ascii=False)])
            state = new_session("live", {"phone": "138xxxx1234", "source": "网站", "behavior_text": behavior})
            score_session(state, DeepSeekClient(fake))
        self.assertEqual(state["score"]["score"], 84)
        self.assertEqual(state["score"]["method"], "deepseek")
        self.assertEqual(state["score"]["priority"], "优先 AI 跟进")
        self.assertIsNone(state["ai_sql"])
        self.assertEqual(state["intent"]["level"], "未知")
        self.assertEqual(len(fake.calls), 1)

    def test_scoring_rejects_invented_behavior_evidence(self):
        response = {"score": 90, "signals": [{"quote": "已经确定采购预算", "interpretation": "高意向"}], "reason": "预算明确"}
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-only"}):
            state = new_session("live", {"behavior_text": "浏览了功能页"})
            score_session(state, DeepSeekClient(FakeHTTP([json.dumps(response, ensure_ascii=False)])))
        self.assertIsNone(state["score"]["score"])
        self.assertEqual(state["score"]["method"], "failed")
        self.assertEqual(state["steps"]["scoring"]["status"], "failed")

    def test_scoring_timeout_never_falls_back_to_sample(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-only"}):
            state = new_session("live", read_json("scenarios.json")["aql"])
            score_session(state, DeepSeekClient(TimeoutHTTP()))
        self.assertIsNone(state["score"]["score"])
        self.assertEqual(state["score"]["method"], "failed")
        self.assertEqual(state["status"], "SCORE_FAILED")

    def test_custom_behavior_is_not_scored_by_sample_rules(self):
        state = new_session("simulation", {"behavior_text": "参加了线上活动并下载技术白皮书"})
        self.assertIsNone(state["score"]["score"])
        self.assertEqual(state["score"]["method"], "simulated")

    def test_review_then_follow_up(self):
        data = read_json("scenarios.json")
        state = new_session("simulation", data["aql"], "B")
        add_reply(state, data["scenarios"][1]["replies"][0])
        self.assertEqual(state["status"], "REVIEW")
        review(state, "confirm_candidate", "DEMO-001", note="已核对营业执照")
        self.assertEqual(state["status"], "REVIEW")
        self.assertEqual(state["fields"]["company_name"]["source"], "human_review")
        self.assertTrue(state["review_actions"][0]["at"])
        add_reply(state, "最近想把门店方案设计和团队协同统一起来，可以让销售联系我。")
        self.assertEqual(state["status"], "READY")
        self.assertIsNotNone(state["ai_sql"])

    def test_correction_and_return(self):
        state = new_session("simulation", scenario_id="B")
        add_reply(state, read_json("scenarios.json")["scenarios"][1]["replies"][0])
        review(state, "correct_name", company_name="杭州虚构云帆设计有限公司")
        self.assertEqual(state["fields"]["company_name"]["value"], "杭州虚构云帆设计有限公司")
        self.assertEqual(state["fields"]["industry"]["status"], "unknown")
        review(state, "return_to_dialogue")
        self.assertEqual(state["status"], "CONVERSING")
        self.assertEqual(state["fields"]["company_name"]["status"], "pending")
        self.assertIsNone(state["ai_sql"])

    def test_live_no_key_is_review_and_never_simulates(self):
        with patch.dict(os.environ, {}, clear=True):
            state = new_session("live")
            add_reply(state, read_json("scenarios.json")["scenarios"][0]["replies"][0])
        self.assertEqual(state["status"], "REVIEW")
        self.assertEqual(state["mode_label"], "DeepSeek 实时推理")
        self.assertIn("DEEPSEEK_API_KEY", state["error"])
        self.assertIn("自动提问已暂停", state["next_question"])
        self.assertIsNone(state["ai_sql"])

    def test_invalid_json_retries_once(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-only"}):
            fake = FakeHTTP(["not-json", "still-not-json"])
            client = DeepSeekClient(fake)
            log = []
            with self.assertRaises(AgentError):
                client.run("dialogue", {"replies": ["虚构内容"]}, log)
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual([x["status"] for x in log], ["failed", "failed"])
        self.assertNotIn("test-only", json.dumps(log))

    def test_timeout_retries_once_without_fake_result(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-only"}):
            fake = TimeoutHTTP()
            state = new_session("live")
            add_reply(state, "虚构测试客户回复", DeepSeekClient(fake))
        self.assertEqual(fake.calls, 2)
        self.assertEqual(state["status"], "REVIEW")
        self.assertIsNone(state["ai_sql"])
        self.assertIn("请求超时", state["error"])

    def test_directory_failure_stays_review(self):
        state = new_session("simulation")
        with patch("server.workflow.evaluate", side_effect=OSError("missing private path")):
            add_reply(state, "我们是虚构企业。")
        self.assertEqual(state["status"], "REVIEW")
        self.assertIsNone(state["ai_sql"])
        self.assertEqual(state["error"], "本地模拟企业候选库读取失败")

    def test_refusal_ends_conversation(self):
        state = new_session("simulation", scenario_id="C")
        add_reply(state, read_json("scenarios.json")["scenarios"][2]["replies"][0])
        self.assertEqual(state["status"], "NURTURE")
        self.assertIn("今天先不打扰", state["next_question"])
        with self.assertRaisesRegex(ValueError, "已结束"):
            add_reply(state, "再问一次")

    def test_live_mock_three_agents_and_evidence_gate(self):
        reply = "我们是杭州星桥家居设计有限公司，在杭州有五家门店，设计师大概十八人。最近想统一门店方案设计和团队协同，可以让销售联系我。"
        dialogue = {"next_question": "还有其他需求吗？", "extracted_fields": {"company_name": "杭州星桥家居设计有限公司", "region": "杭州", "customer_need": "统一门店方案设计和团队协同"}, "evidence": {"company_name": "我们是杭州星桥家居设计有限公司", "region": "在杭州", "customer_need": "想统一门店方案设计和团队协同"}, "missing_fields": ["industry", "store_count", "designer_count"]}
        verification = {"candidate_ids": ["DEMO-001"], "ambiguity_reason": "完整名称匹配", "confirmation_question": "请确认企业全称", "recommend_review": False}
        intent = {"intent_level": "中", "evidence": "可以让销售联系我", "follow_up_willingness": True, "reason": "需求明确且愿意联系"}
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-only"}):
            fake = FakeHTTP([json.dumps(x, ensure_ascii=False) for x in (dialogue, verification, intent)])
            state = new_session("live")
            add_reply(state, reply, DeepSeekClient(fake))
        self.assertEqual(state["status"], "READY")
        self.assertTrue(state["handoff_eligible"])
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(fake.calls[0][1]["json"]["thinking"], {"type": "disabled"})
        self.assertEqual({x["agent"] for x in state["agent_runs"]}, {"dialogue", "verification", "intent"})
        self.assertTrue(all(x["status"] == "success" for x in state["agent_runs"]))
        self.assertEqual(state["fields"]["industry"]["source"], "local_demo_directory")
        self.assertEqual(state["fields"]["store_count"]["value"], 5)
        self.assertEqual(state["fields"]["designer_count"]["value"], 18)

    def test_live_keeps_first_round_fields_when_model_omits_them_later(self):
        replies = ["我们是杭州星桥家居设计有限公司，在杭州有五家门店，设计师大概十八人。", "最近想把门店方案设计和团队协同统一起来，准备下周了解企业版，可以让销售联系我。"]
        first = {"next_question": "需要解决什么问题？", "reply_example": "想统一门店方案设计和团队协同。", "extracted_fields": {"company_name": "杭州星桥家居设计有限公司", "region": "杭州"}, "evidence": {"company_name": "我们是杭州星桥家居设计有限公司", "region": "在杭州"}, "missing_fields": ["customer_need"]}
        second = {"next_question": "还有补充吗？", "extracted_fields": {"customer_need": "统一门店方案设计与团队协同"}, "evidence": {"customer_need": "最近想把门店方案设计和团队协同统一起来"}, "missing_fields": []}
        verification = {"candidate_ids": ["DEMO-001"], "ambiguity_reason": "全称匹配", "confirmation_question": "请确认全称", "recommend_review": False}
        unknown = {"intent_level": "未知", "evidence": "", "follow_up_willingness": False, "reason": "尚无意愿"}
        willing = {"intent_level": "中", "evidence": "可以让销售联系我", "follow_up_willingness": True, "reason": "同意跟进"}
        responses = [first, verification, unknown, second, verification, willing]
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-only"}):
            client = DeepSeekClient(FakeHTTP([json.dumps(x, ensure_ascii=False) for x in responses]))
            state = new_session("live")
            add_reply(state, replies[0], client)
            self.assertEqual(state["status"], "CONVERSING")
            self.assertEqual(state["fields"]["store_count"]["value"], 5)
            self.assertEqual(state["reply_example"]["question"], state["next_question"])
            self.assertEqual(state["reply_example"]["source"], "deepseek_generated")
            add_reply(state, replies[1], client)
        self.assertEqual(state["status"], "READY")
        self.assertTrue(all(x["status"] == "confirmed" for x in state["fields"].values()))
        self.assertTrue(state["handoff_eligible"])
        self.assertTrue(state["call_ended"])
        self.assertIsNotNone(state["ai_sql"])

    def test_live_verification_and_intent_run_in_parallel(self):
        barrier = threading.Barrier(2)
        reply = "我们是杭州星桥家居设计有限公司，在杭州有五家门店，设计师大概十八人。想把设计和协同统一起来，可以让销售联系我。"

        class ParallelFake:
            def parallel_peer(self):
                return self

            def run(self, name, payload, log):
                log.append({"agent": name, "status": "success"})
                if name == "dialogue":
                    return {"next_question": "还有补充吗？", "reply_example": "暂时没有补充。", "extracted_fields": {"company_name": "杭州星桥家居设计有限公司", "region": "杭州", "customer_need": "想把设计和协同统一起来"}, "evidence": {"company_name": "我们是杭州星桥家居设计有限公司", "region": "在杭州", "customer_need": "想把设计和协同统一起来"}}
                barrier.wait(timeout=2)
                if name == "verification":
                    return {"candidate_ids": ["DEMO-001"], "ambiguity_reason": "全称匹配", "confirmation_question": "请确认全称", "reply_example": "全称如上。"}
                return {"intent_level": "高", "evidence": "可以让销售联系我", "follow_up_willingness": True, "reason": "同意跟进"}

        state = new_session("live")
        add_reply(state, reply, ParallelFake())
        self.assertEqual(state["status"], "READY")
        self.assertTrue(state["handoff_eligible"])
        self.assertEqual({x["agent"] for x in state["agent_runs"]}, {"dialogue", "verification", "intent"})

    def test_live_interest_without_contact_consent_stays_review_and_asks_directly(self):
        replies = [
            "我们是杭州星桥家居设计有限公司，在杭州有五家门店，设计师大概十八人。",
            "我们主要想上一套能提升门店设计效率的门店管理软件。",
            "预算大概在十万以内，希望两个月内能用起来。",
        ]
        dialogue = {"next_question": "还有什么需求？", "reply_example": "我们想提高效率。", "extracted_fields": {"company_name": "杭州星桥家居设计有限公司", "region": "杭州", "customer_need": "想上一套能提升门店设计效率的门店管理软件"}, "evidence": {"company_name": "我们是杭州星桥家居设计有限公司", "region": "在杭州", "customer_need": "想上一套能提升门店设计效率的门店管理软件"}, "missing_fields": []}
        verification = {"candidate_ids": ["DEMO-001"], "ambiguity_reason": "全称匹配", "confirmation_question": "请确认全称", "reply_example": "全称如上。", "recommend_review": False}
        intent = {"intent_level": "高", "evidence": "客户有很强购买意向", "follow_up_willingness": True, "reason": "预算和上线计划明确"}
        followup = {"next_question": "还有补充吗？", "reply_example": "没有。", "extracted_fields": {}, "evidence": {}, "missing_fields": []}
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-only"}):
            fake = FakeHTTP([json.dumps(x, ensure_ascii=False) for x in (dialogue, verification, intent, followup, verification, intent)])
            state = new_session("live")
            state["replies"] = replies[:2]
            add_reply(state, replies[2], DeepSeekClient(fake))
            self.assertEqual(state["status"], "CONVERSING")
            self.assertEqual(state["intent"]["level"], "高")
            self.assertEqual(state["intent"]["evidence"], replies[2])
            self.assertEqual(state["intent"]["evidence_source"], "customer_statement_rule")
            self.assertFalse(state["intent"]["follow_up_willingness"])
            self.assertIn("优先收集", state["status_reason"])
            self.assertEqual(state["reply_example"]["source"], "rule_based")
            self.assertEqual(state["steps"]["qualification"]["status"], "running")
            self.assertIn(replies[2], state["steps"]["intent"]["output"])
            add_reply(state, "可以，请让销售下周联系我。", DeepSeekClient(fake))
        self.assertEqual(state["status"], "READY")
        self.assertTrue(state["intent"]["follow_up_willingness"])
        self.assertEqual(state["intent"]["willingness_evidence"], "请让销售下周联系我")

    def test_empty_model_question_gets_targeted_fallback(self):
        dialogue = {"next_question": "", "reply_example": "", "extracted_fields": {"company_name": "杭州星桥家居设计有限公司", "region": "杭州"}, "evidence": {"company_name": "我们是杭州星桥家居设计有限公司", "region": "在杭州"}, "missing_fields": ["customer_need"]}
        verification = {"candidate_ids": ["DEMO-001"], "ambiguity_reason": "全称匹配", "confirmation_question": "", "reply_example": "", "recommend_review": False}
        intent = {"intent_level": "未知", "evidence": "", "follow_up_willingness": False, "reason": "暂无购买信号"}
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-only"}):
            fake = FakeHTTP([json.dumps(x, ensure_ascii=False) for x in (dialogue, verification, intent)])
            state = new_session("live")
            add_reply(state, "我们是杭州星桥家居设计有限公司，在杭州有五家门店。", DeepSeekClient(fake))
        self.assertEqual(state["status"], "CONVERSING")
        self.assertIn("最想改善", state["next_question"])
        self.assertEqual(state["reply_example"]["source"], "rule_based")

    def test_live_recovers_explicit_need_when_model_omits_it(self):
        replies = ["我们是杭州星桥家居设计有限公司，在杭州有五家门店，设计师大概十八人。", "最近想把门店方案设计和团队协同统一起来，准备下周了解企业版，可以让销售联系我。"]
        first = {"next_question": "需要解决什么问题？", "extracted_fields": {"company_name": "杭州星桥家居设计有限公司", "region": "杭州"}, "evidence": {"company_name": "我们是杭州星桥家居设计有限公司", "region": "在杭州"}, "missing_fields": ["customer_need"]}
        second = {"next_question": "还有补充吗？", "extracted_fields": {}, "evidence": {}, "missing_fields": ["customer_need"]}
        verification = {"candidate_ids": ["DEMO-001"], "ambiguity_reason": "全称匹配", "confirmation_question": "请确认全称", "recommend_review": False}
        unknown = {"intent_level": "未知", "evidence": "", "follow_up_willingness": False, "reason": "尚无意愿"}
        willing = {"intent_level": "高", "evidence": "可以让销售联系我", "follow_up_willingness": True, "reason": "同意跟进"}
        responses = [first, verification, unknown, second, verification, willing]
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-only"}):
            client = DeepSeekClient(FakeHTTP([json.dumps(x, ensure_ascii=False) for x in responses]))
            state = new_session("live")
            add_reply(state, replies[0], client)
            add_reply(state, replies[1], client)
        self.assertEqual(state["status"], "READY")
        self.assertEqual(state["fields"]["customer_need"]["value"], "最近想把门店方案设计和团队协同统一起来")
        self.assertEqual(state["fields"]["customer_need"]["source"], "customer_statement")

    def test_api_refresh_and_script_reset(self):
        client = app.test_client()
        state = client.post("/api/sessions", json={"mode": "simulation", "scenario_id": "A"}).json
        first = client.post(f"/api/sessions/{state['id']}/script").json
        second = client.post(f"/api/sessions/{state['id']}/script").json
        restored = client.get(f"/api/sessions/{state['id']}").json
        self.assertEqual(len(first["replies"]), 5)
        self.assertEqual(len(second["replies"]), 5)
        self.assertEqual(restored["status"], "READY")

    def test_scripted_customer_examples_answer_the_current_question(self):
        scenarios = read_json("scenarios.json")["scenarios"]
        for scenario in scenarios[:2]:
            state = new_session("simulation", scenario_id=scenario["id"])
            self.assertIn("贵公司平时怎么称呼", state["next_question"])
            self.assertIn("我们是", scenario["replies"][0])
            for index, reply in enumerate(scenario["replies"]):
                if index:
                    self.assertEqual(state["reply_example"]["text"], reply)
                    self.assertEqual(state["reply_example"]["question"], state["next_question"])
                add_reply(state, reply)
            self.assertTrue(state["call_ended"])

    def test_b_phone_call_does_not_press_for_documents_and_closes_in_review(self):
        state = new_session("simulation", scenario_id="B")
        self.assertIn("您好", state["messages"][0]["text"])
        self.assertIn("智能销售助理", state["messages"][0]["text"])
        for reply in read_json("scenarios.json")["scenarios"][1]["replies"]:
            add_reply(state, reply)
            self.assertNotRegex(state["next_question"], "营业执照|照片|统一社会信用代码")
        self.assertEqual(state["status"], "REVIEW")
        self.assertTrue(state["call_ended"])
        self.assertIsNone(state["ai_sql"])
        self.assertIn("感谢", state["next_question"])
        self.assertNotRegex("\n".join(m["text"] for m in state["messages"] if m["role"] == "agent"), "营业执照|统一社会信用代码|照片")
        with self.assertRaisesRegex(ValueError, "已结束"):
            add_reply(state, "继续问")
        review(state, "confirm_candidate", "DEMO-001", note="内部复核虚构候选")
        self.assertEqual(state["status"], "READY")
        self.assertTrue(state["call_ended"])
        self.assertEqual(state["fields"]["company_name"]["source"], "human_review")

    def test_live_unsafe_model_question_is_replaced_by_phone_script(self):
        reply = "我们是杭州的星桥家居，具体全称我现在不确定。门店有三家，想看看设计协同。"
        dialogue = {"next_question": "请拍照上传营业执照。", "reply_example": "我发照片给你。", "extracted_fields": {"company_name": "星桥家居", "customer_need": "想看看设计协同"}, "evidence": {"company_name": "星桥家居", "customer_need": "想看看设计协同"}, "missing_fields": []}
        verification = {"candidate_ids": ["DEMO-001", "DEMO-002"], "ambiguity_reason": "简称有歧义", "confirmation_question": "请提供统一社会信用代码。", "reply_example": "代码是123", "recommend_review": True}
        intent = {"intent_level": "中", "evidence": "想看看设计协同", "follow_up_willingness": False, "reason": "有初步兴趣"}
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-only"}):
            client = DeepSeekClient(FakeHTTP([json.dumps(x, ensure_ascii=False) for x in (dialogue, verification, intent)]))
            state = new_session("live", scenario_id="B")
            add_reply(state, reply, client)
        self.assertEqual(state["status"], "CONVERSING")
        self.assertIn("目前", state["next_question"])
        self.assertNotRegex(state["next_question"], "营业执照|统一社会信用代码|照片")
        self.assertEqual(state["reply_example"]["source"], "rule_based")
        self.assertEqual(len([m for m in state["messages"] if m["role"] == "agent"]), 2)

    def test_live_b_keeps_customer_alias_when_model_omits_company(self):
        reply = "我们是杭州的星桥家居，公司的完整名称我现在记不清。门店有三家，想看看设计协同。"
        dialogue = {"next_question": "请上传营业执照照片。", "extracted_fields": {"customer_need": "想看看设计协同"}, "evidence": {"customer_need": "想看看设计协同"}, "missing_fields": ["company_name"]}
        verification = {"candidate_ids": ["DEMO-001", "DEMO-002"], "ambiguity_reason": "简称匹配", "confirmation_question": "请提供信用代码", "recommend_review": True}
        intent = {"intent_level": "中", "evidence": "想看看设计协同", "follow_up_willingness": False, "reason": "初步兴趣"}
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-only"}):
            state = new_session("live", scenario_id="B")
            add_reply(state, reply, DeepSeekClient(FakeHTTP([json.dumps(x, ensure_ascii=False) for x in (dialogue, verification, intent)])))
        self.assertEqual(state["fields"]["company_name"]["value"], "星桥家居")
        self.assertEqual(state["fields"]["company_name"]["status"], "pending")
        self.assertEqual([x["id"] for x in state["candidates"]], ["DEMO-001", "DEMO-002"])
        self.assertEqual(state["status"], "CONVERSING")
        self.assertIsNone(state["ai_sql"])

    def test_d_requires_live_and_has_no_preset_script(self):
        with self.assertRaisesRegex(ValueError, "实时推理"):
            new_session("simulation", scenario_id="D")
        state = new_session("live", scenario_id="D")
        self.assertEqual(state["replies"], [])
        self.assertIsNone(state["reply_example"])
        client = app.test_client()
        response = client.post("/api/sessions", json={"mode": "simulation", "scenario_id": "D"})
        self.assertEqual(response.status_code, 400)

    def test_live_prioritizes_sql_fields_over_optional_model_goal(self):
        class GoalClient:
            def parallel_peer(self):
                return None

            def run(self, name, payload, log):
                log.append({"agent": name, "status": "success"})
                if name == "dialogue":
                    return {"question_goal": "timeline", "question_reason": "了解试用时间", "next_question": "您大概希望什么时候开始试用？",
                            "reply_example": "下个月开始试用。", "stop_decision": "finish" if payload["round"] >= 6 else "continue",
                            "stop_reason": "信息已足够", "extracted_fields": {"company_name": "杭州星桥家居设计有限公司", "customer_need": "想把门店方案设计和团队协同统一起来"},
                            "evidence": {"company_name": "我们是杭州星桥家居设计有限公司", "customer_need": "想把门店方案设计和团队协同统一起来"},
                            "missing_fields": [], "discovery_fields": {"timeline": "下个月"}, "discovery_evidence": {"timeline": "下个月"}}
                if name == "verification":
                    return {"candidate_ids": ["DEMO-001"], "ambiguity_reason": "完整名称匹配", "confirmation_question": "", "recommend_review": False}
                return {"intent_level": "高", "evidence": "想把门店方案设计和团队协同统一起来", "follow_up_willingness": True, "reason": "需求及同意联系"}

        state = new_session("live", scenario_id="D")
        client = GoalClient()
        add_reply(state, "我们是杭州星桥家居设计有限公司，想把门店方案设计和团队协同统一起来，可以让销售联系我。", client)
        self.assertEqual(state["question_goal"], "profile")
        self.assertIn("城市", state["next_question"])
        self.assertEqual(state["reply_example"]["source"], "rule_based")
        self.assertTrue(state["handoff_eligible"])
        self.assertEqual(state["status"], "CONVERSING")
        self.assertIsNone(state["ai_sql"])
        for reply in ("下个月。", "我们现在靠表格协作。", "版本经常混乱。", "希望减少返工。", "先在一组试用。"):
            if state["call_ended"]:
                break
            add_reply(state, reply, client)
        self.assertEqual(state["status"], "READY")
        self.assertTrue(state["call_ended"])
        self.assertIsNotNone(state["ai_sql"])
        self.assertEqual(state["discovery"]["timeline"]["evidence"], "下个月")

    def test_live_refusal_stops_immediately(self):
        class RefusalClient:
            def parallel_peer(self):
                return None

            def run(self, name, payload, log):
                if name == "dialogue":
                    return {"next_question": "还想了解什么？", "extracted_fields": {}, "evidence": {}, "missing_fields": []}
                if name == "verification":
                    return {"candidate_ids": [], "ambiguity_reason": "无企业信息", "confirmation_question": "", "recommend_review": True}
                return {"intent_level": "低", "evidence": "没有采购计划", "follow_up_willingness": False, "reason": "不考虑"}

        state = new_session("live", scenario_id="C")
        add_reply(state, "我们目前没有采购计划，也不需要后续联系，谢谢。", RefusalClient())
        self.assertEqual(state["status"], "NURTURE")
        self.assertTrue(state["call_ended"])
        self.assertIsNone(state["ai_sql"])

    def test_reply_example_cannot_volunteer_company_or_scale_before_question(self):
        self.assertFalse(_safe_example("need", "我们是星桥家居，有五家店。"))
        self.assertFalse(_safe_example("timeline", "我们公司叫云帆设计。"))
        self.assertTrue(_safe_example("company", "我们是杭州虚构云帆设计有限公司。"))

    def test_model_stop_alias_is_normalized(self):
        base = {"next_question": "", "extracted_fields": {}, "evidence": {}, "missing_fields": []}
        for value in ("stop", "end", "结束", True):
            self.assertEqual(_validate("dialogue", {**base, "stop_decision": value})["stop_decision"], "finish")

    def test_arbitrary_full_company_is_customer_confirmed_without_directory_match(self):
        class ArbitraryClient:
            def parallel_peer(self):
                return None

            def run(self, name, payload, log):
                if name == "dialogue":
                    return {"question_goal": "need", "next_question": "您目前最想改善哪方面的工作？", "reply_example": "想提高协作效率。",
                            "extracted_fields": {"company_name": "苏州虚构远山科技有限公司"}, "evidence": {"company_name": "我们是苏州虚构远山科技有限公司"}, "missing_fields": []}
                if name == "verification":
                    return {"candidate_ids": [], "ambiguity_reason": "本地库无候选", "confirmation_question": "", "recommend_review": True}
                return {"intent_level": "未知", "evidence": "", "follow_up_willingness": False, "reason": "尚无明确需求"}

        state = new_session("live", scenario_id="D")
        add_reply(state, "我们是苏州虚构远山科技有限公司。", ArbitraryClient())
        self.assertEqual(state["fields"]["company_name"]["status"], "confirmed")
        self.assertEqual(state["fields"]["industry"]["status"], "unknown")
        self.assertEqual(state["candidates"], [])

    def test_operator_can_end_without_evidence_and_cannot_end_twice(self):
        state = new_session("live", scenario_id="D")
        end_call(state)
        self.assertEqual(state["status"], "REVIEW")
        self.assertTrue(state["call_ended"])
        self.assertIsNone(state["ai_sql"])
        self.assertEqual(state["agent_runs"], [])
        with self.assertRaisesRegex(ValueError, "已结束"):
            end_call(state)
        with self.assertRaisesRegex(ValueError, "已结束"):
            add_reply(state, "补一句")

    def test_operator_end_api_uses_existing_verified_evidence(self):
        class EligibleClient:
            def parallel_peer(self):
                return None

            def run(self, name, payload, log):
                log.append({"agent": name, "status": "success"})
                if name == "dialogue":
                    return {"question_goal": "timeline", "next_question": "您希望什么时候开始评估？", "reply_example": "下个月。",
                            "stop_decision": "continue", "extracted_fields": {"company_name": "杭州星桥家居设计有限公司", "customer_need": "想把门店设计和团队协作统一起来"},
                            "evidence": {"company_name": "我们是杭州星桥家居设计有限公司", "customer_need": "想把门店设计和团队协作统一起来"}, "missing_fields": []}
                if name == "verification":
                    return {"candidate_ids": ["DEMO-001"], "ambiguity_reason": "完整名称匹配", "confirmation_question": "", "recommend_review": False}
                return {"intent_level": "中", "evidence": "想把门店设计和团队协作统一起来", "follow_up_willingness": True, "reason": "需求和联系同意明确"}

        state = new_session("live")
        add_reply(state, "我们是杭州星桥家居设计有限公司，想把门店设计和团队协作统一起来，可以让销售联系我。", EligibleClient())
        self.assertEqual(state["status"], "CONVERSING")
        self.assertTrue(state["handoff_eligible"])
        self.assertIsNone(state["ai_sql"])
        from server.app import SESSIONS
        SESSIONS[state["id"]] = state
        response = app.test_client().post(f"/api/sessions/{state['id']}/end")
        self.assertEqual(response.status_code, 200)
        ended = response.json
        self.assertEqual(ended["status"], "READY")
        self.assertTrue(ended["call_ended"])
        self.assertIsNotNone(ended["ai_sql"])
        self.assertEqual(len(ended["agent_runs"]), 3)
        self.assertIn("操作人结束对话", ended["status_reason"])

    def test_model_stop_waits_for_remaining_sql_fields(self):
        class FinishClient:
            def parallel_peer(self):
                return None

            def run(self, name, payload, log):
                if name == "dialogue":
                    return {"question_goal": "timeline", "next_question": "什么时候开始评估？", "stop_decision": "finish",
                            "extracted_fields": {"company_name": "杭州星桥家居设计有限公司", "customer_need": "想把门店设计和团队协作统一起来"},
                            "evidence": {"company_name": "我们是杭州星桥家居设计有限公司", "customer_need": "想把门店设计和团队协作统一起来"}, "missing_fields": []}
                if name == "verification":
                    return {"candidate_ids": ["DEMO-001"], "ambiguity_reason": "全称匹配", "confirmation_question": "", "recommend_review": False}
                return {"intent_level": "中", "evidence": "想把门店设计和团队协作统一起来", "follow_up_willingness": True, "reason": "可联系"}

        state = new_session("live")
        add_reply(state, "我们是杭州星桥家居设计有限公司，想把门店设计和团队协作统一起来，可以让销售联系我。", FinishClient())
        self.assertEqual(len(state["replies"]), 1)
        self.assertEqual(state["status"], "CONVERSING")
        self.assertEqual(state["question_goal"], "profile")
        self.assertFalse(state["call_ended"])
        self.assertIsNone(state["ai_sql"])

    def test_six_fields_collected_in_five_replies_and_end_early(self):
        class SixFieldClient:
            def parallel_peer(self):
                return None

            def run(self, name, payload, log):
                replies = payload["replies"]
                if name == "dialogue":
                    text = "\n".join(replies)
                    values = {}
                    evidence = {}
                    for key, marker, value in (
                        ("company_name", "我们是杭州星桥家居设计有限公司", "杭州星桥家居设计有限公司"),
                        ("customer_need", "想把设计和协同统一起来", "想把设计和协同统一起来"),
                        ("region", "主要在杭州经营", "杭州"),
                    ):
                        if marker in text:
                            values[key], evidence[key] = value, marker
                    return {"question_goal": "timeline", "next_question": "什么时候开始？", "reply_example": "下个月。",
                            "stop_decision": "finish", "extracted_fields": values, "evidence": evidence, "missing_fields": []}
                if name == "verification":
                    return {"candidate_ids": ["DEMO-001"], "ambiguity_reason": "全称匹配", "confirmation_question": "", "recommend_review": False}
                return {"intent_level": "中", "evidence": "想把设计和协同统一起来" if any("想把设计和协同统一起来" in reply for reply in replies) else "",
                        "follow_up_willingness": any("可以让销售联系我" in reply for reply in replies), "reason": "根据客户原话"}

        state = new_session("live", scenario_id="D")
        client = SixFieldClient()
        for reply, expected_goal in (
            ("我们是杭州星桥家居设计有限公司。", "need"),
            ("想把设计和协同统一起来。", "profile"),
            ("主要在杭州经营。", "footprint"),
            ("目前有五家门店，设计师大概十八人。", "consent"),
        ):
            add_reply(state, reply, client)
            self.assertEqual(state["question_goal"], expected_goal)
            self.assertFalse(state["call_ended"])
        add_reply(state, "可以让销售联系我。", client)
        self.assertEqual(len(state["replies"]), 5)
        self.assertEqual(state["status"], "READY")
        self.assertTrue(state["call_ended"])
        self.assertTrue(all(item["status"] == "confirmed" for item in state["fields"].values()))
        self.assertIsNotNone(state["ai_sql"])

    def test_six_round_cap_and_consent_is_asked_in_time(self):
        class LimitedClient:
            def parallel_peer(self):
                return None

            def run(self, name, payload, log):
                if name == "dialogue":
                    replies = payload["replies"]
                    extracted = {"customer_need": "想把团队协作统一起来"}
                    evidence = {"customer_need": "想把团队协作统一起来"}
                    for field_name, marker, value in (("industry", "主要做家居设计", "家居设计"), ("region", "主要在杭州经营", "杭州")):
                        if any(marker in reply for reply in replies):
                            extracted[field_name], evidence[field_name] = value, marker
                    return {"question_goal": "current_workflow", "next_question": "现在团队怎么处理？", "stop_decision": "continue",
                            "extracted_fields": extracted, "evidence": evidence, "missing_fields": []}
                if name == "verification":
                    return {"candidate_ids": [], "ambiguity_reason": "未报企业名称", "confirmation_question": "", "recommend_review": True}
                return {"intent_level": "中", "evidence": "想把团队协作统一起来", "follow_up_willingness": False, "reason": "有需求"}

        state = new_session("live", scenario_id="D")
        client = LimitedClient()
        for reply in ("想把团队协作统一起来。", "主要做家居设计。", "主要在杭州经营。", "目前有五家门店。"):
            add_reply(state, reply, client)
        self.assertEqual(state["question_goal"], "consent")
        self.assertIn("联系", state["next_question"])
        add_reply(state, "可以让销售联系我。", client)
        self.assertFalse(state["call_ended"])
        self.assertEqual(state["question_goal"], "footprint")
        self.assertIn("设计师", state["next_question"])
        self.assertNotIn("门店", state["next_question"])
        add_reply(state, "设计师大概十八人。", client)
        self.assertEqual(len(state["replies"]), 6)
        self.assertEqual(state["status"], "REVIEW")
        self.assertTrue(state["call_ended"])
        with self.assertRaisesRegex(ValueError, "已结束"):
            add_reply(state, "第七轮", client)
        with self.assertRaisesRegex(ValueError, "6 轮上限"):
            review(state, "return_to_dialogue")


if __name__ == "__main__":
    unittest.main()
