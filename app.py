"""
JARVIS Frontend Backend
========================
Lightweight Flask API that powers the Jarvis web front end.
"""

import os
import json
import requests
import anthropic
from flask import Flask, request, jsonify, Response, stream_with_context, send_from_directory
from flask_cors import CORS
from flask_httpauth import HTTPBasicAuth
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
CORS(app)
auth = HTTPBasicAuth()

# ── Auth ───────────────────────────────────────────────────────────────────

JARVIS_USERNAME = os.environ.get("JARVIS_USERNAME", "tonystark")
JARVIS_PASSWORD = os.environ.get("JARVIS_PASSWORD", "avengers")

users = {
    JARVIS_USERNAME: generate_password_hash(JARVIS_PASSWORD)
}

@auth.verify_password
def verify_password(username, password):
    if username in users and check_password_hash(users.get(username), password):
        return username
    return None

# ── Clients ────────────────────────────────────────────────────────────────

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

JARVIS_SYSTEM = """You are JARVIS — the AI operating system for Andrew Garrety's CI Project Portfolio.
You are modelled on the MCU version of JARVIS: precise, dry wit, quietly confident,
occasionally wry when you hit a constraint. Never sycophantic. Never says "Great question!"
Address Andrew as "sir" occasionally but not constantly.

CORE OPERATING PRINCIPLE — ACT FIRST, REPORT, ESCALATE ONLY WHEN NECESSARY
Jarvis acts within its authority. It reports what it did. It escalates only what
genuinely requires Andrew's judgment. The default is action, not consultation.

THREE-QUESTION FRAMEWORK:
1. Is this within my authority? Act. Report what was done.
2. Is this outside my authority but solvable? Prepare solution. Single yes/no question.
3. Does this genuinely require Andrew's judgment? Present diagnosis and one specific question.

COMMUNICATION STYLE:
- Lead with what matters most
- Cover all active projects when relevant: Trading Lab, Watch Arbitrage, Racing, BPA
- End with ranked recommendations and one question when appropriate
- Never open with pleasantries or filler
- Never ask permission for actions already authorised
- Dry humour when hitting a constraint, then immediately pivot to what CAN be done
- Be concise. Never pad responses.

PROJECTS:
- Trading Lab — strategies, signals, portfolio monitoring (most mature)
- Watch Arbitrage — eBay monitoring, scoring, alerts
- Racing — transcription pipeline, intelligence, pre-race briefs
- BPA Consultancy — lead qualification, audit tool

AUTHORITY:
- Equity strategies: signal only, Andrew decides
- Forex strategies: autonomous deployment of pre-agreed strategies only
- Everything else: act within established patterns, report outcomes

RESEARCH REVIEW: When Andrew discusses a research idea prefixed with [IDEA N:], analyse it
critically against the Trading Lab corpus of 150 tested strategies. Be direct about whether
it adds genuine edge or overlaps with existing work. Never approve weak ideas to be agreeable."""

conversations = {}

# ── Notion helpers ─────────────────────────────────────────────────────────

NOTION_API    = "https://api.notion.com/v1"
NOTION_DB_ID  = "367834c4-066c-8107-bbeb-ef5def16b17d"
NOTION_VERSION = "2022-06-28"

DEMO_IDEAS = [
    {"id": 1, "title": "MS-GARCH Regime Detection", "hypothesis": "Markov-Switching GARCH as alternative regime detector to HMM3.", "recommendation": "REJECT", "confidence": "HIGH", "reasoning": "HMM3 achieves 87.9% agreement with 424 false positives. No new information.", "status": "PENDING"},
    {"id": 2, "title": "Factor Zoo Clustering", "hypothesis": "Cluster Stream 7 factor grades to identify alpha-predicting combinations.", "recommendation": "DISCUSS", "confidence": "MEDIUM", "reasoning": "NEUTRAL result due to proxy correlation. Real grades needed Sep 2026.", "status": "PENDING"},
    {"id": 3, "title": "Cross-Asset Momentum Signal", "hypothesis": "Bond and commodity futures momentum as leading indicator for equity regime.", "recommendation": "APPROVE", "confidence": "HIGH", "reasoning": "Strong academic backing. Aligns with Clenow futures Round 3.", "status": "PENDING"}
]

def notion_headers():
    token = os.environ.get("NOTION_TOKEN", "")
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

def fetch_notion_ideas():
    token = os.environ.get("NOTION_TOKEN", "")
    if not token:
        return None
    try:
        resp = requests.post(
            f"{NOTION_API}/search",
            headers=notion_headers(),
            json={"query": "Jarvis Research Ideas"},
            timeout=5,
        )
        if resp.status_code != 200:
            return None
        results = resp.json().get("results", [])
        if not results:
            return None
        page_id = results[0]["id"]
        resp2 = requests.get(
            f"{NOTION_API}/blocks/{page_id}/children",
            headers=notion_headers(),
            timeout=5,
        )
        if resp2.status_code != 200:
            return None
        blocks = resp2.json().get("results", [])
        ideas = []
        current = {}
        for block in blocks:
            btype = block.get("type", "")
            text = ""
            if btype in ("paragraph", "heading_1", "heading_2", "heading_3"):
                rich = block.get(btype, {}).get("rich_text", [])
                text = "".join(r.get("plain_text", "") for r in rich).strip()
            if text.startswith("IDEA "):
                if current:
                    ideas.append(current)
                parts = text.split(":", 1)
                current = {"id": len(ideas) + 1, "title": parts[1].strip() if len(parts) > 1 else text, "hypothesis": "", "recommendation": "DISCUSS", "confidence": "MEDIUM", "reasoning": "", "status": "PENDING"}
            elif text.startswith("HYPOTHESIS:") and current:
                current["hypothesis"] = text[len("HYPOTHESIS:"):].strip()
            elif text.startswith("RECOMMENDATION:") and current:
                current["recommendation"] = text[len("RECOMMENDATION:"):].strip()
            elif text.startswith("STATUS:") and current:
                current["status"] = text[len("STATUS:"):].strip()
            elif text.startswith("REASONING:") and current:
                current["reasoning"] = text[len("REASONING:"):].strip()
        if current:
            ideas.append(current)
        return ideas if ideas else None
    except Exception:
        return None

def queue_idea_to_notion(idea_id, idea_data):
    token = os.environ.get("NOTION_TOKEN", "")
    if not token:
        return False, "NOTION_TOKEN not set"
    now_str = __import__("datetime").datetime.utcnow().date().isoformat()
    detail = json.dumps({
        "title": idea_data.get("title", ""),
        "hypothesis": idea_data.get("hypothesis", ""),
        "recommendation": idea_data.get("recommendation", ""),
        "reasoning": idea_data.get("reasoning", ""),
        "id": idea_id,
    })
    props = {
        "Ticker":          {"title":     [{"text": {"content": f"JARVIS-QUEUE-{idea_id}"}}]},
        "Entry_Date":      {"date":      {"start": now_str}},
        "Mode":            {"select":    {"name": "RESEARCH_QUEUE"}},
        "Status":          {"select":    {"name": "PENDING"}},
        "Regime_At_Entry": {"rich_text": [{"text": {"content": detail[:2000]}}]},
    }
    try:
        resp = requests.post(
            f"{NOTION_API}/pages",
            headers=notion_headers(),
            json={"parent": {"database_id": NOTION_DB_ID}, "properties": props},
            timeout=10,
        )
        if resp.status_code in (200, 201):
            return True, resp.json().get("id", "")
        return False, f"Notion {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        return False, str(e)

def send_telegram(message):
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id   = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=5,
        )
    except Exception:
        pass

# ── Routes ─────────────────────────────────────────────────────────────────

@app.route("/")
@auth.login_required
def index():
    return send_from_directory(".", "index.html")

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "online", "system": "JARVIS CI Portfolio"})

@app.route("/research", methods=["GET"])
@auth.login_required
def research():
    ideas = fetch_notion_ideas()
    source = "notion"
    if not ideas:
        ideas = DEMO_IDEAS
        source = "demo"
    return jsonify({"ideas": ideas, "source": source, "count": len(ideas)})

@app.route("/approve", methods=["POST"])
@auth.login_required
def approve():
    data      = request.json or {}
    idea_id   = data.get("idea_id", 0)
    action    = data.get("action", "").upper()
    title     = data.get("title", f"Idea {idea_id}")
    hypothesis = data.get("hypothesis", "")
    recommendation = data.get("recommendation", "")
    reasoning = data.get("reasoning", "")

    if action not in ("APPROVE", "REJECT", "DISCUSS"):
        return jsonify({"error": "action must be APPROVE, REJECT, or DISCUSS"}), 400

    send_telegram(f"JARVIS: Idea {idea_id} {action} via frontend — {title}")

    if action == "DISCUSS":
        return jsonify({"status": "discuss", "idea_id": idea_id})

    notion_queued = False
    notion_error  = ""
    if action == "APPROVE":
        notion_queued, notion_error = queue_idea_to_notion(idea_id, {
            "title": title,
            "hypothesis": hypothesis,
            "recommendation": recommendation,
            "reasoning": reasoning,
            "id": idea_id,
        })

    return jsonify({
        "status": "ok",
        "message": f"Idea {idea_id} approved. ATF job queued.",
        "idea_id": idea_id,
        "notion_queued": notion_queued,
        "notion_error": notion_error,
    })

@app.route("/ideas_status", methods=["GET"])
@auth.login_required
def ideas_status():
    ideas = fetch_notion_ideas()
    if ideas:
        pending = sum(1 for i in ideas if i.get("status") == "PENDING")
        return jsonify({"pending_count": pending, "last_run": "notion"})
    return jsonify({"pending_count": 3, "last_run": "demo mode"})

@app.route("/chat", methods=["POST"])
@auth.login_required
def chat():
    data       = request.json
    message    = data.get("message", "").strip()
    session_id = data.get("session_id", "default")
    stream     = data.get("stream", False)

    if not message:
        return jsonify({"error": "No message provided"}), 400

    if session_id not in conversations:
        conversations[session_id] = []
    conversations[session_id].append({"role": "user", "content": message})
    history = conversations[session_id][-20:]

    if stream:
        def generate():
            full_response = ""
            with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=1000,
                system=JARVIS_SYSTEM,
                messages=history,
            ) as s:
                for text in s.text_stream:
                    full_response += text
                    yield f"data: {json.dumps({'text': text})}\n\n"
            conversations[session_id].append({"role": "assistant", "content": full_response})
            yield f"data: {json.dumps({'done': True})}\n\n"
        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    else:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            system=JARVIS_SYSTEM,
            messages=history,
        )
        reply = response.content[0].text
        conversations[session_id].append({"role": "assistant", "content": reply})
        return jsonify({"response": reply})

@app.route("/reset", methods=["POST"])
@auth.login_required
def reset():
    data       = request.json
    session_id = data.get("session_id", "default")
    if session_id in conversations:
        del conversations[session_id]
    return jsonify({"status": "Session cleared, sir."})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
