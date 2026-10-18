"""
telegram bulk sender bot — Render-ready
commands:
  /bulk <message> <count>          send <message> <count> times here
  /bulk <count>  (as reply)        repeat replied message <count> times
  /mention @username [count]       ping a user (default 1x)
  /all [count]                     ping every user seen in this chat (default 1x)
  /all user1,user2,... [count]     ping a specific list
  /stop                            abort any running job in this chat
  /help  /start                    show usage

env:
  BOT_TOKEN     bot token (required, has fallback baked in)
  ADMIN_IDS     comma-separated user ids allowed to use commands (empty = anyone)
  STATE_DIR     where seen.json lives (default ./data)
  PORT          health-check port for Render (default 8080)
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

BOT_TOKEN = os.getenv("BOT_TOKEN", "8694449874:AAGgokB7g9U5NWbxra0G8C798knfMGronFk")
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
}
STATE_DIR = os.getenv("STATE_DIR", "./data")
SEEN_PATH = os.path.join(STATE_DIR, "seen.json")
PORT = int(os.getenv("PORT", "8080"))

MAX_COUNT = 1000
DELAY = 0.05
PROGRESS_EVERY = 50

# chat_id -> {user_id(str): user_dict}
SEEN: dict = {}
# chat_id -> threading.Event  (stop flag for active job)
ACTIVE: dict = {}
SEEN_LOCK = threading.Lock()
ACTIVE_LOCK = threading.Lock()


# ---------- persistence ----------

def load_seen():
    global SEEN
    try:
        with open(SEEN_PATH, "r", encoding="utf-8") as f:
            SEEN = {int(k): {int(uid): u for uid, u in v.items()} for k, v in json.load(f).items()}
    except Exception:
        SEEN = {}


def save_seen():
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with SEEN_LOCK:
            blob = {str(k): {str(uid): u for uid, u in v.items()} for k, v in SEEN.items()}
        tmp = SEEN_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(blob, f)
        os.replace(tmp, SEEN_PATH)
    except Exception as e:
        print("[persist] err:", e)


# ---------- telegram api ----------

def call(method, **params):
    try:
        r = requests.post(f"{API}/{method}", json=params, timeout=30)
        return r.json()
    except Exception as e:
        return {"ok": False, "description": str(e)}


def send(chat_id, text, **kw):
    return call("sendMessage", chat_id=chat_id, text=text, **kw)


def edit(chat_id, message_id, text, **kw):
    return call("editMessageText", chat_id=chat_id, message_id=message_id, text=text, **kw)


def get_updates(offset=None, timeout=30):
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    try:
        r = requests.get(f"{API}/getUpdates", params=params, timeout=timeout + 30)
        return r.json()
    except Exception as e:
        return {"ok": False, "result": [], "description": str(e)}


# ---------- tracker ----------

def track(chat_id, user):
    if not user or user.get("is_bot"):
        return
    with SEEN_LOCK:
        SEEN.setdefault(chat_id, {})[user["id"]] = user
    save_seen()


def mention_of(user):
    if user.get("username"):
        return f"@{user['username']}"
    return f'<a href="tg://user?id={user["id"]}">{user.get("first_name", "user")}</a>'


# ---------- job execution ----------

def _run_bulk(chat_id, text, count, parse_mode, status_id, stop_evt, label="bulk"):
    sent = 0
    last_report = 0
    for i in range(count):
        if stop_evt.is_set():
            break
        resp = send(chat_id, text, parse_mode=parse_mode)
        if resp.get("ok"):
            sent += 1
        else:
            retry = resp.get("parameters", {}).get("retry_after")
            if retry:
                time.sleep(retry)
                again = send(chat_id, text, parse_mode=parse_mode)
                if again.get("ok"):
                    sent += 1
        if status_id and (i + 1 - last_report) >= PROGRESS_EVERY:
            last_report = i + 1
            edit(chat_id, status_id, f"{label}: {sent}/{count}…")
        time.sleep(DELAY)

    with ACTIVE_LOCK:
        ACTIVE.pop(chat_id, None)
    if status_id:
        tag = "stopped" if stop_evt.is_set() else "done"
        edit(chat_id, status_id, f"{tag} — {sent}/{count}")


def start_job(chat_id, text, count, parse_mode, label="bulk"):
    with ACTIVE_LOCK:
        if chat_id in ACTIVE:
            send(chat_id, "a job is already running here — /stop first")
            return
        stop_evt = threading.Event()
        ACTIVE[chat_id] = stop_evt

    status = send(chat_id, f"{label}: sending {count}x…")
    status_id = status["result"]["message_id"] if status.get("ok") else None

    t = threading.Thread(
        target=_run_bulk,
        args=(chat_id, text, count, parse_mode, status_id, stop_evt, label),
        daemon=True,
    )
    t.start()


# ---------- commands ----------

def require_admin(chat_id, user_id):
    if not ADMIN_IDS:
        return True
    if user_id in ADMIN_IDS:
        return True
    send(chat_id, "not authorized")
    return False


def cmd_help(chat_id):
    send(
        chat_id,
        "commands:\n"
        "/bulk <message> <count>       — repeat <message> <count> times\n"
        "/bulk <count>  (as reply)      — repeat replied message <count> times\n"
        "/mention @user [count]         — ping a user\n"
        "/all [count]                   — ping everyone tracked in this chat\n"
        "/all user1,user2 [count]       — ping an explicit list\n"
        "/stop                          — abort running job\n"
        "/help                          — this message",
    )


def cmd_bulk(chat_id, user_id, body, replied_text):
    if not require_admin(chat_id, user_id):
        return
    if replied_text:
        if not body:
            send(chat_id, "usage (reply): /bulk <count>")
            return
        try:
            count = int(body)
        except ValueError:
            send(chat_id, "count must be an integer")
            return
        target = replied_text
    else:
        if not body:
            send(chat_id, "usage: /bulk <message> <count>")
            return
        try:
            target, count_s = body.rsplit(" ", 1)
            count = int(count_s)
        except ValueError:
            send(chat_id, "usage: /bulk <message> <count>")
            return
    count = max(1, min(count, MAX_COUNT))
    start_job(chat_id, target, count, None, label="bulk")


def cmd_mention(chat_id, user_id, body):
    if not require_admin(chat_id, user_id):
        return
    parts = body.split()
    if not parts:
        send(chat_id, "usage: /mention @username [count]")
        return
    handle = parts[0] if parts[0].startswith("@") else "@" + parts[0]
    try:
        count = int(parts[1]) if len(parts) > 1 else 1
    except ValueError:
        count = 1
    count = max(1, min(count, MAX_COUNT))
    start_job(chat_id, handle, count, None, label=f"mention {handle}")


def cmd_all(chat_id, user_id, body):
    if not require_admin(chat_id, user_id):
        return
    with SEEN_LOCK:
        users = list(SEEN.get(chat_id, {}).values())
    tokens = body.split() if body else []
    count, explicit = 1, []
    if tokens:
        try:
            count = int(tokens[-1])
            tokens = tokens[:-1]
        except ValueError:
            pass
        if tokens:
            explicit = tokens
    count = max(1, min(count, MAX_COUNT))

    if explicit:
        tags = [t if t.startswith("@") else "@" + t for t in explicit]
        text, parse_mode = " ".join(tags), None
    else:
        if not users:
            send(chat_id, "no users tracked yet — they need to send a message first")
            return
        # skip the bot itself
        tags = [mention_of(u) for u in users if u.get("username") != "bot"]
        text = " ".join(tags)
        parse_mode = "HTML" if "<a " in text else None

    start_job(chat_id, text, count, parse_mode, label=f"all ({len(tags)} users)")


def cmd_stop(chat_id):
    with ACTIVE_LOCK:
        evt = ACTIVE.get(chat_id)
    if not evt:
        send(chat_id, "nothing running")
        return
    evt.set()
    send(chat_id, "stop requested")


# ---------- dispatch ----------

def handle_message(msg):
    text = msg.get("text", "")
    chat_id = msg["chat"]["id"]
    user = msg.get("from", {})
    user_id = user.get("id")

    track(chat_id, user)

    replied = msg.get("reply_to_message")
    replied_text = (replied or {}).get("text", "")

    def after(prefix):
        return text[len(prefix):].strip()

    if text.startswith("/start") or text.startswith("/help"):
        cmd_help(chat_id)
    elif text.startswith("/stop"):
        cmd_stop(chat_id)
    elif text.startswith("/bulk"):
        cmd_bulk(chat_id, user_id, after("/bulk"), replied_text)
    elif text.startswith("/mention"):
        cmd_mention(chat_id, user_id, after("/mention"))
    elif text.startswith("/all"):
        cmd_all(chat_id, user_id, after("/all"))


# ---------- health server (Render keepalive) ----------

class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def start_health():
    srv = HTTPServer(("0.0.0.0", PORT), Health)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[health] listening on :{PORT}")


# ---------- main loop ----------

def main():
    load_seen()
    start_health()
    # clear any stuck webhook so getUpdates works
    call("deleteWebhook")
    print("[bot] starting polling…")
    offset = None
    while True:
        try:
            data = get_updates(offset)
            if not data.get("ok"):
                time.sleep(3)
                continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                if "message" in upd:
                    try:
                        handle_message(upd["message"])
                    except Exception as e:
                        print("[dispatch] err:", e)
        except KeyboardInterrupt:
            print("[bot] bye")
            return
        except Exception as e:
            print("[loop] err:", e)
            time.sleep(2)


if __name__ == "__main__":
    main()
