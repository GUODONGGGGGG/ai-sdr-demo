import json
import os
import subprocess
import sys
import unittest


class PublicDeployTests(unittest.TestCase):
    def test_public_access_and_global_post_limit(self):
        script = '''
import json
from server.app import app
client = app.test_client()
base = {"base_url": "https://demo.example.com"}
before = client.get("/", **base)
api_before = client.get("/api/bootstrap", **base)
health = client.get("/healthz", **base)
wrong = client.post("/login", data={"code": "wrong"}, **base)
login = client.post("/login", data={"code": "test-access-code-123"}, **base)
directory = client.get("/api/bootstrap", **base)
first = client.post("/api/sessions", json={"mode": "simulation", "scenario_id": "A"}, **base)
second = client.post("/api/sessions", json={"mode": "simulation", "scenario_id": "A"}, **base)
print(json.dumps({"before": before.status_code, "location": before.location, "api_before": api_before.status_code, "health": health.status_code, "wrong": wrong.status_code, "login": login.status_code, "directory": directory.status_code, "company_ids": [item["id"] for item in directory.json["companies"]], "first": first.status_code, "second": second.status_code, "secure_cookie": "Secure" in login.headers.get("Set-Cookie", "")}))
'''
        env = {**os.environ, "PUBLIC_DEMO": "1", "DEEPSEEK_API_KEY": "test-only", "DEMO_ACCESS_CODE": "test-access-code-123", "DEMO_SESSION_SECRET": "a" * 40, "DEMO_POST_LIMIT_PER_HOUR": "1"}
        process = subprocess.run([sys.executable, "-c", script], env=env, text=True, capture_output=True, check=True)
        result = json.loads(process.stdout)
        self.assertEqual((result["before"], result["api_before"], result["health"]), (302, 401, 200))
        self.assertTrue(result["location"].endswith("/login"))
        self.assertEqual((result["wrong"], result["login"]), (200, 302))
        self.assertEqual(result["directory"], 200)
        self.assertEqual(result["company_ids"], ["DEMO-001", "DEMO-002", "DEMO-003"])
        self.assertEqual((result["first"], result["second"]), (201, 429))
        self.assertTrue(result["secure_cookie"])

    def test_public_mode_rejects_missing_secrets(self):
        env = {**os.environ, "PUBLIC_DEMO": "1"}
        for key in ("DEEPSEEK_API_KEY", "DEMO_ACCESS_CODE", "DEMO_SESSION_SECRET"):
            env.pop(key, None)
        process = subprocess.run([sys.executable, "-c", "from server.app import app"], env=env, text=True, capture_output=True)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("公网模式需要", process.stderr)


if __name__ == "__main__":
    unittest.main()
