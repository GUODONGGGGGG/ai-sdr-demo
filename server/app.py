"""Local-only Flask API and demo page."""

from __future__ import annotations

import os
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import timedelta
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, session, url_for

from .core import read_json
from .workflow import add_reply, end_call, new_session, retry_live, review, score_session

ROOT = Path(__file__).resolve().parents[1]
app = Flask(__name__, static_folder=str(ROOT / "web"), static_url_path="/web", template_folder=str(ROOT / "web"))
SESSIONS: dict[str, dict] = {}
PUBLIC_DEMO = os.getenv("PUBLIC_DEMO") == "1"
try:
    DEMO_POST_LIMIT = int(os.getenv("DEMO_POST_LIMIT_PER_HOUR", "60"))
except ValueError as exc:
    raise RuntimeError("DEMO_POST_LIMIT_PER_HOUR 必须是整数") from exc
if not 1 <= DEMO_POST_LIMIT <= 120:
    raise RuntimeError("DEMO_POST_LIMIT_PER_HOUR 必须在 1–120 之间")
if PUBLIC_DEMO:
    if not os.getenv("DEEPSEEK_API_KEY") or len(os.getenv("DEMO_ACCESS_CODE", "")) < 10 or len(os.getenv("DEMO_SESSION_SECRET", "")) < 32:
        raise RuntimeError("公网模式需要 DEEPSEEK_API_KEY、至少 10 位 DEMO_ACCESS_CODE 和至少 32 位 DEMO_SESSION_SECRET")
app.secret_key = os.getenv("DEMO_SESSION_SECRET") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=PUBLIC_DEMO, PERMANENT_SESSION_LIFETIME=timedelta(hours=8))
_limit_lock = threading.Lock()
_api_posts: deque[float] = deque()
_login_attempts: dict[str, deque[float]] = defaultdict(deque)


def _within_limit(events: deque[float], maximum: int, window: int) -> bool:
    current = time.monotonic()
    while events and events[0] <= current - window:
        events.popleft()
    if len(events) >= maximum:
        return False
    events.append(current)
    return True


@app.before_request
def protect_public_demo():
    if not PUBLIC_DEMO or request.path in {"/healthz", "/login"}:
        return None
    if session.get("demo_access") is not True:
        return (jsonify({"error": "请先输入演示访问码"}), 401) if request.path.startswith("/api/") else redirect(url_for("login"))
    if request.method == "POST" and request.path.startswith("/api/"):
        with _limit_lock:
            allowed = _within_limit(_api_posts, DEMO_POST_LIMIT, 3600)
        if not allowed:
            return jsonify({"error": "演示调用已达到每小时上限，请稍后再试"}), 429
    return None


@app.after_request
def no_cache_private_data(response):
    if request.path.startswith("/api/") or request.path == "/login":
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.route("/login", methods=["GET", "POST"])
def login():
    if not PUBLIC_DEMO:
        return redirect(url_for("index"))
    if session.get("demo_access") is True:
        return redirect(url_for("index"))
    invalid = False
    if request.method == "POST":
        identifier = request.remote_addr or "unknown"
        with _limit_lock:
            allowed = _within_limit(_login_attempts[identifier], 10, 900)
        if not allowed:
            return render_template("login.html", rate_limited=True), 429
        submitted = request.form.get("code", "")
        if hmac.compare_digest(submitted, os.environ["DEMO_ACCESS_CODE"]):
            session.clear()
            session["demo_access"] = True
            session.permanent = True
            return redirect(url_for("index"))
        invalid = True
    return render_template("login.html", invalid=invalid)


@app.get("/")
def index():
    return send_from_directory(ROOT / "web", "index.html")


@app.get("/api/bootstrap")
def bootstrap():
    data = read_json("scenarios.json")
    return jsonify({"aql": data["aql"], "behavior_cases": data["behavior_cases"], "scenarios": [{"id": x["id"], "title": x["title"], "replies": x["replies"]} for x in data["scenarios"]], "live_available": bool(os.getenv("DEEPSEEK_API_KEY")), "model": os.getenv("DEEPSEEK_MODEL", "deepseek-flash"), "directory_notice": "本地模拟企业候选库，非真实工商数据"})


def _session(session_id: str):
    return SESSIONS.get(session_id)


@app.post("/api/sessions")
def create_session():
    body = request.get_json(silent=True) or {}
    try:
        state = new_session(body.get("mode", "simulation"), body.get("aql"), body.get("scenario_id", "A"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    SESSIONS[state["id"]] = state
    if state["mode"] == "live":
        score_session(state)
    return jsonify(state), 201


@app.post("/api/sessions/<session_id>/score")
def retry_score(session_id):
    state = _session(session_id)
    if not state:
        return jsonify({"error": "会话不存在"}), 404
    try:
        return jsonify(score_session(state))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/sessions/<session_id>")
def get_session(session_id):
    state = _session(session_id)
    return jsonify(state) if state else (jsonify({"error": "会话不存在"}), 404)


@app.post("/api/sessions/<session_id>/reply")
def post_reply(session_id):
    state = _session(session_id)
    if not state:
        return jsonify({"error": "会话不存在"}), 404
    try:
        return jsonify(add_reply(state, (request.get_json(silent=True) or {}).get("reply", "")))
    except (ValueError, AttributeError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/sessions/<session_id>/end")
def end_session(session_id):
    state = _session(session_id)
    if not state:
        return jsonify({"error": "会话不存在"}), 404
    try:
        return jsonify(end_call(state))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/sessions/<session_id>/script")
def run_script(session_id):
    state = _session(session_id)
    if not state:
        return jsonify({"error": "会话不存在"}), 404
    if state["mode"] != "simulation" or state["scenario_id"] == "D":
        return jsonify({"error": "预设脚本仅用于 A/B/C 模拟运行模式"}), 400
    scenario = next(x for x in read_json("scenarios.json")["scenarios"] if x["id"] == state["scenario_id"])
    replacement = new_session("simulation", state["aql"], state["scenario_id"])
    replacement["id"] = state["id"]
    SESSIONS[session_id] = replacement
    for reply in scenario["replies"]:
        add_reply(replacement, reply)
    return jsonify(replacement)


@app.post("/api/sessions/<session_id>/review")
def post_review(session_id):
    state = _session(session_id)
    if not state:
        return jsonify({"error": "会话不存在"}), 404
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(review(state, body.get("action", ""), body.get("candidate_id", ""), body.get("company_name", ""), body.get("note", "")))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/sessions/<session_id>/retry")
def post_retry(session_id):
    state = _session(session_id)
    if not state:
        return jsonify({"error": "会话不存在"}), 404
    try:
        return jsonify(retry_live(state))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5050")), debug=False)
