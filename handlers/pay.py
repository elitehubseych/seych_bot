
import logging
import threading

import db
from config import config
from handlers.registry import command
from utils.parse import extract_target_id, format_amount, parse_amount
from utils.receipt import generate_transaction_id, send_transfer_receipts
from utils.vk import display_name

logger = logging.getLogger(__name__)

COMMISSION_RATE = 0.05
DEFAULT_AMOUNT = 1000


@command("pay")
def cmd_pay(user, args, message):
    reply_message = message.get("reply_message")
    target_id, remaining = extract_target_id(args, reply_message)
    allowed_group_id = -int(config.ID_GROUP)

    if target_id is None:
        if not (args or "").strip():
            return "Укажи получателя: pay @получатель сумма (или ответь на его сообщение)"
        return None
    if target_id == user["vk_id"]:
        return "Себе переводить нельзя 🙃"

    if user["vk_id"] < 0 and user["vk_id"] != allowed_group_id:
        return "Эта команда не доступна на сообществах 😕"
    if target_id < 0 and target_id != allowed_group_id:
        return "Эта команда не доступна на сообществах 😕"

    raw_amount = remaining.strip()
    if raw_amount.lower() in ("все", "всё"):
        amount = user["balance"]
        if amount <= 0:
            return "У тебя нет элитов на наличных 🤷"
    else:
        amount = parse_amount(raw_amount, default=DEFAULT_AMOUNT)
        if amount is None or amount <= 0:
            return None

    if user["balance"] < amount:
        return f"Недостаточно элитов. На балансе: {format_amount(user['balance'])} 💰"

    target = db.get_user(target_id)
    if target is None:
        return "Не удалось найти получателя, попробуй позже"

    result = db.transfer_elites(
        user["vk_id"], target_id, amount, COMMISSION_RATE, int(config.DEV_ID)
    )
    if result is None:
        return "Перевод не удался. Проверь баланс и попробуй снова"

    sender_name = display_name(user)
    receiver_name = display_name(target)
    transaction_id = generate_transaction_id()

    response_text = (
        f"{sender_name} перевёл {format_amount(amount)} элитов {receiver_name} 💸\n"
        f"Комиссия: {format_amount(result['commission'])} элитов 🏦\n"
        f"Итог: {format_amount(result['net'])} элитов ✅"
    )

    thread = threading.Thread(
        target=send_transfer_receipts,
        kwargs={
            "peer_id": message.get("peer_id"),
            "sender_id": user["vk_id"],
            "receiver_id": target_id,
            "amount_text": format_amount(result["amount"]),
            "commission_text": format_amount(result["commission"]),
            "net_text": format_amount(result["net"]),
            "transaction_id": transaction_id,
            "message_text": response_text,
            "source_label": "cash",
            "dest_label": "cash",
        },
        daemon=True,
    )
    thread.start()

    return response_text
