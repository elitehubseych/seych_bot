
import logging
import threading

import db
from config import config
from handlers.registry import command
from utils.parse import extract_target_id, format_amount, parse_amount
from utils.receipt import generate_transaction_id, send_transfer_receipts
from utils.vk import display_name, display_name_by_vk_id

logger = logging.getLogger(__name__)

COMMISSION_RATE = 0.05
DEFAULT_AMOUNT = 1000

BOT_ID = -abs(int(config.ID_GROUP))
DEV_ID = int(config.DEV_ID)


@command("paybot")
def cmd_paybot(user, args, message):
    if user["vk_id"] != DEV_ID:
        return None

    reply_message = message.get("reply_message")
    target_id, remaining = extract_target_id(args, reply_message)

    if target_id is None:
        if not (args or "").strip():
            return "Укажи получателя: paybot @получатель сумма"
        return None
    if target_id == BOT_ID:
        return "Себе переводить нельзя 🙃"
    if target_id < 0:
        return "Эта команда не доступна на сообществах 😕"

    raw_amount = remaining.strip()
    if raw_amount.lower() in ("все", "всё"):
        amount = bot_balance
        if amount <= 0:
            return "У бота нет элитов на наличных 🤷"
    else:
        amount = parse_amount(raw_amount, default=DEFAULT_AMOUNT)
        if amount is None or amount <= 0:
            return None

    bot_user = db.get_user_readonly(BOT_ID)
    bot_balance = bot_user["balance"] if bot_user else 0
    if bot_balance < amount:
        return (
            f"У бота недостаточно элитов. "
            f"На балансе: {format_amount(bot_balance)} 💎"
        )

    target = db.get_user(target_id)
    if target is None:
        return "Не удалось найти получателя, попробуй позже"

    result = db.transfer_mixed(
        BOT_ID, target_id, amount, COMMISSION_RATE, DEV_ID,
        sender_source="cash", receiver_dest="cash",
    )
    if result is None:
        return "Перевод не удался. Проверь баланс и попробуй снова"

    sender_name = display_name_by_vk_id(BOT_ID)
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
            "sender_id": BOT_ID,
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
