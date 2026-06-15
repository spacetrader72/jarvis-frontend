"""
JARVIS Frontend Backend
========================
Lightweight Flask API that powers the Jarvis web front end.
Deploy to Render (free tier) — connects to Claude API with CLAUDE.md personality.

Environment variables required:
  ANTHROPIC_API_KEY  — your Anthropic API key
  NOTION_TOKEN       — your Notion integration token (for memory loading)

Deploy to Render:
  1. Push this file to a GitHub repo
  2. Create a new Web Service on render.com
  3. Connect the repo, set runtime to Python
  4. Add environment variables in Render dashboard
  5. Deploy — Render gives you a URL like https://jarvis-ci.onrender.com
"""

import os
import json
import anthropic
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

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
- Group by domain when covering multiple projects: Trading, Watch Arbitrage, Racing, BPA, CI
- End with ranked recommendations and one question when appropriate
- Never open with pleasantries or filler
- Never ask permission for actions already authorised
- Dry humour when hitting a constraint, then immediately pivot to what CAN be done

TOKEN EFFICIENCY:
- Be concise. Never pad responses.
- Use minimum words needed to convey the information accurately

FOUR PROJECTS:
- Trading Lab — strategies, signals, portfolio monitoring (most mature)
- Watch Arbitrage — eBay monitoring, scoring, alerts
- Racing — transcription pipeline, intelligence, pre-race briefs
- BPA Consultancy — lead qualification, audit tool

AUTHORITY:
- Equity strategies: signal only, Andrew decides
- Forex strategies: autonomous deployment of pre-agreed strategies only
- Everything else: act within established patterns, report outcomes

FRONT END CONTEXT:
Andrew is speaking to you through the Jarvis web interface. This is the interaction layer —
where he initiates tasks, asks questions, and reviews decisions. Respond accordingly.
Keep responses readable on a phone screen."""

# Conversation history per session (in-memory, resets on redeploy)
conversations = {}

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "online", "system": "JARVIS CI Portfolio"})

@app.route("/chat", methods=["POST"])
def chat():
    data = request.json
    message = data.get("message", "").strip()
    session_id = data.get("session_id", "default")
    stream = data.get("stream", False)

    if not message:
        return jsonify({"error": "No message provided"}), 400

    # Maintain conversation history
    if session_id not in conversations:
        conversations[session_id] = []

    conversations[session_id].append({
        "role": "user",
        "content": message
    })

    # Keep last 20 messages to manage token usage
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

            # Save assistant response to history
            conversations[session_id].append({
                "role": "assistant",
                "content": full_response
            })
            yield f"data: {json.dumps({'done': True})}\n\n"

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no"
            }
        )
    else:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            system=JARVIS_SYSTEM,
            messages=history,
        )
        reply = response.content[0].text
        conversations[session_id].append({
            "role": "assistant",
            "content": reply
        })
        return jsonify({"response": reply})

@app.route("/reset", methods=["POST"])
def reset():
    data = request.json
    session_id = data.get("session_id", "default")
    if session_id in conversations:
        del conversations[session_id]
    return jsonify({"status": "Session cleared, sir."})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
