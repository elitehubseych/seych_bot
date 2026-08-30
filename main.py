
import json
import threading
import time

import flask
from flask import Flask, request

import db
from config import config
from handlers.bank import handle_bank_event
from handlers.blackjack import handle_bj_event
from handlers.business import handle_message_event as handle_biz_event
from handlers.coin import handle_message_event as handle_game_event
from handlers.coin import schedule_unmute
from handlers.inventory import handle_message_event as handle_inventory_event
from handlers.marriage import handle_message_event as handle_marriage_event
from handlers.messages import handle_message_new
import handlers.commands
import handlers.promo

from handlers.registry import DEAD_SESSION

app = Flask(__name__)

db.init_db()


def _restore_mutes():
    for row in db.get_active_mutes():
        remaining = max(int(row.get("remaining_sec") or 0), 0)
        schedule_unmute(row["peer_id"], row["vk_id"], remaining + 5)


_restore_mutes()


_SEEN_EVENTS = {}
_SEEN_EVENTS_LOCK = threading.Lock()
_EVENT_TTL_SEC = 600
STALE_MESSAGE_SEC = 300


def _mark_event(event_key):
    now = time.monotonic()
    with _SEEN_EVENTS_LOCK:
        for stale in [k for k, t in _SEEN_EVENTS.items() if now - t > _EVENT_TTL_SEC]:
            _SEEN_EVENTS.pop(stale, None)
        if event_key in _SEEN_EVENTS:
            return False
        _SEEN_EVENTS[event_key] = now
        return True


@app.route("/", methods=["POST"])
@app.route("/callback", methods=["POST"])
def callback():
    data = request.get_json(silent=True) or {}
    event_type = data.get("type")

    if event_type == "confirmation":
        return config.CONFIRMATION_TOKEN

    if event_type not in ("message_new", "message_event"):
        return "ok"

    obj = data.get("object") or {}
    msg = obj.get("message", obj)
    fallback_key = "%s:%s:%s" % (
        msg.get("from_id"), msg.get("peer_id"), msg.get("conversation_message_id") or msg.get("id"),
    )
    event_key = data.get("event_id") or fallback_key
    if not _mark_event("%s:%s" % (event_type, event_key)):
        return "ok"

    if event_type == "message_new":
        msg_date = msg.get("date")
        age_sec = time.time() - msg_date if isinstance(msg_date, (int, float)) else 0
        if age_sec > STALE_MESSAGE_SEC:
            return "ok"

        threading.Thread(target=handle_message_new, args=(data,), daemon=True).start()
    else:
        try:
            snackbar = (
                handle_biz_event(data)
                or handle_marriage_event(data)
                or handle_game_event(data)
                or handle_bank_event(data)
                or handle_bj_event(data)
                or handle_inventory_event(data)
            )
        except Exception:
            snackbar = {"type": "show_snackbar", "text": "⚠️ Ошибка, попробуй ещё раз"}

        if snackbar is DEAD_SESSION:
            return flask.Response(
                json.dumps({"type": "show_snackbar", "text": "🕰 Кнопка устарела"}),
                mimetype="application/json",
            )
        if snackbar:
            return flask.Response(json.dumps(snackbar), mimetype="application/json")
        return "ok"

    return "ok"


@app.route("/ping", methods=["GET"])
def ping():
    return "ok"


@app.route("/api/profile/<int:user_id>", methods=["GET"])
def api_profile(user_id):
    try:
        user = db.get_user_readonly(user_id)
        if not user:
            return flask.jsonify({"error": "not found"}), 404
        return flask.jsonify({
            "vk_id": user.get("vk_id"),
            "balance": user.get("balance", 0),
            "credit_rating": user.get("credit_rating", 0),
            "nickname": user.get("nickname"),
            "wins": user.get("wins", 0),
            "games": user.get("games", 0),
        })
    except Exception:
        return flask.jsonify({"error": "server"}), 500


@app.route("/api/inventory/<int:user_id>", methods=["GET"])
def api_inventory(user_id):
    try:
        peer_id = flask.request.args.get("peer_id", type=int)
        items = db.get_inventory(user_id, peer_id=peer_id) if hasattr(db, 'get_inventory') else []
        return flask.jsonify({"items": items})
    except Exception:
        return flask.jsonify({"items": []})


@app.route("/api/business/<int:user_id>", methods=["GET"])
def api_business(user_id):
    try:
        peer_id = flask.request.args.get("peer_id", type=int)
        businesses = []
        if hasattr(db, 'get_chat_businesses') and peer_id:
            businesses = db.get_chat_businesses(peer_id, user_id)
        elif hasattr(db, 'get_user_businesses'):
            businesses = db.get_user_businesses(user_id)
        return flask.jsonify({"businesses": businesses})
    except Exception:
        return flask.jsonify({"businesses": []})


@app.route("/api/bank/<int:user_id>", methods=["GET"])
def api_bank(user_id):
    try:
        info = db.ensure_bank_account(user_id)
        credit = db.get_active_credit(user_id) if hasattr(db, 'get_active_credit') else None
        return flask.jsonify({
            "balance": info.get("balance", 0) if info else 0,
            "credit": credit,
        })
    except Exception:
        return flask.jsonify({"balance": 0})


@app.route("/api/marriage/<int:user_id>", methods=["GET"])
def api_marriage(user_id):
    try:
        peer_id = flask.request.args.get("peer_id", type=int)
        marriage = db.get_marriage(user_id, peer_id) if hasattr(db, 'get_marriage') else None
        if marriage:
            partner_id = marriage.get("user1") if marriage.get("user2") == user_id else marriage.get("user2")
            partner = db.get_user_readonly(partner_id) if partner_id else None
            return flask.jsonify({
                "partner": partner_id,
                "partner_name": (partner.get("nickname") or str(partner_id)) if partner else str(partner_id),
                "duration": marriage.get("duration_text", ""),
            })
        return flask.jsonify({"partner": None})
    except Exception:
        return flask.jsonify({"partner": None})


@app.route("/api/top", methods=["GET"])
def api_top():
    try:
        peer_id = flask.request.args.get("peer_id", type=int)
        users = []
        if hasattr(db, 'get_top_users') and peer_id:
            rows = db.get_top_users(peer_id, limit=10)
            for r in rows:
                u = db.get_user_readonly(r["vk_id"]) if hasattr(db, 'get_user_readonly') else None
                users.append({
                    "vk_id": r["vk_id"],
                    "balance": r.get("balance", 0),
                    "name": (u.get("nickname") or str(r["vk_id"])) if u else str(r["vk_id"]),
                    "photo": "",
                })
        return flask.jsonify({"users": users})
    except Exception:
        return flask.jsonify({"users": []})


@app.route("/app")
@app.route("/app/<path:path>")
def serve_mini_app(path="index.html"):
    import os
    app_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app")
    file_path = os.path.join(app_dir, path)
    if not os.path.isfile(file_path):
        file_path = os.path.join(app_dir, "index.html")
    return flask.send_file(file_path)


if __name__ == "__main__":
    try:
        from waitress import serve

        serve(app, host="0.0.0.0", port=5000, threads=12)
    except ImportError:
        app.run(host="0.0.0.0", port=5000, debug=False)
