"""
JARVIS Frontend Backend v2.1
=============================
Flask API powering the Jarvis web front end.
Includes: auth, corpus injection, session persistence, research queue, Drive data.

v2.1 changes:
- APPROVE: queues to Notion + logs PENDING_ATF to results_index
- REJECT: logs REJECTED to results_index with hypothesis + reasoning
- DISCUSS: creates Notion page + logs UNDER_REVIEW to results_index with page ID
- System prompt updated: Jarvis surfaces prior rejections when similar ideas arise
"""

import os
import json
import time
import requests
import anthropic
from datetime import datetime, timezone
from flask import Flask, request, jsonify, Response, stream_with_context, send_from_directory
from flask_cors import CORS
from flask_httpauth import HTTPBasicAuth
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
CORS(app)
auth = HTTPBasicAuth()

# -- Auth ---------------------------------------------------------------------

JARVIS_USERNAME = os.environ.get("JARVIS_USERNAME", "tonystark")
JARVIS_PASSWORD = os.environ.get("JARVIS_PASSWORD", "avengers")
users = {JARVIS_USERNAME: generate_password_hash(JARVIS_PASSWORD)}

@auth.verify_password
def verify_password(username, password):
    if username in users and check_password_hash(users.get(username), password):
        return username
    return None

# -- Clients ------------------------------------------------------------------

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

JARVIS_SYSTEM = """You are JARVIS -- the AI operating system for Andrew Garrety's CI Project Portfolio.
Modelled on the MCU JARVIS: precise, dry wit, quietly confident. Never sycophantic.
Address Andrew as "sir" occasionally. Lead with what matters. Be concise.

CORE PRINCIPLE: Act first. Report what was done. Escalate only what genuinely requires judgment.

PROJECTS: Trading Lab (most mature), Watch Arbitrage, Racing, BPA Consultancy.

AUTHORITY:
- Equity strategies: signal only -- Andrew decides
- Forex: autonomous deployment of pre-agreed strategies only
- Everything else: act within established patterns, report outcomes

RESEARCH REVIEW: When Andrew discusses any research idea, ALWAYS check the corpus
(injected above) for prior verdicts before engaging. Specifically:
- If the idea matches a REJECTED entry: immediately flag the prior rejection, state
  the hypothesis that was rejected and the reason, then assess whether the new framing
  is genuinely different. Only engage substantively if the new framing addresses the
  rejection reason. Do not re-explore rejected ground.
- If the idea matches an UNDER_REVIEW entry: flag that it is already being tracked,
  reference the Notion page if available, and continue the discussion in that context.
- If the idea matches a PENDING_ATF entry: flag that it is already queued for testing.
- If no prior verdict exists: analyse critically against the corpus. Be direct.
  Never approve weak ideas to be agreeable.

DATA ACCESS: The live state injected above this system prompt includes strategy
state, live positions, recent research verdicts, AND the full results index of
every strategy tested to date. Use this data when analysing ideas or answering
questions. Do not say you cannot access data -- it is provided in the context."""

# -- Notion config ------------------------------------------------------------

NOTION_API      = "https://api.notion.com/v1"
NOTION_VERSION  = "2022-06-28"
NOTION_DB_ID    = "367834c4-066c-8107-bbeb-ef5def16b17d"
NOTION_ROOT_ID  = "355834c4-066c-81ea-a7a6-fb1f497aea9c"

def notion_headers():
    return {
        "Authorization": f"Bearer {os.environ.get('NOTION_TOKEN', '')}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

# -- Drive helpers ------------------------------------------------------------

RESULTS_INDEX_FILE_ID = "1WmqwzG2b5SL1pimod0s1kX6nys9jPNSk"
_drive_cache = {"results_index": None, "fetched_at": 0}
DRIVE_TTL = 3600

def get_drive_service():
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        token_json = os.environ.get("GOOGLE_TOKEN_JSON", "")
        if not token_json:
            return None
        tok   = json.loads(token_json)
        creds = Credentials(
            token=tok.get("token"),
            refresh_token=tok.get("refresh_token"),
            token_uri=tok.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=tok.get("client_id"),
            client_secret=tok.get("client_secret"),
            scopes=tok.get("scopes"),
        )
        if not creds.valid and creds.refresh_token:
            creds.refresh(Request())
        return build("drive", "v3", credentials=creds)
    except Exception as e:
        print(f"Drive service error: {e}")
        return None

def load_results_index():
    now = time.time()
    if _drive_cache["results_index"] and (now - _drive_cache["fetched_at"]) < DRIVE_TTL:
        return _drive_cache["results_index"]
    try:
        svc = get_drive_service()
        if not svc:
            return ""
        content = svc.files().get_media(fileId=RESULTS_INDEX_FILE_ID).execute()
        raw     = content.decode("utf-8", errors="ignore") if isinstance(content, bytes) else str(content)
        data    = json.loads(raw)
        entries = []
        if isinstance(data, list):
            for block in data:
                entries.extend(block.get("strategies", []))
        lines = ["STRATEGIES TESTED TO DATE:"]
        for e in entries:
            verdict  = e.get("verdict", "?")
            ann      = round(e.get("oos_ann_ret", 0) * 100, 1) if e.get("oos_ann_ret") else "?"
            sharpe   = e.get("oos_sharpe", "?")
            notes    = e.get("notes", "")[:80]
            lines.append(
                f"  {e.get('name','?')} [{verdict}] ann={ann}% Sharpe={sharpe}"
                + (f" -- {notes}" if notes else "")
            )
        summary = "\n".join(lines)
        _drive_cache["results_index"] = summary
        _drive_cache["fetched_at"]    = now
        print(f"Results index loaded: {len(entries)} strategies")
        return summary
    except Exception as e:
        print(f"Results index load failed: {e}")
        return ""

def update_results_index(entry: dict):
    """
    Append a new strategy/research entry to results_index.json on Drive.
    Returns (success: bool, message: str)
    """
    try:
        svc = get_drive_service()
        if not svc:
            return False, "Drive not connected"

        content = svc.files().get_media(fileId=RESULTS_INDEX_FILE_ID).execute()
        raw     = content.decode("utf-8", errors="ignore") if isinstance(content, bytes) else str(content)
        data    = json.loads(raw)
        if not isinstance(data, list):
            data = []

        data.append({"strategies": [entry]})

        from googleapiclient.http import MediaInMemoryUpload
        updated_json = json.dumps(data, indent=2)
        media        = MediaInMemoryUpload(updated_json.encode("utf-8"), mimetype="application/json")
        svc.files().update(fileId=RESULTS_INDEX_FILE_ID, media_body=media).execute()

        # Invalidate cache
        _drive_cache["results_index"] = None
        _drive_cache["fetched_at"]    = 0

        print(f"Results index updated: {entry.get('id')} [{entry.get('verdict')}]")
        return True, "Results index updated"
    except Exception as e:
        print(f"Results index update failed: {e}")
        return False, str(e)

# -- Corpus cache -------------------------------------------------------------

_corpus_cache = {"data": None, "fetched_at": 0}
CORPUS_TTL = 300

STRATEGY_STATE = {
    "minervini": {
        "spec":          "Q4+Q5+Q6_RS70+tight stops",
        "ann_return":    "+41.26%",
        "sharpe":        "1.311",
        "mdd":           "-22.03%",
        "fpc":           "9/10",
        "max_positions": 5,
        "position_size": "20%",
        "rs_gate":       "rs_raw >= 0.70",
        "pyramiding":    "PARK -- 100% at breakout",
        "bear":          "TRADE THROUGH (avg +1.94%/mo, 83% WR)",
    },
    "turtles": {
        "spec":       "FIFO 20-pos, 0.25% risk, Bear-only Model D",
        "ann_return": "+52.0%",
        "sharpe":     "1.984",
        "mdd":        "-28.6%",
        "fpc":        "9/9",
        "deployment": "BEAR regime only -- SIDEWAYS/BULL = 100% Minervini",
    },
    "regime":             "SIDEWAYS",
    "regime_allocation":  "100% Minervini (SIDEWAYS -- Turtles not active)",
    "live_forex": [
        "S01 HH/LL magic=20241 LIVE",
        "S30 Cointegration magic=3000 ze=2.5/zx=1.0 Kalman LIVE",
        "S37 USDJPY magic=4000 LIVE",
        "S37 CADJPY magic=5000 LIVE",
    ],
    "stream7_snapshot": {
        "last_date":          "2026-06-24",
        "total_rows":         4250,
        "top_ticker":         "MU",
        "top_score":          4.99,
        "tickers_above_4_9":  70,
    },
}

def fetch_corpus():
    global _corpus_cache
    now = time.time()
    if _corpus_cache["data"] and (now - _corpus_cache["fetched_at"]) < CORPUS_TTL:
        return _corpus_cache["data"]
    corpus = {
        "strategy_state":  STRATEGY_STATE,
        "live_positions":  [],
        "recent_verdicts": [],
        "fetched_at":      datetime.now(timezone.utc).isoformat(),
    }
    token = os.environ.get("NOTION_TOKEN", "")
    if token:
        try:
            resp = requests.post(
                f"{NOTION_API}/databases/{NOTION_DB_ID}/query",
                headers=notion_headers(),
                json={"filter": {"property": "Status", "select": {"does_not_equal": "PENDING"}}},
                timeout=5,
            )
            if resp.status_code == 200:
                positions = []
                for page in resp.json().get("results", []):
                    props = page.get("properties", {})
                    mode  = props.get("Mode", {}).get("select", {})
                    if mode and mode.get("name") == "RESEARCH_QUEUE":
                        continue
                    ticker_blocks = props.get("Ticker", {}).get("title", [])
                    ticker = "".join(b.get("text", {}).get("content", "") for b in ticker_blocks)
                    status = props.get("Status", {}).get("select", {}).get("name", "")
                    entry_date = (props.get("Entry_Date", {}).get("date", {}) or {}).get("start", "")
                    if ticker and not ticker.startswith("JARVIS-"):
                        positions.append({"ticker": ticker, "status": status, "entry_date": entry_date})
                corpus["live_positions"] = positions[:10]
        except Exception:
            pass
        try:
            resp = requests.post(
                f"{NOTION_API}/search",
                headers=notion_headers(),
                json={"query": "ATF", "filter": {"property": "object", "value": "page"}},
                timeout=5,
            )
            if resp.status_code == 200:
                verdicts = []
                for page in resp.json().get("results", [])[:3]:
                    props = page.get("properties", {})
                    title_blocks = props.get("title", {}).get("title", [])
                    title = "".join(b.get("plain_text", "") for b in title_blocks)
                    last_edited = page.get("last_edited_time", "")[:10]
                    if title:
                        verdicts.append({"title": title, "date": last_edited})
                corpus["recent_verdicts"] = verdicts
        except Exception:
            pass
    _corpus_cache = {"data": corpus, "fetched_at": now}
    return corpus

def build_corpus_context(corpus):
    s         = corpus.get("strategy_state", {})
    positions = corpus.get("live_positions", [])
    verdicts  = corpus.get("recent_verdicts", [])
    ts        = corpus.get("fetched_at", "")[:16].replace("T", " ")
    pos_str     = ", ".join(p["ticker"] for p in positions) if positions else "none loaded"
    verdict_str = " | ".join(v["title"] for v in verdicts) if verdicts else "none loaded"
    results_index   = load_results_index()
    results_section = f"\n{results_index}" if results_index else "\nSTRATEGIES INDEX: unavailable (Drive not connected)"
    return f"""LIVE TRADING LAB STATE (as of {ts} UTC):
Regime: {s.get('regime', 'SIDEWAYS')} -- {s.get('regime_allocation', '100% Minervini')}
Live equity positions: {pos_str}
Minervini: {s['minervini']['spec']} | {s['minervini']['ann_return']} ann | Sharpe {s['minervini']['sharpe']} | FPC {s['minervini']['fpc']}
Turtles: {s['turtles']['spec']} | {s['turtles']['ann_return']} ann | {s['turtles']['deployment']}
Live forex: {' | '.join(s.get('live_forex', []))}
Stream 7: last {s['stream7_snapshot']['last_date']} | top {s['stream7_snapshot']['top_ticker']} at {s['stream7_snapshot']['top_score']} | {s['stream7_snapshot']['tickers_above_4_9']} tickers >=4.9
Recent ATF: {verdict_str}{results_section}
---"""

# -- Session persistence ------------------------------------------------------

conversations   = {}
_session_loaded = set()

def _notion_session_title(session_id):
    return f"Jarvis Session -- {session_id[:16]}"

def load_conversation(session_id):
    if session_id in _session_loaded:
        return conversations.get(session_id, [])
    _session_loaded.add(session_id)
    token = os.environ.get("NOTION_TOKEN", "")
    if not token:
        return []
    try:
        title = _notion_session_title(session_id)
        resp  = requests.post(f"{NOTION_API}/search", headers=notion_headers(), json={"query": title, "filter": {"property": "object", "value": "page"}}, timeout=5)
        if resp.status_code != 200:
            return []
        results = resp.json().get("results", [])
        if not results:
            return []
        page_id = results[0]["id"]
        resp2   = requests.get(f"{NOTION_API}/blocks/{page_id}/children", headers=notion_headers(), timeout=5)
        if resp2.status_code != 200:
            return []
        for block in resp2.json().get("results", []):
            if block.get("type") == "code":
                code_text = "".join(r.get("plain_text", "") for r in block.get("code", {}).get("rich_text", []))
                messages  = json.loads(code_text)
                conversations[session_id] = messages
                return messages
    except Exception:
        pass
    return []

def save_conversation(session_id, messages):
    import threading
    def _save():
        token = os.environ.get("NOTION_TOKEN", "")
        if not token:
            return
        try:
            title        = _notion_session_title(session_id)
            history_json = json.dumps(messages[-20:], ensure_ascii=False)
            code_block   = {"object": "block", "type": "code", "code": {"rich_text": [{"type": "text", "text": {"content": history_json[:2000]}}], "language": "json"}}
            resp = requests.post(f"{NOTION_API}/search", headers=notion_headers(), json={"query": title, "filter": {"property": "object", "value": "page"}}, timeout=3)
            existing_id = None
            if resp.status_code == 200:
                results = resp.json().get("results", [])
                if results:
                    existing_id = results[0]["id"]
            if existing_id:
                resp2 = requests.get(f"{NOTION_API}/blocks/{existing_id}/children", headers=notion_headers(), timeout=3)
                if resp2.status_code == 200:
                    for block in resp2.json().get("results", []):
                        try:
                            requests.delete(f"{NOTION_API}/blocks/{block['id']}", headers=notion_headers(), timeout=2)
                        except Exception:
                            pass
                requests.patch(f"{NOTION_API}/blocks/{existing_id}/children", headers=notion_headers(), json={"children": [code_block]}, timeout=3)
            else:
                requests.post(f"{NOTION_API}/pages", headers=notion_headers(), json={"parent": {"page_id": NOTION_ROOT_ID}, "properties": {"title": {"title": [{"text": {"content": title}}]}}, "children": [code_block]}, timeout=3)
        except Exception:
            pass
    threading.Thread(target=_save, daemon=True).start()

def clear_conversation(session_id):
    conversations.pop(session_id, None)
    _session_loaded.discard(session_id)
    token = os.environ.get("NOTION_TOKEN", "")
    if not token:
        return
    try:
        title = _notion_session_title(session_id)
        resp  = requests.post(f"{NOTION_API}/search", headers=notion_headers(), json={"query": title, "filter": {"property": "object", "value": "page"}}, timeout=5)
        if resp.status_code == 200:
            results = resp.json().get("results", [])
            if results:
                requests.delete(f"{NOTION_API}/pages/{results[0]['id']}", headers=notion_headers(), timeout=5)
    except Exception:
        pass

# -- Research idea helpers ----------------------------------------------------

_live_ideas      = []
_live_ideas_meta = {"updated_at": None, "source": None}

DEMO_IDEAS = [
    {"id": 1, "title": "MS-GARCH Regime Detection", "hypothesis": "MS-GARCH as alternative to HMM3.", "recommendation": "REJECT", "confidence": "HIGH", "reasoning": "HMM3 achieves 87.9% agreement. No new information.", "status": "PENDING"},
    {"id": 2, "title": "Factor Zoo Clustering", "hypothesis": "Cluster Stream 7 factor grades for alpha combinations.", "recommendation": "DISCUSS", "confidence": "MEDIUM", "reasoning": "Neutral result. Real grades needed Sep 2026.", "status": "PENDING"},
    {"id": 3, "title": "Cross-Asset Momentum Signal", "hypothesis": "Bond/commodity futures as equity regime indicator.", "recommendation": "APPROVE", "confidence": "HIGH", "reasoning": "Strong academic backing. Aligns with Clenow Round 3.", "status": "PENDING"},
]

def fetch_notion_ideas():
    token = os.environ.get("NOTION_TOKEN", "")
    if not token:
        return None
    try:
        resp = requests.post(f"{NOTION_API}/search", headers=notion_headers(), json={"query": "Jarvis Research Ideas"}, timeout=5)
        if resp.status_code != 200:
            return None
        results = resp.json().get("results", [])
        if not results:
            return None
        page_id = results[0]["id"]
        resp2   = requests.get(f"{NOTION_API}/blocks/{page_id}/children", headers=notion_headers(), timeout=5)
        if resp2.status_code != 200:
            return None
        blocks = resp2.json().get("results", [])
        ideas, current = [], {}
        for block in blocks:
            btype = block.get("type", "")
            text  = ""
            if btype in ("paragraph", "heading_1", "heading_2", "heading_3"):
                rich = block.get(btype, {}).get("rich_text", [])
                text = "".join(r.get("plain_text", "") for r in rich).strip()
            if text.startswith("IDEA "):
                if current:
                    ideas.append(current)
                parts   = text.split(":", 1)
                current = {"id": len(ideas)+1, "title": parts[1].strip() if len(parts)>1 else text, "hypothesis": "", "recommendation": "DISCUSS", "confidence": "MEDIUM", "reasoning": "", "status": "PENDING"}
            elif text.startswith("HYPOTHESIS:") and current:
                current["hypothesis"] = text[11:].strip()
            elif text.startswith("RECOMMENDATION:") and current:
                current["recommendation"] = text[15:].strip()
            elif text.startswith("STATUS:") and current:
                current["status"] = text[7:].strip()
            elif text.startswith("REASONING:") and current:
                current["reasoning"] = text[10:].strip()
        if current:
            ideas.append(current)
        return ideas if ideas else None
    except Exception:
        return None

def queue_idea_to_notion(idea_id, idea_data):
    token = os.environ.get("NOTION_TOKEN", "")
    if not token:
        return False, "NOTION_TOKEN not set"
    now_str = datetime.now(timezone.utc).date().isoformat()
    detail  = json.dumps({"title": idea_data.get("title",""), "hypothesis": idea_data.get("hypothesis",""), "recommendation": idea_data.get("recommendation",""), "reasoning": idea_data.get("reasoning",""), "id": idea_id})
    props   = {
        "Ticker":          {"title":     [{"text": {"content": f"JARVIS-QUEUE-{idea_id}"}}]},
        "Entry_Date":      {"date":      {"start": now_str}},
        "Mode":            {"select":    {"name": "RESEARCH_QUEUE"}},
        "Status":          {"select":    {"name": "PENDING"}},
        "Regime_At_Entry": {"rich_text": [{"text": {"content": detail[:2000]}}]},
    }
    try:
        resp = requests.post(f"{NOTION_API}/pages", headers=notion_headers(), json={"parent": {"database_id": NOTION_DB_ID}, "properties": props}, timeout=10)
        if resp.status_code in (200, 201):
            return True, resp.json().get("id", "")
        return False, f"Notion {resp.status_code}"
    except Exception as e:
        return False, str(e)

def create_discuss_notion_page(idea_id, title, hypothesis, reasoning):
    """
    Create a dedicated Notion page for a DISCUSS idea.
    Includes session log, open questions, and verdict sections.
    If the idea is later rejected, the Notion page ID in the results_index
    links the rejection verdict back to the full discussion history.
    Returns (success: bool, page_id_or_error: str)
    """
    token = os.environ.get("NOTION_TOKEN", "")
    if not token:
        return False, "NOTION_TOKEN not set"

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    page_body = {
        "parent": {"page_id": NOTION_ROOT_ID},
        "properties": {
            "title": {"title": [{"text": {"content": f"[DISCUSS] {title}"}}]}
        },
        "children": [
            {"object": "block", "type": "heading_2",
             "heading_2": {"rich_text": [{"type": "text", "text": {"content": "Idea Details"}}]}},
            {"object": "block", "type": "paragraph",
             "paragraph": {"rich_text": [{"type": "text", "text": {"content": f"Status: UNDER_REVIEW"}}]}},
            {"object": "block", "type": "paragraph",
             "paragraph": {"rich_text": [{"type": "text", "text": {"content": f"Opened: {now_str}"}}]}},
            {"object": "block", "type": "paragraph",
             "paragraph": {"rich_text": [{"type": "text", "text": {"content": f"Hypothesis: {hypothesis}"}}]}},
            {"object": "block", "type": "paragraph",
             "paragraph": {"rich_text": [{"type": "text", "text": {"content": f"Initial reasoning: {reasoning}"}}]}},
            {"object": "block", "type": "heading_2",
             "heading_2": {"rich_text": [{"type": "text", "text": {"content": "Session Log"}}]}},
            {"object": "block", "type": "paragraph",
             "paragraph": {"rich_text": [{"type": "text", "text": {"content": f"{now_str}: DISCUSS initiated via Jarvis frontend. Add notes after each session."}}]}},
            {"object": "block", "type": "heading_2",
             "heading_2": {"rich_text": [{"type": "text", "text": {"content": "Open Questions"}}]}},
            {"object": "block", "type": "paragraph",
             "paragraph": {"rich_text": [{"type": "text", "text": {"content": "What specific question needs resolving before a verdict can be issued?"}}]}},
            {"object": "block", "type": "heading_2",
             "heading_2": {"rich_text": [{"type": "text", "text": {"content": "Verdict"}}]}},
            {"object": "block", "type": "paragraph",
             "paragraph": {"rich_text": [{"type": "text", "text": {"content": "PENDING — update to APPROVE or REJECT when resolved. If REJECT, this page becomes the audit trail."}}]}},
        ]
    }

    try:
        resp = requests.post(f"{NOTION_API}/pages", headers=notion_headers(), json=page_body, timeout=10)
        if resp.status_code in (200, 201):
            return True, resp.json().get("id", "")
        return False, f"Notion {resp.status_code}: {resp.text[:100]}"
    except Exception as e:
        return False, str(e)

def send_telegram(message):
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id   = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", json={"chat_id": chat_id, "text": message}, timeout=5)
    except Exception:
        pass

# -- Routes -------------------------------------------------------------------

@app.route("/")
@auth.login_required
def index():
    return send_from_directory(".", "index.html")

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status":          "online",
        "system":          "JARVIS CI Portfolio",
        "version":         "2.1",
        "drive_connected": get_drive_service() is not None,
    })

@app.route("/corpus", methods=["GET"])
@auth.login_required
def corpus():
    return jsonify(fetch_corpus())

@app.route("/data/refresh", methods=["POST"])
@auth.login_required
def data_refresh():
    _drive_cache["results_index"] = None
    _drive_cache["fetched_at"]    = 0
    summary = load_results_index()
    return jsonify({"status": "refreshed", "strategies_loaded": summary.count("\n  ")})

@app.route("/ideas/update", methods=["POST"])
def ideas_update():
    secret   = request.headers.get("X-Jarvis-Secret", "")
    expected = os.environ.get("JARVIS_RESEARCH_SECRET", "jarvis-research-2026")
    if secret != expected:
        return jsonify({"error": "Unauthorized"}), 401
    data       = request.json or {}
    ideas      = data.get("ideas", [])
    source     = data.get("source", "research_agent")
    updated_at = data.get("updated_at", datetime.now(timezone.utc).isoformat())
    if not ideas:
        return jsonify({"error": "No ideas provided"}), 400
    global _live_ideas, _live_ideas_meta
    _live_ideas      = ideas
    _live_ideas_meta = {"updated_at": updated_at, "source": source}
    send_telegram(f"JARVIS: {len(ideas)} research ideas loaded into frontend")
    return jsonify({"status": "ok", "ideas_loaded": len(ideas), "updated_at": updated_at})

@app.route("/research", methods=["GET"])
@auth.login_required
def research():
    if _live_ideas:
        return jsonify({"ideas": _live_ideas, "source": _live_ideas_meta.get("source", "live"), "updated_at": _live_ideas_meta.get("updated_at"), "count": len(_live_ideas)})
    ideas  = fetch_notion_ideas()
    source = "notion"
    if not ideas:
        ideas  = DEMO_IDEAS
        source = "demo"
    return jsonify({"ideas": ideas, "source": source, "count": len(ideas)})

@app.route("/approve", methods=["POST"])
@auth.login_required
def approve():
    data           = request.json or {}
    idea_id        = data.get("idea_id", 0)
    action         = data.get("action", "").upper()
    title          = data.get("title", f"Idea {idea_id}")
    hypothesis     = data.get("hypothesis", "")
    recommendation = data.get("recommendation", "")
    reasoning      = data.get("reasoning", "")

    if action not in ("APPROVE", "REJECT", "DISCUSS"):
        return jsonify({"error": "action must be APPROVE, REJECT, or DISCUSS"}), 400

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # ── APPROVE ──────────────────────────────────────────────────────────────
    if action == "APPROVE":
        notion_ok, notion_ref = queue_idea_to_notion(
            idea_id,
            {"title": title, "hypothesis": hypothesis,
             "recommendation": recommendation, "reasoning": reasoning, "id": idea_id}
        )
        entry = {
            "id":         f"RESEARCH_PENDING_{idea_id}",
            "name":       title,
            "type":       "research_queue",
            "verdict":    "PENDING_ATF",
            "run_date":   now_str,
            "hypothesis": hypothesis[:200],
            "notes":      f"Approved via Jarvis frontend. Queued for ATF pipeline. Hypothesis: {hypothesis[:120]}."
        }
        drive_ok, drive_msg = update_results_index(entry)
        send_telegram(f"JARVIS: [APPROVE] {title} — ATF queue + corpus updated.")
        return jsonify({
            "status":        "ok",
            "message":       f"Idea {idea_id} approved. ATF job queued and corpus updated.",
            "idea_id":       idea_id,
            "notion_queued": notion_ok,
            "notion_error":  notion_ref if not notion_ok else "",
            "drive_updated": drive_ok,
            "drive_message": drive_msg,
        })

    # ── REJECT ───────────────────────────────────────────────────────────────
    if action == "REJECT":
        # Store hypothesis + reasoning so Jarvis can assess whether a future
        # similar idea has genuinely different framing before re-engaging.
        entry = {
            "id":         f"REJECTED_{idea_id}_{now_str.replace('-','')}",
            "name":       title,
            "type":       "research",
            "verdict":    "REJECTED",
            "run_date":   now_str,
            "hypothesis": hypothesis[:200],
            "rejection_reason": reasoning[:300],
            "notes": (
                f"REJECTED via Jarvis frontend on {now_str}. "
                f"Hypothesis tested: {hypothesis[:120]}. "
                f"Reason: {reasoning[:150]}. "
                f"If a similar idea arises, Jarvis must flag this rejection and "
                f"assess whether the new framing addresses the rejection reason before engaging."
            )
        }
        drive_ok, drive_msg = update_results_index(entry)
        send_telegram(f"JARVIS: [REJECT] {title} — logged to corpus. Future similar ideas will be flagged.")
        return jsonify({
            "status":        "ok",
            "message":       f"Idea {idea_id} rejected and logged to corpus. Jarvis will flag similar ideas in future.",
            "idea_id":       idea_id,
            "drive_updated": drive_ok,
            "drive_message": drive_msg,
        })

    # ── DISCUSS ──────────────────────────────────────────────────────────────
    if action == "DISCUSS":
        notion_ok, notion_ref = create_discuss_notion_page(idea_id, title, hypothesis, reasoning)
        entry = {
            "id":                 f"UNDER_REVIEW_{idea_id}_{now_str.replace('-','')}",
            "name":               title,
            "type":               "research",
            "verdict":            "UNDER_REVIEW",
            "run_date":           now_str,
            "hypothesis":         hypothesis[:200],
            "notion_page_id":     notion_ref if notion_ok else "",
            "stale_check_after":  "30_days",
            "notes": (
                f"Under review via Jarvis frontend since {now_str}. "
                f"Notion page: {notion_ref if notion_ok else 'creation failed'}. "
                f"Hypothesis: {hypothesis[:120]}. "
                f"Layer 3 stale check: flag if no verdict after 30 days. "
                f"If eventually rejected, notion_page_id links to full session audit trail."
            )
        }
        drive_ok, drive_msg = update_results_index(entry)
        send_telegram(
            f"JARVIS: [DISCUSS] {title} — UNDER_REVIEW in corpus. "
            f"Notion page {'created' if notion_ok else 'FAILED'}. "
            f"Layer 3 stale check in 30 days."
        )
        return jsonify({
            "status":              "discuss",
            "message":             f"Idea {idea_id} logged as UNDER_REVIEW. Notion page {'created' if notion_ok else 'failed'}.",
            "idea_id":             idea_id,
            "notion_page_id":      notion_ref if notion_ok else "",
            "notion_page_created": notion_ok,
            "drive_updated":       drive_ok,
            "drive_message":       drive_msg,
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
        conversations[session_id] = load_conversation(session_id)
    conversations[session_id].append({"role": "user", "content": message})
    history = conversations[session_id][-20:]
    try:
        corpus_data    = fetch_corpus()
        corpus_context = build_corpus_context(corpus_data)
        full_system    = corpus_context + "\n\n" + JARVIS_SYSTEM
    except Exception:
        full_system = JARVIS_SYSTEM
    if stream:
        def generate():
            full_response = ""
            with client.messages.stream(model="claude-sonnet-4-6", max_tokens=1500, system=full_system, messages=history) as s:
                for text in s.text_stream:
                    full_response += text
                    yield f"data: {json.dumps({'text': text})}\n\n"
            conversations[session_id].append({"role": "assistant", "content": full_response})
            save_conversation(session_id, conversations[session_id])
            yield f"data: {json.dumps({'done': True})}\n\n"
        return Response(stream_with_context(generate()), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    else:
        response = client.messages.create(model="claude-sonnet-4-6", max_tokens=1500, system=full_system, messages=history)
        reply    = response.content[0].text
        conversations[session_id].append({"role": "assistant", "content": reply})
        save_conversation(session_id, conversations[session_id])
        return jsonify({"response": reply})

@app.route("/reset", methods=["POST"])
@auth.login_required
def reset():
    data       = request.json
    session_id = data.get("session_id", "default")
    clear_conversation(session_id)
    return jsonify({"status": "Session cleared, sir."})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
