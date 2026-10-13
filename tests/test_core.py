import unittest

from server.core import evaluate, run_scenario, score_aql, search_companies


class PhaseOneTests(unittest.TestCase):
    def test_a_ready_and_evidenced(self):
        result = run_scenario("A")
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["score"]["score"], 68)
        self.assertEqual(result["score"]["method"], "simulated")
        self.assertIsNotNone(result["ai_sql"])
        for item in result["fields"].values():
            self.assertEqual(item["status"], "confirmed")
            self.assertTrue(item["source"] and item["evidence"])

    def test_b_ambiguous_and_no_sql(self):
        result = run_scenario("B")
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual({c["id"] for c in result["candidates"]}, {"DEMO-001", "DEMO-002"})
        self.assertEqual(result["fields"]["company_name"]["status"], "pending")
        self.assertEqual(result["fields"]["store_count"]["value"], 3)
        self.assertIsNone(result["ai_sql"])

    def test_c_nurture(self):
        result = run_scenario("C")
        self.assertEqual(result["status"], "NURTURE")
        self.assertEqual(result["intent"]["level"], "低")
        self.assertIsNone(result["ai_sql"])

    def test_unknown_name_stays_unknown(self):
        result = evaluate({}, ["我们是从未收录的企业，想看看方案，可以让销售联系我。"])
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["fields"]["company_name"]["status"], "pending")
        self.assertEqual(result["candidates"], [])

    def test_incomplete_behavior_has_no_precise_score(self):
        self.assertIsNone(score_aql({"source": "AI 外呼"})["score"])

    def test_region_alone_does_not_confirm_company(self):
        self.assertEqual(search_companies("不存在", "杭州"), [])

    def test_exact_name_search_is_unique(self):
        self.assertEqual([c["id"] for c in search_companies("杭州星桥家居设计有限公司")], ["DEMO-001"])


if __name__ == "__main__":
    unittest.main()
