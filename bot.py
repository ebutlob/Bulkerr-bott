"""
telegram bulk sender bot — group + admin restricted, Render-ready
commands (groups only, admins only):
  /bulk <message> <count>          repeat <message> <count> times
  /bulk <count>  (as reply)        repeat replied message <count> times
  /mention @username [count]       ping a user
  /all [count]                     ping everyone tracked in this chat
  /all user1,user2 [count]         ping an explicit list
  /stop                            abort running job
  /help                            usage

env:
  BOT_TOKEN   bot token
  ADMIN_IDS   extra user ids allowed (comma-separated) — supergroup/group admins always allowed
  STATE_DIR   path for seen.json (default ./data)
  PORT        health port (default 8080)
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

BOT_TOKEN = os.getenv("BOT_TOKEN", "8694449874:AAF7BilyqATOSud6EZukG-N6OCTwSh0sFhs")
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

EXTRA_ADMINS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
}
STATE_DIR = os.getenv("STATE_DIR", "./data")
SEEN_PATH = os.path.join(STATE_DIR, "seen.json")
PORT = int(os.getenv("PORT", "8080"))

MAX_COUNT = 1000
DELAY = 0.05
PROGRESS_EVERY = 50
MEMBER_CACHE_TTL = 60  # seconds

SEEN: dict = {}
ACTIVE: dict = {}
SEEN_LOCK = threading.Lock()
ACTIVE_LOCK = threading.Lock()
MEMBER_CACHE: dict = {}  # (chat_id, user_id) -> (status, ts)
CACHE_LOCK = threading.Lock()


# ---------- persistence ----------

def load_seen():
    global SEEN
    try:
        with open(SEEN_PATH, "r", encoding="utf-8") as f:
            SEEN = {int(k): {int(uid): u for uid, u in v.items()}
                    for k, v in json.load(f).items()}
    except Exception:
        SEEN = {}


def save_seen():
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with SEEN_LOCK:
            blob = {str(k): {str(uid): u for uid, u in v.items()}
                    for k, v in SEEN.items()}
        tmp = SEEN_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(blob, f)
        os.replace(tmp, SEEN_PATH)
    except Exception as e:
        print("[persist]", e)


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


def is_group_admin(chat_id, user_id):
    if not user_id:
        return False
    if user_id in EXTRA_ADMINS:
        return True
    key = (chat_id, user_id)
    now = time.time()
    with CACHE_LOCK:
        cached = MEMBER_CACHE.get(key)
    if cached and now - cached[1] < MEMBER_CACHE_TTL:
        return cached[0] in ("administrator", "creator")
    resp = call("getChatMember", chat_id=chat_id, user_id=user_id)
    status = resp.get("result", {}).get("status", "left") if resp.get("ok") else "left"
    with CACHE_LOCK:
        MEMBER_CACHE[key] = (status, now)
    return status in ("administrator", "creator")


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


# ---------- job runner ----------

def _run_bulk(chat_id, text, count, parse_mode, status_id, stop_evt, label):
    sent = 0
    first_err = None
    last_report = 0
    for i in range(count):
        if stop_evt.is_set():
            break
        resp = send(chat_id, text, parse_mode=parse_mode)
        if resp.get("ok"):
            sent += 1
        else:
            if first_err is None:
                first_err = resp.get("description", "unknown error")
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
        tail = f" — {sent}/{count}"
        if first_err:
            tail += f"\nfirst error: {first_err}"
        edit(chat_id, status_id, tag + tail)


def start_job(chat_id, text, count, parse_mode, label="bulk"):
    with ACTIVE_LOCK:
        if chat_id in ACTIVE:
            send(chat_id, "job already running here — /stop first")
            return
        stop_evt = threading.Event()
        ACTIVE[chat_id] = stop_evt
    status = send(chat_id, f"{label}: sending {count}x…")
    status_id = status["result"]["message_id"] if status.get("ok") else None
    threading.Thread(
        target=_run_bulk,
        args=(chat_id, text, count, parse_mode, status_id, stop_evt, label),
        daemon=True,
    ).start()


# ---------- auth gate ----------

def gate(chat_id, user_id, chat_type):
    if chat_type not in ("group", "supergroup"):
        send(chat_id, "this bot only works in groups")
        return False
    if not is_group_admin(chat_id, user_id):
        send(chat_id, "admins only")
        return False
    return True


# ---------- command parsing ----------

def split_cmd(text, bot_username):
    """Return (command_name, rest) stripping /cmd@Bot suffix."""
    if not text.startswith("/"):
        return None, ""
    head, _, tail = text.partition(" ")
    head = head[1:]
    if "@" in head:
        name, _, mention = head.partition("@")
        if mention.lower() != (bot_username or "").lower():
            return None, ""
        head = name
    return head.lower(), tail.strip()


def reply_text_of(msg):
    r = msg.get("reply_to_message") or {}
    return r.get("text", "")


# ---------- commands ----------

def cmd_help(chat_id):
    send(
        chat_id,
        "commands (admins only, groups only):\n"
        "/bulk <message> <count>       — repeat message\n"
        "/bulk <count>  (as reply)      — repeat replied message\n"
        "/mention @user [count]         — ping a user\n"
        "/all [count]                   — ping everyone tracked here\n"
        "/all user1,user2 [count]       — ping explicit list\n"
        "/stop                          — abort running job\n"
        "/help                          — this message",
    )


def cmd_bulk(chat_id, body, replied):
    if replied:
        if not body.isdigit():
            send(chat_id, "usage (reply): /bulk <count>")
            return
        count, target = int(body), replied
    else:
        try:
            target, c = body.rsplit(" ", 1)
            count = int(c)
        except ValueError:
            send(chat_id, "usage: /bulk <message> <count>")
            return
        if not target:
            send(chat_id, "empty message")
            return
    start_job(chat_id, target, max(1, min(count, MAX_COUNT)), None, label="bulk")


def cmd_mention(chat_id, body):
    parts = body.split()
    if not parts:
        send(chat_id, "usage: /mention @username [count]")
        return
    handle = parts[0] if parts[0].startswith("@") else "@" + parts[0]
    try:
        count = int(parts[1]) if len(parts) > 1 else 1
    except ValueError:
        count = 1
    start_job(chat_id, handle, max(1, min(count, MAX_COUNT)), None, label=f"mention {handle}")


def cmd_all(chat_id, body):
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
        label = f"all ({len(tags)})"
    else:
        if not users:
            send(chat_id, "no users tracked yet — someone needs to chat first")
            return
        tags = [mention_of(u) for u in users]
        text = " ".join(tags)
        parse_mode = "HTML" if "<a " in text else None
        label = f"all ({len(tags)})"

    start_job(chat_id, text, count, parse_mode, label=label)


def cmd_stop(chat_id):
    with ACTIVE_LOCK:
        evt = ACTIVE.get(chat_id)
    if not evt:
        send(chat_id, "nothing running")
        return
    evt.set()
    send(chat_id, "stop requested")


# ---------- dispatch ----------

BOT_USERNAME = ""  # fetched at startup


def handle_message(msg):
    text = msg.get("text", "")
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    chat_type = chat.get("type")
    user = msg.get("from") or {}
    user_id = user.get("id")

    if chat_type in ("group", "supergroup"):
        track(chat_id, user)

    cmd, body = split_cmd(text, BOT_USERNAME)
    if not cmd:
        return
    if not gate(chat_id, user_id, chat_type):
        return

    replied = reply_text_of(msg)
    if cmd in ("start", "help"):
        cmd_help(chat_id)
    elif cmd == "stop":
        cmd_stop(chat_id)
    elif cmd == "bulk":
        cmd_bulk(chat_id, body, replied)
    elif cmd == "mention":
        cmd_mention(chat_id, body)
    elif cmd == "all":
        cmd_all(chat_id, body)


# ---------- health ----------

class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        b = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


def start_health():
    srv = HTTPServer(("0.0.0.0", PORT), Health)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[health] :{PORT}")


# ---------- main ----------

def main():
    global BOT_USERNAME
    load_seen()
    start_health()
    call("deleteWebhook")

    me = call("getMe")
    if me.get("ok"):
        BOT_USERNAME = me["result"].get("username", "")
        print(f"[bot] @{BOT_USERNAME}")

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
                        print("[dispatch]", e)
        except KeyboardInterrupt:
            return
        except Exception as e:
            print("[loop]", e)
            time.sleep(2)


if __name__ == "__main__":
    main()
