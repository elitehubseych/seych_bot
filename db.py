import datetime
import json
import threading
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

from config import config


class TransferRejected(Exception):
    pass

_pool = None
_POOL_LOCK = threading.Lock()
_POOL_MIN = 1
_POOL_MAX = 16


def _get_pool():
    global _pool
    with _POOL_LOCK:
        if _pool is None:
            _pool = ThreadedConnectionPool(_POOL_MIN, _POOL_MAX, config.DATABASE_URL)
        return _pool


@contextmanager
def db_cursor(cursor_factory=None):
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=cursor_factory) as cur:
            yield cur
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        pool.putconn(conn)


CREATE_USERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS users (
    vk_id BIGINT PRIMARY KEY,
    nickname VARCHAR(50) DEFAULT NULL,
    balance INT DEFAULT 0,
    daily_last_used TIMESTAMP DEFAULT NULL,
    registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

CREATE_CHAT_MEMBERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS chat_members (
    peer_id BIGINT NOT NULL,
    vk_id BIGINT NOT NULL,
    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (peer_id, vk_id)
)
"""

CREATE_CHAT_MUTES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS chat_mutes (
    peer_id BIGINT NOT NULL,
    vk_id BIGINT NOT NULL,
    muted_until TIMESTAMP NOT NULL,
    muted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (peer_id, vk_id)
)
"""

CREATE_CHAT_MARRIAGES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS chat_marriages (
    id SERIAL PRIMARY KEY,
    peer_id BIGINT NOT NULL,
    user1_id BIGINT NOT NULL,
    user2_id BIGINT NOT NULL,
    married_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    divorced_at TIMESTAMPTZ
)
"""

CREATE_BANK_TRANSACTIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS bank_transactions (
    id SERIAL PRIMARY KEY,
    vk_id BIGINT NOT NULL,
    counterparty_id BIGINT,
    kind VARCHAR(16) NOT NULL,
    source VARCHAR(8) NOT NULL DEFAULT 'cash',
    amount BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


CREATE_USER_ITEMS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS user_items (
    vk_id BIGINT NOT NULL,
    item_key VARCHAR(40) NOT NULL,
    qty INT NOT NULL DEFAULT 0,
    PRIMARY KEY (vk_id, item_key)
)
"""

CREATE_USER_ADVENT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS user_advent (
    vk_id BIGINT PRIMARY KEY,
    cycle INT NOT NULL DEFAULT 1,
    claimed INT NOT NULL DEFAULT 0,
    last_claim DATE
)
"""

CREATE_CHAT_IDENTIFIERS_SQL = """
CREATE TABLE IF NOT EXISTS chat_identifiers (
    peer_id BIGINT PRIMARY KEY,
    chat_code VARCHAR(10) NOT NULL UNIQUE
)
"""


def get_connection():
    try:
        return psycopg2.connect(config.DATABASE_URL)
    except Exception:
        raise


def init_db():
    try:
        with db_cursor() as cur:
            cur.execute(CREATE_USERS_TABLE_SQL)
            cur.execute(CREATE_CHAT_MEMBERS_TABLE_SQL)
            cur.execute(CREATE_CHAT_MUTES_TABLE_SQL)
            cur.execute(CREATE_CHAT_MARRIAGES_TABLE_SQL)
            cur.execute(CREATE_BANK_TRANSACTIONS_TABLE_SQL)
            cur.execute(CREATE_USER_ITEMS_TABLE_SQL)
            cur.execute(CREATE_USER_ADVENT_TABLE_SQL)
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS businesses (
                    chat_id BIGINT NOT NULL,
                    kind VARCHAR(16) NOT NULL,
                    owner_vk BIGINT NOT NULL DEFAULT 0,
                    pocket BIGINT NOT NULL DEFAULT 0,
                    upgrades INT NOT NULL DEFAULT 0,
                    paid_until TIMESTAMPTZ,
                    sale_info TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (chat_id, kind)
                )
                """
            )
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS active_title VARCHAR(40)")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS total_earned BIGINT NOT NULL DEFAULT 0")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS total_spent BIGINT NOT NULL DEFAULT 0")
            cur.execute("ALTER TABLE chat_members ADD COLUMN IF NOT EXISTS message_count BIGINT NOT NULL DEFAULT 0")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS bank_balance BIGINT NOT NULL DEFAULT 0")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS account_number VARCHAR(14)")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS bank_interest_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS loan TEXT")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS loan_log TEXT NOT NULL DEFAULT '[]'")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS credit_rating INT NOT NULL DEFAULT 50")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS credit_rating_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_account_number_uq ON users(account_number) WHERE account_number IS NOT NULL")
            cur.execute("CREATE INDEX IF NOT EXISTS bank_transactions_owner_idx ON bank_transactions(vk_id, created_at DESC)")
            cur.execute("ALTER TABLE chat_marriages ALTER COLUMN married_at TYPE TIMESTAMPTZ")
            cur.execute("ALTER TABLE chat_marriages ALTER COLUMN divorced_at TYPE TIMESTAMPTZ")
            cur.execute("ALTER TABLE chat_marriages ALTER COLUMN married_at SET DEFAULT CURRENT_TIMESTAMP")
            cur.execute("DELETE FROM chat_mutes WHERE muted_until <= CURRENT_TIMESTAMP")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS promo_codes (
                    code VARCHAR(40) PRIMARY KEY,
                    rewards TEXT NOT NULL,
                    max_uses INT NOT NULL DEFAULT 1,
                    used_count INT NOT NULL DEFAULT 0,
                    expires_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS promo_claims (
                    code VARCHAR(40) NOT NULL,
                    vk_id BIGINT NOT NULL,
                    claimed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (code, vk_id)
                )
                """
            )
            cur.execute(CREATE_CHAT_IDENTIFIERS_SQL)
    except psycopg2.Error:
        pass


def get_user(vk_id):
    try:
        with db_cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE vk_id = %s", (vk_id,))
            user = cur.fetchone()
            if user is None:
                cur.execute(
                    "INSERT INTO users (vk_id) VALUES (%s) RETURNING *",
                    (vk_id,),
                )
                user = cur.fetchone()
            return dict(user)
    except psycopg2.Error:
        return None


def get_user_readonly(vk_id):
    try:
        with db_cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE vk_id = %s", (vk_id,))
            user = cur.fetchone()
            return dict(user) if user else None
    except psycopg2.Error:
        return None


def transfer_elites(sender_id, receiver_id, amount, commission_rate, dev_id):
    commission = round(amount * commission_rate)
    net = amount - commission
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT balance FROM users WHERE vk_id = %s FOR UPDATE",
                (sender_id,),
            )
            row = cur.fetchone()
            if row is None or row[0] < amount:
                raise TransferRejected()

            cur.execute(
                "UPDATE users SET balance = balance - %s WHERE vk_id = %s",
                (amount, sender_id),
            )
            cur.execute(
                """
                INSERT INTO users (vk_id, balance) VALUES (%s, %s)
                ON CONFLICT (vk_id) DO UPDATE SET balance = users.balance + EXCLUDED.balance
                """,
                (receiver_id, net),
            )
            if commission > 0:
                cur.execute(
                    """
                    INSERT INTO users (vk_id, balance) VALUES (%s, %s)
                    ON CONFLICT (vk_id) DO UPDATE SET balance = users.balance + EXCLUDED.balance
                    """,
                    (dev_id, commission),
                )
        return {"amount": amount, "commission": commission, "net": net}
    except TransferRejected:
        return None
    except psycopg2.Error:
        return None


def set_nickname(vk_id, nickname):
    try:
        with db_cursor() as cur:
            cur.execute(
                "UPDATE users SET nickname = %s WHERE vk_id = %s",
                (nickname, vk_id),
            )
        return True
    except psycopg2.Error:
        return False


def clear_nickname(vk_id):
    try:
        with db_cursor() as cur:
            cur.execute(
                "UPDATE users SET nickname = NULL WHERE vk_id = %s",
                (vk_id,),
            )
        return True
    except psycopg2.Error:
        return False


def claim_daily(vk_id, amount, cooldown_interval="24 hours"):
    try:
        with db_cursor() as cur:
            cur.execute(
                f"""
                UPDATE users
                SET balance = balance + %s,
                    daily_last_used = CURRENT_TIMESTAMP
                WHERE vk_id = %s
                  AND (
                      daily_last_used IS NULL
                      OR daily_last_used <= CURRENT_TIMESTAMP - INTERVAL '{cooldown_interval}'
                  )
                RETURNING balance
                """,
                (amount, vk_id),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except psycopg2.Error:
        return None


def daily_ready_at(vk_id):
    try:
        with db_cursor() as cur:
            cur.execute(
                """
                SELECT daily_last_used + INTERVAL '24 hours'
                FROM users WHERE vk_id = %s AND daily_last_used IS NOT NULL
                """,
                (vk_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except psycopg2.Error:
        return None


def update_balance(vk_id, amount):
    try:
        with db_cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (vk_id, balance, total_earned, total_spent)
                VALUES (%s, %s, GREATEST(%s, 0), -LEAST(%s, 0))
                ON CONFLICT (vk_id)
                DO UPDATE SET balance = users.balance + EXCLUDED.balance,
                    total_earned = users.total_earned + EXCLUDED.total_earned,
                    total_spent = users.total_spent + EXCLUDED.total_spent
                RETURNING balance
                """,
                (vk_id, amount, amount, amount),
            )
            return cur.fetchone()[0]
    except psycopg2.Error:
        return None


def bump_messages(peer_id, vk_id):
    try:
        with db_cursor() as cur:
            cur.execute(
                """
                INSERT INTO chat_members (peer_id, vk_id, message_count)
                VALUES (%s, %s, 1)
                ON CONFLICT (peer_id, vk_id)
                DO UPDATE SET message_count = chat_members.message_count + 1
                RETURNING message_count
                """,
                (peer_id, vk_id),
            )
            return cur.fetchone()[0]
    except psycopg2.Error:
        return None


def set_loan(vk_id, loan_json, log_json=None):
    try:
        with db_cursor() as cur:
            if log_json is None:
                cur.execute(
                    "UPDATE users SET loan = %s WHERE vk_id = %s",
                    (loan_json, vk_id),
                )
            else:
                cur.execute(
                    "UPDATE users SET loan = %s, loan_log = %s WHERE vk_id = %s",
                    (loan_json, log_json, vk_id),
                )
            return True
    except psycopg2.Error:
        return False


def set_credit_rating(vk_id, rating, at_dt):
    try:
        with db_cursor() as cur:
            cur.execute(
                "UPDATE users SET credit_rating = %s, credit_rating_at = %s WHERE vk_id = %s",
                (rating, at_dt, vk_id),
            )
            return True
    except psycopg2.Error:
        return False


def get_chat_member_info(peer_id, vk_id):
    try:
        with db_cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT joined_at, message_count FROM chat_members "
                "WHERE peer_id = %s AND vk_id = %s",
                (peer_id, vk_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    except psycopg2.Error:
        return None


def ensure_chat_member(peer_id, vk_id):
    try:
        with db_cursor() as cur:
            cur.execute(
                """
                INSERT INTO chat_members (peer_id, vk_id) VALUES (%s, %s)
                ON CONFLICT (peer_id, vk_id) DO NOTHING
                """,
                (peer_id, vk_id),
            )
        return True
    except psycopg2.Error:
        return False


def get_chat_top(peer_id, limit=30):
    try:
        with db_cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT u.vk_id, u.nickname, u.balance
                FROM chat_members cm
                JOIN users u ON u.vk_id = cm.vk_id
                WHERE cm.peer_id = %s
                ORDER BY u.balance DESC, u.vk_id ASC
                LIMIT %s
                """,
                (peer_id, limit),
            )
            return cur.fetchall()
    except psycopg2.Error:
        return []


def get_chat_total_balance(peer_id):
    try:
        with db_cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(SUM(u.balance), 0)
                FROM chat_members cm
                JOIN users u ON u.vk_id = cm.vk_id
                WHERE cm.peer_id = %s
                """,
                (peer_id,),
            )
            row = cur.fetchone()
            return int(row[0]) if row and row[0] is not None else 0
    except psycopg2.Error:
        return 0


def get_chat_nicknames(peer_id):
    try:
        with db_cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT u.vk_id, u.nickname, u.balance
                FROM chat_members cm
                JOIN users u ON u.vk_id = cm.vk_id
                WHERE cm.peer_id = %s
                ORDER BY u.nickname NULLS LAST, u.vk_id ASC
                """,
                (peer_id,),
            )
            return cur.fetchall()
    except psycopg2.Error:
        return []


def get_chat_member_ids(peer_id):
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT vk_id FROM chat_members WHERE peer_id = %s",
                (peer_id,),
            )
            return [row[0] for row in cur.fetchall()]
    except psycopg2.Error:
        return []


def set_chat_mute(peer_id, vk_id, seconds):
    from datetime import datetime, timedelta

    try:
        muted_until = datetime.utcnow() + timedelta(seconds=seconds)
        with db_cursor() as cur:
            cur.execute(
                """
                INSERT INTO chat_mutes (peer_id, vk_id, muted_until)
                VALUES (%s, %s, %s)
                ON CONFLICT (peer_id, vk_id)
                DO UPDATE SET muted_until = EXCLUDED.muted_until
                """,
                (peer_id, vk_id, muted_until),
            )
        return True
    except psycopg2.Error:
        return False


def clear_chat_mute(peer_id, vk_id):
    try:
        with db_cursor() as cur:
            cur.execute(
                "DELETE FROM chat_mutes WHERE peer_id = %s AND vk_id = %s",
                (peer_id, vk_id),
            )
        return True
    except psycopg2.Error:
        return False


def get_active_mutes():
    try:
        with db_cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT peer_id, vk_id,
                       EXTRACT(EPOCH FROM (muted_until - CURRENT_TIMESTAMP))::int AS remaining_sec
                FROM chat_mutes
                WHERE muted_until > CURRENT_TIMESTAMP
                """
            )
            return cur.fetchall()
    except psycopg2.Error:
        return []


def create_chat_marriage(peer_id, user1_id, user2_id):
    try:
        with db_cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO chat_marriages (peer_id, user1_id, user2_id)
                VALUES (%s, %s, %s)
                RETURNING id, peer_id, user1_id, user2_id, married_at
                """,
                (peer_id, user1_id, user2_id),
            )
            return dict(cur.fetchone())
    except psycopg2.Error:
        return None


def get_active_marriage_for(peer_id, vk_id):
    try:
        with db_cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, peer_id, user1_id, user2_id, married_at
                FROM chat_marriages
                WHERE peer_id = %s
                  AND divorced_at IS NULL
                  AND %s IN (user1_id, user2_id)
                ORDER BY id DESC
                LIMIT 1
                """,
                (peer_id, vk_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    except psycopg2.Error:
        return None


def get_marriage_by_id(marriage_id):
    try:
        with db_cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM chat_marriages WHERE id = %s", (marriage_id,))
            row = cur.fetchone()
            return dict(row) if row else None
    except psycopg2.Error:
        return None


def get_active_marriages(peer_id):
    try:
        with db_cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, peer_id, user1_id, user2_id, married_at
                FROM chat_marriages
                WHERE peer_id = %s AND divorced_at IS NULL
                ORDER BY married_at ASC, id ASC
                """,
                (peer_id,),
            )
            return [dict(row) for row in cur.fetchall()]
    except psycopg2.Error:
        return []


def end_chat_marriage(marriage_id):
    try:
        with db_cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE chat_marriages
                SET divorced_at = CURRENT_TIMESTAMP
                WHERE id = %s AND divorced_at IS NULL
                RETURNING id, peer_id, user1_id, user2_id, married_at, divorced_at
                """,
                (marriage_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    except psycopg2.Error:
        return None


BANK_CAP = 10_000_000
BANK_WEEKLY_RATE = 0.07

import random as _random

_ACCRUE_SQL = """
WITH t AS (
    SELECT vk_id,
           GREATEST(FLOOR(EXTRACT(EPOCH FROM (now() - bank_interest_at)) / (7 * 86400))::bigint, 0) AS weeks
    FROM users WHERE vk_id = %(vk_id)s
)
UPDATE users u
SET bank_balance = LEAST(
        u.bank_balance + FLOOR(u.bank_balance * %(rate)s * t.weeks)::bigint,
        %(cap)s
    ),
    bank_interest_at = u.bank_interest_at + (t.weeks * 7 || ' days')::interval
FROM t
WHERE u.vk_id = t.vk_id AND t.weeks > 0
"""


def _accrue_in_cur(cur, vk_id):
    cur.execute(_ACCRUE_SQL, {
        "vk_id": vk_id,
        "rate": BANK_WEEKLY_RATE,
        "cap": BANK_CAP,
    })


def _generate_account_number():
    return str(_random.randint(10**13, 10**14 - 1))


def ensure_bank_account(vk_id):
    try:
        with db_cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT balance, bank_balance, account_number FROM users WHERE vk_id = %s",
                (vk_id,),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "INSERT INTO users (vk_id) VALUES (%s) "
                    "RETURNING balance, bank_balance, account_number",
                    (vk_id,),
                )
                row = cur.fetchone()
            number = row["account_number"]
            if not number:
                for _ in range(5):
                    candidate = _generate_account_number()
                    try:
                        cur.execute(
                            "UPDATE users SET account_number = %s "
                            "WHERE vk_id = %s AND account_number IS NULL",
                            (candidate, vk_id),
                        )
                        number = candidate
                        break
                    except psycopg2.IntegrityError:
                        continue
                row["account_number"] = number
            return dict(row)
    except psycopg2.Error:
        return None


def _anchor_after_change(interest_at, old_bank, new_bank, now_dt=None):
    from datetime import datetime, timedelta, timezone

    if now_dt is None:
        now_dt = datetime.now(timezone.utc)
    if interest_at is None or new_bank <= 0:
        return now_dt
    if interest_at.tzinfo is None:
        interest_at = interest_at.replace(tzinfo=timezone.utc)
    elapsed = (now_dt - interest_at).total_seconds()
    if elapsed <= 0:
        return now_dt
    day_units = elapsed * max(old_bank or 0, 0)
    day_units = min(day_units, 6.999 * 86400 * new_bank)
    progress_sec = day_units / new_bank
    return now_dt - timedelta(seconds=progress_sec)


def find_user_by_account(number):
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT vk_id FROM users WHERE account_number = %s",
                (str(number),),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except psycopg2.Error:
        return None


def get_bank_info(vk_id):
    try:
        with db_cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            _accrue_in_cur(cur, vk_id)
            cur.execute(
                "SELECT balance, bank_balance, account_number FROM users WHERE vk_id = %s",
                (vk_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    except psycopg2.Error:
        return None


def bank_deposit(vk_id, amount):
    try:
        with db_cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            _accrue_in_cur(cur, vk_id)
            cur.execute(
                "SELECT balance, bank_balance, bank_interest_at "
                "FROM users WHERE vk_id = %s FOR UPDATE",
                (vk_id,),
            )
            row = cur.fetchone()
            if row is None or row["balance"] < amount:
                return {"ok": False, "reason": "funds"}
            new_bank = row["bank_balance"] + amount
            if new_bank > BANK_CAP:
                return {
                    "ok": False,
                    "reason": "cap",
                    "cap_left": max(BANK_CAP - row["bank_balance"], 0),
                }
            anchor = _anchor_after_change(
                row["bank_interest_at"], row["bank_balance"], new_bank
            )
            cur.execute(
                "UPDATE users SET balance = balance - %s, bank_balance = %s, "
                "bank_interest_at = %s WHERE vk_id = %s",
                (amount, new_bank, anchor, vk_id),
            )
            add_bank_transaction(vk_id, "deposit", "cash", amount)
            return {"ok": True, "cash": row["balance"] - amount, "bank": new_bank}
    except psycopg2.Error:
        return {"ok": False, "reason": "error"}


def bank_withdraw(vk_id, amount):
    try:
        with db_cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            _accrue_in_cur(cur, vk_id)
            cur.execute(
                "SELECT balance, bank_balance, bank_interest_at "
                "FROM users WHERE vk_id = %s FOR UPDATE",
                (vk_id,),
            )
            row = cur.fetchone()
            if row is None or row["bank_balance"] < amount:
                return {"ok": False, "reason": "funds"}
            new_bank = row["bank_balance"] - amount
            anchor = _anchor_after_change(
                row["bank_interest_at"], row["bank_balance"], new_bank
            )
            cur.execute(
                "UPDATE users SET balance = balance + %s, "
                "bank_balance = bank_balance - %s, bank_interest_at = %s "
                "WHERE vk_id = %s",
                (amount, amount, anchor, vk_id),
            )
            add_bank_transaction(vk_id, "withdraw", "bank", amount)
            return {"ok": True, "cash": row["balance"] + amount, "bank": row["bank_balance"] - amount}
    except psycopg2.Error:
        return {"ok": False, "reason": "error"}


def transfer_mixed(sender_id, receiver_id, amount, commission_rate, dev_id,
                   sender_source="cash", receiver_dest="bank"):
    commission = round(amount * commission_rate)
    net = amount - commission
    src_column = "balance" if sender_source == "cash" else "bank_balance"
    dst_column = "balance" if receiver_dest == "cash" else "bank_balance"
    other_column = "bank_balance" if src_column == "balance" else "balance"
    ids = sorted({sender_id, receiver_id, dev_id})
    try:
        with db_cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            _accrue_in_cur(cur, sender_id)
            cur.execute(
                "SELECT vk_id, balance, bank_balance, bank_interest_at FROM users "
                "WHERE vk_id = ANY(%s) ORDER BY vk_id FOR UPDATE",
                (ids,),
            )
            rows = {r["vk_id"]: r for r in cur.fetchall()}
            sender_row = rows.get(sender_id)
            if sender_row is None or sender_row[src_column] < amount:
                raise TransferRejected()

            for uid in (receiver_id, dev_id):
                if uid not in rows:
                    cur.execute("INSERT INTO users (vk_id) VALUES (%s)", (uid,))
                    rows[uid] = {"vk_id": uid, "balance": 0, "bank_balance": 0,
                                 "bank_interest_at": None}

            if src_column == "bank_balance":
                s_new_bank = sender_row["bank_balance"] - amount
                s_anchor = _anchor_after_change(
                    sender_row["bank_interest_at"],
                    sender_row["bank_balance"], s_new_bank,
                )
                cur.execute(
                    "UPDATE users SET bank_interest_at = %s WHERE vk_id = %s",
                    (s_anchor, sender_id),
                )

            cur.execute(
                "UPDATE users SET %s = %s - %%s WHERE vk_id = %%s" % (src_column, src_column),
                (amount, sender_id),
            )
            cur.execute(
                "UPDATE users SET %s = %s + %%s WHERE vk_id = %%s" % (dst_column, dst_column),
                (net, receiver_id),
            )
            if dst_column == "bank_balance":
                r_row = rows[receiver_id]
                r_anchor = _anchor_after_change(
                    r_row["bank_interest_at"],
                    r_row["bank_balance"], r_row["bank_balance"] + net,
                )
                cur.execute(
                    "UPDATE users SET bank_interest_at = %s WHERE vk_id = %s",
                    (r_anchor, receiver_id),
                )
            cur.execute(
                "UPDATE users SET balance = balance + %s WHERE vk_id = %s",
                (commission, dev_id),
            )

            add_bank_transaction(sender_id, "transfer_out", sender_source, amount,
                                 counterparty_id=receiver_id)
            add_bank_transaction(receiver_id, "transfer_in", receiver_dest, net,
                                 counterparty_id=sender_id)

            return {
                "amount": amount,
                "commission": commission,
                "net": net,
                "sender_source": sender_source,
                "receiver_dest": receiver_dest,
                "sender_left": {
                    src_column: sender_row[src_column] - amount,
                    other_column: sender_row[other_column],
                },
            }
    except TransferRejected:
        return None
    except psycopg2.Error:
        return None


def add_bank_transaction(vk_id, kind, source, amount, counterparty_id=None):
    try:
        with db_cursor() as cur:
            cur.execute(
                "INSERT INTO bank_transactions (vk_id, counterparty_id, kind, source, amount) "
                "VALUES (%s, %s, %s, %s, %s)",
                (vk_id, counterparty_id, kind, source, amount),
            )
    except psycopg2.Error:
        pass


def get_bank_transactions(vk_id, limit=8):
    try:
        with db_cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT counterparty_id, kind, source, amount, created_at "
                "FROM bank_transactions WHERE vk_id = %s "
                "ORDER BY created_at DESC LIMIT %s",
                (vk_id, limit),
            )
            return [dict(r) for r in cur.fetchall()]
    except psycopg2.Error:
        return []


def add_item(vk_id, item_key, qty=1):
    try:
        with db_cursor() as cur:
            cur.execute(
                "INSERT INTO user_items (vk_id, item_key, qty) VALUES (%s, %s, %s) "
                "ON CONFLICT (vk_id, item_key) DO UPDATE SET qty = user_items.qty + EXCLUDED.qty",
                (vk_id, item_key, qty),
            )
            return True
    except psycopg2.Error:
        return False


def take_item(vk_id, item_key, qty=1):
    try:
        with db_cursor() as cur:
            cur.execute(
                "UPDATE user_items SET qty = qty - %s "
                "WHERE vk_id = %s AND item_key = %s AND qty >= %s RETURNING qty",
                (qty, vk_id, item_key, qty),
            )
            return cur.fetchone() is not None
    except psycopg2.Error:
        return False


def transfer_item(from_id, to_id, item_key, qty=1):
    try:
        with db_cursor() as cur:
            cur.execute(
                "UPDATE user_items SET qty = qty - %s "
                "WHERE vk_id = %s AND item_key = %s AND qty >= %s",
                (qty, from_id, item_key, qty),
            )
            if cur.rowcount == 0:
                return False
            cur.execute(
                "INSERT INTO user_items (vk_id, item_key, qty) VALUES (%s, %s, %s) "
                "ON CONFLICT (vk_id, item_key) DO UPDATE SET qty = user_items.qty + EXCLUDED.qty",
                (to_id, item_key, qty),
            )
            return True
    except psycopg2.Error:
        return False


def item_qty(vk_id, item_key):
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT qty FROM user_items WHERE vk_id = %s AND item_key = %s",
                (vk_id, item_key),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0
    except psycopg2.Error:
        return 0


def get_inventory(vk_id):
    try:
        with db_cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT item_key, qty FROM user_items "
                "WHERE vk_id = %s AND qty > 0 ORDER BY item_key",
                (vk_id,),
            )
            return [dict(r) for r in cur.fetchall()]
    except psycopg2.Error:
        return []


def get_active_title(vk_id):
    try:
        with db_cursor() as cur:
            cur.execute("SELECT active_title FROM users WHERE vk_id = %s", (vk_id,))
            row = cur.fetchone()
            return row[0] if row and row[0] else None
    except psycopg2.Error:
        return None


def set_active_title(vk_id, title_key):
    try:
        with db_cursor() as cur:
            cur.execute(
                "UPDATE users SET active_title = %s WHERE vk_id = %s",
                (title_key, vk_id),
            )
            return True
    except psycopg2.Error:
        return False


def owns_title(vk_id, title_key):
    return item_qty(vk_id, title_key) > 0


def get_advent(vk_id):
    try:
        with db_cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT cycle, claimed, last_claim FROM user_advent WHERE vk_id = %s", (vk_id,))
            row = cur.fetchone()
            if row:
                return dict(row)
            return {"cycle": 1, "claimed": 0, "last_claim": None}
    except psycopg2.Error:
        return {"cycle": 1, "claimed": 0, "last_claim": None}


def save_advent_claim(vk_id, cycle, claimed, last_claim_date):
    try:
        with db_cursor() as cur:
            if claimed == 0 and last_claim_date is not None:
                cur.execute(
                    "INSERT INTO user_advent (vk_id, cycle, claimed, last_claim) "
                    "VALUES (%s, %s, 0, %s) "
                    "ON CONFLICT (vk_id) DO UPDATE SET cycle = EXCLUDED.cycle, "
                    "claimed = 0, last_claim = EXCLUDED.last_claim",
                    (vk_id, cycle, last_claim_date),
                )
            else:
                cur.execute(
                    "INSERT INTO user_advent (vk_id, cycle, claimed, last_claim) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (vk_id) DO UPDATE SET cycle = EXCLUDED.cycle, "
                    "claimed = EXCLUDED.claimed, last_claim = EXCLUDED.last_claim",
                    (vk_id, cycle, claimed, last_claim_date),
                )
            return True
    except psycopg2.Error:
        return False


_BIZ_KINDS = ("bank", "coin", "roulette", "blackjack")


def biz_get(chat_id, kind):
    try:
        with db_cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM businesses WHERE chat_id = %s AND kind = %s",
                (chat_id, kind),
            )
            return cur.fetchone()
    except psycopg2.Error:
        return None


def biz_ensure(chat_id, kind):
    try:
        with db_cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO businesses (chat_id, kind)
                VALUES (%s, %s)
                ON CONFLICT (chat_id, kind) DO NOTHING
                RETURNING *
                """,
                (chat_id, kind),
            )
            row = cur.fetchone()
        return row if row else biz_get(chat_id, kind)
    except psycopg2.Error:
        return None


def biz_update(chat_id, kind, **fields):
    allowed = {"owner_vk", "pocket", "upgrades", "paid_until", "sale_info"}
    sets, vals = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "sale_info" and not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        sets.append("%s = %%s" % key)
        vals.append(value)
    if not sets:
        return False
    vals.extend([chat_id, kind])
    try:
        with db_cursor() as cur:
            cur.execute(
                "UPDATE businesses SET %s WHERE chat_id = %%s AND kind = %%s"
                % ", ".join(sets),
                vals,
            )
        return True
    except psycopg2.Error:
        return False


def _biz_prune_stats(stats, keep_days=35):
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=keep_days)).date().isoformat()
    for key in ("earn", "clients"):
        bucket = stats.get(key)
        if isinstance(bucket, dict):
            stats[key] = {d: v for d, v in bucket.items() if d >= cutoff}
    return stats


def biz_turnover(chat_id, kind, vk_ids, cut):
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    try:
        with db_cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT owner_vk, pocket, sale_info FROM businesses "
                "WHERE chat_id = %s AND kind = %s FOR UPDATE",
                (chat_id, kind),
            )
            row = cur.fetchone()
            if not row:
                cur.execute(
                    "INSERT INTO businesses (chat_id, kind) VALUES (%s, %s)",
                    (chat_id, kind),
                )
                pocket, owner_vk, raw_stats = 0, 0, "{}"
            else:
                pocket, owner_vk, raw_stats = row["pocket"], row["owner_vk"], row["sale_info"]
            try:
                stats = json.loads(raw_stats or "{}")
            except Exception:
                stats = {}
            earn_map = stats.setdefault("earn", {})
            clients_map = stats.setdefault("clients", {})
            earn_map[today] = int(earn_map.get(today, 0)) + int(cut)
            day_clients = clients_map.get(today)
            if not isinstance(day_clients, list):
                day_clients = []
            for vk_id in (vk_ids or []):
                if vk_id and int(vk_id) > 0 and int(vk_id) not in day_clients:
                    day_clients.append(int(vk_id))
            clients_map[today] = day_clients
            stats = _biz_prune_stats(stats)

            new_pocket = pocket + (int(cut) if owner_vk and owner_vk > 0 else 0)
            cur.execute(
                "UPDATE businesses SET pocket = %s, sale_info = %s "
                "WHERE chat_id = %s AND kind = %s",
                (
                    new_pocket,
                    json.dumps(stats, ensure_ascii=False),
                    chat_id,
                    kind,
                ),
            )
        return True
    except psycopg2.Error:
        return False


def biz_withdraw_pocket(chat_id, kind):
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT pocket FROM businesses "
                "WHERE chat_id = %s AND kind = %s AND owner_vk > 0",
                (chat_id, kind),
            )
            row = cur.fetchone()
            amount = int(row[0]) if row else 0
            if amount <= 0:
                return 0
            cur.execute(
                "UPDATE businesses SET pocket = 0 "
                "WHERE chat_id = %s AND kind = %s AND owner_vk > 0",
                (chat_id, kind),
            )
            return amount
    except psycopg2.Error:
        return 0


def biz_stats_summary(sale_info_raw, now_dt=None):
    try:
        stats = json.loads(sale_info_raw or "{}")
    except Exception:
        stats = {}
    now_dt = now_dt or datetime.datetime.now(datetime.timezone.utc)
    week_cut = (now_dt - datetime.timedelta(days=6)).date().isoformat()
    today = now_dt.date().isoformat()

    def _sum(map_):
        total_today = total_week = total_all = 0
        for day, value in map_.items():
            if day >= week_cut:
                total_week += int(value) if not isinstance(value, list) else len(value)
                if day == today:
                    total_today = int(value) if not isinstance(value, list) else len(value)
            total_all += int(value) if not isinstance(value, list) else len(value)
        return total_today, total_week, total_all

    earn_t, earn_w, earn_a = _sum(stats.get("earn") or {})
    cli_t, cli_w, cli_a = _sum(stats.get("clients") or {})
    earn_days = sorted((stats.get("earn") or {}).keys())
    if earn_days and earn_a > 0:
        try:
            span = max((now_dt.date() - datetime.date.fromisoformat(earn_days[0])).days + 1, 1)
        except ValueError:
            span = len(earn_days)
        earn_avg = int(round(earn_a / float(span)))
    else:
        earn_avg = 0
    return {
        "earn": {"today": earn_t, "week": earn_w, "all": earn_a, "avg": earn_avg},
        "clients": {"today": cli_t, "week": cli_w, "all": cli_a},
    }


def dev_reset_account(vk_id):
    try:
        with db_cursor() as cur:
            cur.execute(
                "UPDATE users SET balance = 0, bank_balance = 0, "
                "total_earned = 0, total_spent = 0, active_title = NULL, "
                "loan = NULL, loan_log = '[]', credit_rating = 50 "
                "WHERE vk_id = %s",
                (vk_id,),
            )
            cur.execute("DELETE FROM user_items WHERE vk_id = %s", (vk_id,))
            cur.execute("DELETE FROM user_advent WHERE vk_id = %s", (vk_id,))
            cur.execute(
                "UPDATE businesses SET owner_vk = 0, pocket = 0, "
                "upgrades = 0, paid_until = NULL, sale_info = '{}' "
                "WHERE owner_vk = %s RETURNING chat_id, kind",
                (vk_id,),
            )
            biz_rows = cur.fetchall()
            cur.execute(
                "UPDATE chat_members SET message_count = 0 WHERE vk_id = %s",
                (vk_id,),
            )
            return [(r[0], r[1]) for r in biz_rows]
    except psycopg2.Error:
        return []


def dev_find_chats_with_user(vk_id):
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT peer_id FROM chat_members WHERE vk_id = %s",
                (vk_id,),
            )
            return [r[0] for r in cur.fetchall()]
    except psycopg2.Error:
        return []


import string as _string
import random as _random

_CHAT_CODE_ALPHABET = _string.ascii_letters + _string.digits


def _gen_chat_code(length=8):
    return "".join(_random.choices(_CHAT_CODE_ALPHABET, k=length))


def get_chat_code(peer_id):
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT chat_code FROM chat_identifiers WHERE peer_id = %s",
                (peer_id,),
            )
            row = cur.fetchone()
            if row:
                return row[0]
            for _ in range(10):
                code = _gen_chat_code()
                try:
                    cur.execute(
                        "INSERT INTO chat_identifiers (peer_id, chat_code) "
                        "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (peer_id, code),
                    )
                    if cur.rowcount:
                        return code
                except psycopg2.IntegrityError:
                    continue
            cur.execute(
                "SELECT chat_code FROM chat_identifiers WHERE peer_id = %s",
                (peer_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except psycopg2.Error:
        return None


def get_peer_by_chat_code(code):
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT peer_id FROM chat_identifiers WHERE chat_code = %s",
                (code,),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except psycopg2.Error:
        return None


def get_all_chat_codes():
    try:
        with db_cursor() as cur:
            cur.execute("SELECT peer_id, chat_code FROM chat_identifiers ORDER BY peer_id")
            return [(r[0], r[1]) for r in cur.fetchall()]
    except psycopg2.Error:
        return []


def get_all_peer_ids(limit=500):
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT peer_id, MAX(joined_at) AS last_seen FROM chat_members "
                "GROUP BY peer_id ORDER BY last_seen DESC LIMIT %s",
                (limit,),
            )
            return [(r[0], r[1]) for r in cur.fetchall()]
    except psycopg2.Error:
        return []


def title_rarity_percent(title_key):
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM user_items "
                "WHERE item_key = %s AND qty > 0",
                (title_key,),
            )
            owners = cur.fetchone()[0] or 0
            cur.execute("SELECT COUNT(*) FROM users")
            total = cur.fetchone()[0] or 0
            if total == 0:
                return 0
            return round(owners / total * 100, 1)
    except psycopg2.Error:
        return 0


def title_owners_count(title_key):
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM user_items "
                "WHERE item_key = %s AND qty > 0",
                (title_key,),
            )
            row = cur.fetchone()
            return row[0] if row else 0
    except psycopg2.Error:
        return 0


def promo_get(code):
    try:
        with db_cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT code, rewards, max_uses, used_count, expires_at "
                "FROM promo_codes WHERE LOWER(code) = LOWER(%s)",
                (code,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            row = dict(row)
            try:
                row["rewards"] = json.loads(row["rewards"])
            except Exception:
                row["rewards"] = {}
            return row
    except psycopg2.Error:
        return None


def promo_already_claimed(code, vk_id):
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT 1 FROM promo_claims WHERE LOWER(code) = LOWER(%s) AND vk_id = %s",
                (code, vk_id),
            )
            return cur.fetchone() is not None
    except psycopg2.Error:
        return True


def promo_take_claim(code):
    try:
        with db_cursor() as cur:
            cur.execute(
                "UPDATE promo_codes SET used_count = used_count + 1 "
                "WHERE LOWER(code) = LOWER(%s) "
                "  AND used_count < max_uses "
                "  AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP) "
                "RETURNING used_count",
                (code,),
            )
            return cur.fetchone() is not None
    except psycopg2.Error:
        return False


def promo_mark_claimed(code, vk_id):
    try:
        with db_cursor() as cur:
            cur.execute(
                "INSERT INTO promo_claims (code, vk_id) VALUES (LOWER(%s), %s) "
                "ON CONFLICT (code, vk_id) DO NOTHING",
                (code, vk_id),
            )
        return True
    except psycopg2.Error:
        return False
