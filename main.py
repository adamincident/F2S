import html
import sqlite3
import time
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Optional, Tuple
from eth_account import Account
from tronpy import Tron
from tronpy.keys import PrivateKey

import requests

# =========================
# CONFIG
# =========================
BOT_TOKEN = "8709397983:AAGN-NhPOlSZUgRgAX_mqO3X9Zj7AaXiYKo"
CHANNEL_ID = "-1003764332533"

MAIN_ETH_WALLET = "0x0eAd9196934aA92d24B16060E78D644d4198606e"
MAIN_TRON_WALLET = "TEoPpnymKPkf7BKpnASM8QNPa5bETzKX25"

PRICE_PER_CHAR = Decimal("0.35")
MIN_CHARS = 3
MAX_CHARS = 3000
POST_COOLDOWN_SECONDS = 30
POLL_TIMEOUT = 30
DB_PATH = "Fund2Say.db"

WALLETS = {
    "BTC": "bc1q96rxp2wrx4jcfnkgre32umfq4kr20pyc9vfsps",
    "ETH": "0x0eAd9196934aA92d24B16060E78D644d4198606e",
    "XRP": "rBHoTHTuZqmAE9DEHUSfpoUhaW3Y4DT52q",
    "SOL": "4mPV1NH2f7ka6W4pAi8ThKy6ks7kY4aepXKQdEiZVJcm",
    "TRON": "TEoPpnymKPkf7BKpnASM8QNPa5bETzKX25",
    "LTC": "Lc2NcwamnGT4TPbu4fcFUDUgfNanDgLL4J",
    "TON": "UQAdYJLHM7eZetdGTYJNxZsZg7zZBmH5nk8aR-padG6OFdTV",
}

ETHERSCAN_API_KEY = "V7AKCX4IDTP6HMD9XYHS52SZM578B4IJF6"

BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
tron = Tron()

# =========================
# DB
# =========================
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row

def init_db() -> None:
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            balance_usd TEXT NOT NULL DEFAULT '0.00',
            state TEXT,
            pending_message TEXT,
            pending_cost TEXT,
            last_post_at INTEGER NOT NULL DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coin TEXT NOT NULL,
            tx_hash TEXT NOT NULL UNIQUE,
            amount_coin TEXT NOT NULL,
            amount_usd TEXT NOT NULL,
            claimed_by INTEGER NOT NULL,
            claimed_at INTEGER NOT NULL
        )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS addresses (
        user_id INTEGER PRIMARY KEY,
        eth_address TEXT,
        private_key TEXT,
        last_balance_wei TEXT NOT NULL DEFAULT '0',
        tron_address TEXT,
        tron_private_key TEXT,
        last_trx_balance TEXT NOT NULL DEFAULT '0'
    )
""")

    # 🔥 TRON columns
    try:
        cur.execute("ALTER TABLE addresses ADD COLUMN tron_address TEXT")
    except sqlite3.OperationalError:
        pass

    try:
        cur.execute("ALTER TABLE addresses ADD COLUMN tron_private_key TEXT")
    except sqlite3.OperationalError:
        pass

    try:
        cur.execute("ALTER TABLE addresses ADD COLUMN last_trx_balance TEXT NOT NULL DEFAULT '0'")
    except sqlite3.OperationalError:
        pass

    conn.commit()


def get_user(user_id: int) -> sqlite3.Row:
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if row:
        return row

    cur.execute("""
        INSERT INTO users (user_id, balance_usd, state, pending_message, pending_cost, last_post_at)
        VALUES (?, '0.00', NULL, NULL, NULL, 0)
    """, (user_id,))
    conn.commit()
    cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    return cur.fetchone()
    
def get_or_create_eth_address(user_id: int):
    cur = conn.cursor()

    cur.execute("SELECT eth_address FROM addresses WHERE user_id = ?", (user_id,))
    row = cur.fetchone()

    if row and row["eth_address"]:
        return row["eth_address"]

    acct = Account.create()
    address = acct.address
    private_key = acct.key.hex()

    cur.execute("SELECT 1 FROM addresses WHERE user_id = ?", (user_id,))
    existing = cur.fetchone()

    if existing:
        cur.execute(
            """
            UPDATE addresses
            SET eth_address = ?, private_key = ?, last_balance_wei = '0'
            WHERE user_id = ?
            """,
            (address, private_key, user_id),
        )
    else:
        cur.execute(
            """
            INSERT INTO addresses (
                user_id, eth_address, private_key, last_balance_wei,
                tron_address, tron_private_key, last_trx_balance
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, address, private_key, "0", None, None, "0"),
        )

    conn.commit()
    return address


def get_or_create_tron_address(user_id: int):
    cur = conn.cursor()

    cur.execute("SELECT tron_address FROM addresses WHERE user_id = ?", (user_id,))
    row = cur.fetchone()

    if row and row["tron_address"]:
        return row["tron_address"]

    private_key = PrivateKey.random()
    tron_address = private_key.public_key.to_base58check_address()
    tron_private_key = private_key.hex()

    cur.execute("SELECT 1 FROM addresses WHERE user_id = ?", (user_id,))
    existing = cur.fetchone()

    if existing:
        cur.execute(
            """
            UPDATE addresses
            SET tron_address = ?, tron_private_key = ?, last_trx_balance = '0'
            WHERE user_id = ?
            """,
            (tron_address, tron_private_key, user_id),
        )
    else:
        cur.execute(
            """
            INSERT INTO addresses (
                user_id, eth_address, private_key, last_balance_wei,
                tron_address, tron_private_key, last_trx_balance
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, None, None, "0", tron_address, tron_private_key, "0"),
        )

    conn.commit()
    return tron_address

def update_user_profile(user_id: int, username: Optional[str], first_name: Optional[str]) -> None:
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (user_id, username, first_name, balance_usd, state, pending_message, pending_cost, last_post_at)
        VALUES (?, ?, ?, '0.00', NULL, NULL, NULL, 0)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name
    """, (user_id, username, first_name))
    conn.commit()


def set_state(user_id: int, state: Optional[str], pending_message: Optional[str] = None,
              pending_cost: Optional[str] = None) -> None:
    cur = conn.cursor()
    cur.execute("""
        UPDATE users
        SET state = ?, pending_message = ?, pending_cost = ?
        WHERE user_id = ?
    """, (state, pending_message, pending_cost, user_id))
    conn.commit()


def add_balance(user_id: int, amount_usd: Decimal) -> Decimal:
    user = get_user(user_id)
    current = Decimal(user["balance_usd"])
    new_balance = (current + amount_usd).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    cur = conn.cursor()
    cur.execute("UPDATE users SET balance_usd = ? WHERE user_id = ?", (str(new_balance), user_id))
    conn.commit()
    return new_balance


def deduct_balance(user_id: int, amount_usd: Decimal) -> Tuple[bool, Decimal]:
    user = get_user(user_id)
    current = Decimal(user["balance_usd"])
    if current < amount_usd:
        return False, current

    new_balance = (current - amount_usd).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET balance_usd = ?, last_post_at = ? WHERE user_id = ?",
        (str(new_balance), int(time.time()), user_id)
    )
    conn.commit()
    return True, new_balance


def get_balance(user_id: int) -> Decimal:
    user = get_user(user_id)
    return Decimal(user["balance_usd"])


def is_tx_already_claimed(tx_hash: str) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM claims WHERE tx_hash = ?", (tx_hash,))
    return cur.fetchone() is not None


def save_claim(coin: str, tx_hash: str, amount_coin: Decimal, amount_usd: Decimal, user_id: int) -> None:
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO claims (coin, tx_hash, amount_coin, amount_usd, claimed_by, claimed_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (coin, tx_hash, str(amount_coin), str(amount_usd), user_id, int(time.time())))
    conn.commit()


# =========================
# TELEGRAM HELPERS
# =========================
def tg_request(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    resp = requests.post(f"{BASE_URL}/{method}", json=payload, timeout=30)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error in {method}: {data}")
    return data


def send_message(
    chat_id: int | str,
    text: str,
    reply_markup: Optional[Dict[str, Any]] = None,
    parse_mode: Optional[str] = None,
    disable_web_page_preview: bool = True
) -> None:
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": disable_web_page_preview,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    if parse_mode:
        payload["parse_mode"] = parse_mode
    tg_request("sendMessage", payload)


def answer_callback(callback_query_id: str, text: str = "") -> None:
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    tg_request("answerCallbackQuery", payload)


def get_updates(offset: Optional[int]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "timeout": 5,
        "allowed_updates": ["message", "callback_query"],
    }
    if offset is not None:
        payload["offset"] = offset
    resp = requests.get(f"{BASE_URL}/getUpdates", params=payload, timeout=15)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"getUpdates failed: {data}")
    return data


# =========================
# UI
# =========================
def main_menu_keyboard() -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "💳 Deposit", "callback_data": "menu_deposit"},
                {"text": "💰 Balance", "callback_data": "menu_balance"},
            ],
            [
                {"text": "✍️ Send Message", "callback_data": "menu_send"},
                {"text": "❓ Help", "callback_data": "menu_help"},
            ],
        ]
    }

def deposit_keyboard() -> Dict[str, Any]:
    rows = []
    coins = list(WALLETS.keys())
    for i in range(0, len(coins), 2):
        row = [{"text": coins[i], "callback_data": f"deposit_{coins[i]}"}]
        if i + 1 < len(coins):
            row.append({"text": coins[i + 1], "callback_data": f"deposit_{coins[i + 1]}"})
        rows.append(row)
    rows.append([{"text": "Back", "callback_data": "menu_home"}])
    return {"inline_keyboard": rows}


def post_choice_keyboard() -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "Post Publicly", "callback_data": "post_public"},
                {"text": "Post Anonymously", "callback_data": "post_anon"},
            ],
            [{"text": "Cancel", "callback_data": "cancel_post"}],
        ]
    }


def confirm_keyboard(mode: str) -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "Confirm", "callback_data": f"confirm_{mode}"}],
            [{"text": "Cancel", "callback_data": "cancel_post"}],
        ]
    }


def format_usd(amount: Decimal) -> str:
    return f"${amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}"


def cost_for_message(message: str) -> Decimal:
    chars = len(message)
    return (Decimal(chars) * PRICE_PER_CHAR).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def welcome_text() -> str:
    return (
        "🔥 <b>Fund2Say</b>\n\n"
        "Turn your balance into messages in the channel.\n\n"
        f"• Rate: {format_usd(PRICE_PER_CHAR)} per character\n"
        f"• Minimum: {MIN_CHARS} chars ({format_usd(PRICE_PER_CHAR * MIN_CHARS)})\n"
        f"• Maximum: {MAX_CHARS} chars\n\n"
        "👇 Use the buttons below to begin."
    )


def help_text() -> str:
    return (
        "<b>How it works</b>\n\n"
        "1. Tap <b>Deposit</b>\n"
        "2. Send crypto\n"
        "3. Balance updates automatically (ETH)\n"
        "4. Tap <b>Send Message</b>\n"
        "5. Confirm and post\n\n"
        "<b>Notes</b>\n"
        "• ETH = auto detected\n"
        "• Others = /claim\n"
        "• Min 3 characters"
    )


# =========================
# CHAIN VERIFICATION
# =========================
def get_price_map() -> Dict[str, Decimal]:
    ids = {
        "BTC": "bitcoin",
        "ETH": "ethereum",
        "SOL": "solana",
        "XRP": "ripple",
        "TRON": "tron",
        "LTC": "litecoin",
        "TON": "the-open-network",
    }

    joined = ",".join(ids.values())

    resp = requests.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": joined, "vs_currencies": "usd"},
        timeout=20,
    )

    data = resp.json()

    prices: Dict[str, Decimal] = {}

    for coin, coin_id in ids.items():
        usd = data.get(coin_id, {}).get("usd", 0)
        prices[coin] = Decimal(str(usd))

    return prices

def verify_eth(tx_hash: str, prices: Dict[str, Decimal]):
    try:
        # get receipt
        resp = requests.post(
            "https://eth.llamarpc.com",
            json={
                "jsonrpc": "2.0",
                "method": "eth_getTransactionReceipt",
                "params": [tx_hash],
                "id": 1,
            },
            timeout=20,
        )

        data = resp.json()
        receipt = data.get("result")

        if not receipt:
            return False, "Transaction not found or not confirmed yet.", Decimal("0"), Decimal("0")

        if receipt.get("status") != "0x1":
            return False, "Transaction failed.", Decimal("0"), Decimal("0")

        # 🔥 get full tx
        tx_resp = requests.post(
            "https://eth.llamarpc.com",
            json={
                "jsonrpc": "2.0",
                "method": "eth_getTransactionByHash",
                "params": [tx_hash],
                "id": 1,
            },
            timeout=20,
        )

        tx_data = tx_resp.json()
        tx = tx_data.get("result")

        if not tx:
            return False, "Transaction data unavailable.", Decimal("0"), Decimal("0")

        # 🔥 CASE 1: direct transfer
        to_addr = (tx.get("to") or "").lower()
        value_wei = int(tx.get("value", "0x0"), 16)

        if to_addr == WALLETS["ETH"].lower() and value_wei > 0:
            amount_coin = Decimal(value_wei) / Decimal(10**18)
            amount_usd = (amount_coin * prices["ETH"]).quantize(Decimal("0.01"))
            return True, "ok", amount_coin, amount_usd

        # 🔥 CASE 2: internal transfer (your situation)
        # estimate from logs (fallback)
        for log in receipt.get("logs", []):
            if not isinstance(log, dict):
                continue

            # check if your wallet is involved
            topics = log.get("topics", [])
            if any(WALLETS["ETH"].lower()[2:] in str(t).lower() for t in topics):
                # we can’t perfectly decode ETH internal transfers,
                # but we confirm involvement → approximate value from tx
                if value_wei > 0:
                    amount_coin = Decimal(value_wei) / Decimal(10**18)
                    amount_usd = (amount_coin * prices["ETH"]).quantize(Decimal("0.01"))
                    return True, "ok", amount_coin, amount_usd

        return False, "Transaction does not involve your wallet.", Decimal("0"), Decimal("0")

    except Exception as e:
        return False, f"ETH error: {str(e)}", Decimal("0"), Decimal("0")

def check_eth_deposits():
    try:
        cur = conn.cursor()
        prices = get_price_map()
        eth_price = prices.get("ETH", Decimal("0"))

        cur.execute("SELECT user_id, eth_address, last_balance_wei FROM addresses")
        rows = cur.fetchall()

        for row in rows:
            user_id = row["user_id"]
            eth_address = row["eth_address"]

            if not eth_address:
                continue

            address = eth_address.lower()
            last_balance_wei = int(row["last_balance_wei"] or "0")

            resp = requests.post(
                "https://eth.llamarpc.com",
                json={
                    "jsonrpc": "2.0",
                    "method": "eth_getBalance",
                    "params": [address, "latest"],
                    "id": 1,
                },
                timeout=20,
            )

            try:
                data = resp.json()
            except Exception:
                print(f"[ETH CHECK] bad RPC response for {address}")
                continue

            if not isinstance(data, dict):
                print(f"[ETH CHECK] invalid RPC payload for {address}: {data}")
                continue

            balance_hex = data.get("result")
            if not balance_hex:
                continue

            current_balance_wei = int(balance_hex, 16)

            if current_balance_wei <= last_balance_wei:
                continue

            delta_wei = current_balance_wei - last_balance_wei
            amount_eth = Decimal(delta_wei) / Decimal(10**18)
            amount_usd = (amount_eth * eth_price).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP
            )

            cur.execute(
                "UPDATE addresses SET last_balance_wei = ? WHERE user_id = ?",
                (str(current_balance_wei), user_id),
            )
            conn.commit()

            new_balance = add_balance(user_id, amount_usd)

            cur.execute("SELECT private_key FROM addresses WHERE user_id = ?", (user_id,))
            pk_row = cur.fetchone()

            if pk_row and pk_row["private_key"]:
                try:
                    sweep_eth(pk_row["private_key"], address)
                    print(f"[ETH SWEEP] success for user {user_id}")
                except Exception as sweep_error:
                    print(f"[ETH SWEEP ERROR] {sweep_error}")

            send_message(
                user_id,
                (
                    "✅ <b>Deposit Received</b>\n\n"
                    f"• Amount: {amount_eth} ETH\n"
                    f"• Credited: {format_usd(amount_usd)}\n"
                    f"• New balance: {format_usd(new_balance)}"
                ),
                parse_mode="HTML",
            )

    except Exception as e:
        print(f"ETH check error: {e}")

def check_tron_deposits():
    try:
        cur = conn.cursor()
        prices = get_price_map()
        trx_price = prices.get("TRON", Decimal("0"))

        cur.execute("""
            SELECT user_id, tron_address, last_trx_balance
            FROM addresses
            WHERE tron_address IS NOT NULL
        """)
        rows = cur.fetchall()

        print(f"[TRON CHECK] scanning {len(rows)} addresses")

        for row in rows:
            user_id = row["user_id"]
            address = row["tron_address"]
            last_balance_sun = int(row["last_trx_balance"] or "0")

            current_balance_trx = tron.get_account_balance(address)
            current_balance_sun = int(Decimal(str(current_balance_trx)) * Decimal("1000000"))

            print(
                f"[TRON CHECK] user={user_id} "
                f"last={last_balance_sun} current={current_balance_sun}"
            )

            if current_balance_sun <= last_balance_sun:
                continue

            delta_sun = current_balance_sun - last_balance_sun
            amount_trx = Decimal(delta_sun) / Decimal("1000000")
            amount_usd = (amount_trx * trx_price).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP
            )

            cur.execute(
                "UPDATE addresses SET last_trx_balance = ? WHERE user_id = ?",
                (str(current_balance_sun), user_id),
            )
            conn.commit()

            new_balance = add_balance(user_id, amount_usd)

            cur.execute("SELECT tron_private_key FROM addresses WHERE user_id = ?", (user_id,))
            pk_row = cur.fetchone()

            if pk_row and pk_row["tron_private_key"]:
                try:
                    sweep_tron(pk_row["tron_private_key"])
                    print(f"[TRON SWEEP] success for user {user_id}")
                except Exception as sweep_error:
                    print(f"[TRON SWEEP ERROR] {sweep_error}")

            send_message(
                user_id,
                (
                    "✅ <b>Deposit Received</b>\n\n"
                    f"• Amount: {amount_trx} TRX\n"
                    f"• Credited: {format_usd(amount_usd)}\n"
                    f"• New balance: {format_usd(new_balance)}"
                ),
                parse_mode="HTML",
            )

    except Exception as e:
        print(f"TRON check error: {e}")

def sweep_tron(private_key_hex: str):
    try:
        pk = PrivateKey(bytes.fromhex(private_key_hex))
        owner = pk.public_key.to_base58check_address()

        balance_trx = tron.get_account_balance(owner)
        if balance_trx <= Decimal("0"):
            return

        reserve = Decimal("1")
        send_amount = Decimal(str(balance_trx)) - reserve

        if send_amount <= Decimal("0"):
            print(f"[TRON SWEEP] Not enough TRX to sweep from {owner}")
            return

        txn = (
            tron.trx.transfer(owner, MAIN_TRON_WALLET, int(send_amount * Decimal("1000000")))
            .memo("Fund2Say sweep")
            .build()
            .sign(pk)
        )
        result = txn.broadcast()

        print(f"[TRON SWEEP] {owner} -> {MAIN_TRON_WALLET} | result={result}")

    except Exception as e:
        print(f"[TRON SWEEP ERROR] {e}")

from web3 import Web3

w3 = Web3(Web3.HTTPProvider("https://eth.llamarpc.com"))


def sweep_eth(private_key: str, from_address: str):
    try:
        from_address = Web3.to_checksum_address(from_address)
        to_address = Web3.to_checksum_address(MAIN_ETH_WALLET)

        balance = w3.eth.get_balance(from_address)

        if balance <= 0:
            return

        gas_price = w3.eth.gas_price
        gas_limit = 21000
        fee = gas_price * gas_limit

        if balance <= fee:
            print(f"[SWEEP] Not enough ETH to cover gas for {from_address}")
            return

        value = balance - fee
        nonce = w3.eth.get_transaction_count(from_address)

        tx = {
            "nonce": nonce,
            "to": to_address,
            "value": value,
            "gas": gas_limit,
            "gasPrice": gas_price,
            "chainId": 1,
        }

        signed_tx = w3.eth.account.sign_transaction(tx, private_key)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)

        print(f"[SWEEP] Sent {value} wei → {to_address} | tx: {tx_hash.hex()}")

    except Exception as e:
        print(f"[SWEEP ERROR] {e}")

def verify_btc_like(tx_hash: str, coin: str, prices: Dict[str, Decimal]) -> Tuple[bool, str, Decimal, Decimal]:
    chain_slug = "bitcoin" if coin == "BTC" else "litecoin"
    resp = requests.get(
        f"https://api.blockchair.com/{chain_slug}/dashboards/transaction/{tx_hash}",
        timeout=25,
    )
    data = resp.json()
    tx_block = data.get("data", {}).get(tx_hash)
    if not tx_block:
        return False, f"{coin} transaction not found.", Decimal("0"), Decimal("0")

    outputs = tx_block.get("outputs", [])
    target = WALLETS[coin]
    satoshis = 0
    for out in outputs:
        recipient = out.get("recipient")
        if recipient == target:
            satoshis += int(out.get("value", 0))

    if satoshis <= 0:
        return False, f"This {coin} transaction was not sent to the Fund2Say {coin} address.", Decimal("0"), Decimal("0")

    amount_coin = Decimal(satoshis) / Decimal(10**8)
    amount_usd = (amount_coin * prices[coin]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return True, "ok", amount_coin, amount_usd


def verify_sol(tx_hash: str, prices: Dict[str, Decimal]) -> Tuple[bool, str, Decimal, Decimal]:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [tx_hash, {"encoding": "json", "maxSupportedTransactionVersion": 0}],
    }
    resp = requests.post("https://api.mainnet-beta.solana.com", json=body, timeout=25)
    data = resp.json()
    result = data.get("result")
    if not result:
        return False, "SOL transaction not found.", Decimal("0"), Decimal("0")

    meta = result.get("meta") or {}
    if meta.get("err") is not None:
        return False, "SOL transaction is not confirmed successfully.", Decimal("0"), Decimal("0")

    message = (result.get("transaction") or {}).get("message") or {}
    account_keys = message.get("accountKeys") or []

    try:
        idx = account_keys.index(WALLETS["SOL"])
    except ValueError:
        return False, "This SOL transaction does not involve the Fund2Say SOL address.", Decimal("0"), Decimal("0")

    pre_bal = meta.get("preBalances", [])
    post_bal = meta.get("postBalances", [])
    if idx >= len(pre_bal) or idx >= len(post_bal):
        return False, "Could not verify SOL amount.", Decimal("0"), Decimal("0")

    lamports_received = int(post_bal[idx]) - int(pre_bal[idx])
    if lamports_received <= 0:
        return False, "No incoming SOL was detected for the Fund2Say SOL address.", Decimal("0"), Decimal("0")

    amount_coin = Decimal(lamports_received) / Decimal(10**9)
    amount_usd = (amount_coin * prices["SOL"]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return True, "ok", amount_coin, amount_usd


def verify_xrp(tx_hash: str, prices: Dict[str, Decimal]) -> Tuple[bool, str, Decimal, Decimal]:
    resp = requests.get(f"https://data.ripple.com/v2/transactions/{tx_hash}", timeout=20)
    data = resp.json()
    tx = data.get("transaction")
    if not tx:
        return False, "XRP transaction not found.", Decimal("0"), Decimal("0")

    outcome = data.get("outcome") or {}
    if outcome.get("result") != "tesSUCCESS":
        return False, "XRP transaction is not confirmed successfully.", Decimal("0"), Decimal("0")

    destination = tx.get("Destination")
    if destination != WALLETS["XRP"]:
        return False, "This XRP transaction was not sent to the Fund2Say XRP address.", Decimal("0"), Decimal("0")

    amount_drops = tx.get("Amount")
    if not amount_drops or not str(amount_drops).isdigit():
        return False, "Could not verify XRP amount.", Decimal("0"), Decimal("0")

    amount_coin = Decimal(amount_drops) / Decimal(10**6)
    amount_usd = (amount_coin * prices["XRP"]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return True, "ok", amount_coin, amount_usd


def verify_tron(tx_hash: str, prices: Dict[str, Decimal]) -> Tuple[bool, str, Decimal, Decimal]:
    resp = requests.get(f"https://apilist.tronscanapi.com/api/transaction-info?hash={tx_hash}", timeout=20)
    data = resp.json()
    if not data or data.get("code"):
        return False, "TRON transaction not found.", Decimal("0"), Decimal("0")

    if data.get("confirmed") is False:
        return False, "TRON transaction is not confirmed yet.", Decimal("0"), Decimal("0")

    to_addr = data.get("toAddress")
    if to_addr != WALLETS["TRON"]:
        return False, "This TRON transaction was not sent to the Fund2Say TRON address.", Decimal("0"), Decimal("0")

    amount_sun = int(data.get("amount", 0))
    if amount_sun <= 0:
        return False, "Could not verify TRON amount.", Decimal("0"), Decimal("0")

    amount_coin = Decimal(amount_sun) / Decimal(10**6)
    amount_usd = (amount_coin * prices["TRON"]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return True, "ok", amount_coin, amount_usd


def verify_ton(tx_hash: str, prices: Dict[str, Decimal]) -> Tuple[bool, str, Decimal, Decimal]:
    resp = requests.get(
        "https://toncenter.com/api/v2/getTransactions",
        params={"address": WALLETS["TON"], "limit": 20},
        timeout=25,
    )
    data = resp.json()
    txs = data.get("result", [])
    for tx in txs:
        tx_id = tx.get("transaction_id", {})
        current_hash = tx_id.get("hash")
        if current_hash != tx_hash:
            continue

        in_msg = tx.get("in_msg") or {}
        value = int(in_msg.get("value", 0))
        if value <= 0:
            return False, "No incoming TON was detected for the Fund2Say TON address.", Decimal("0"), Decimal("0")

        amount_coin = Decimal(value) / Decimal(10**9)
        amount_usd = (amount_coin * prices["TON"]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return True, "ok", amount_coin, amount_usd

    return False, "TON transaction not found in recent wallet history.", Decimal("0"), Decimal("0")


def verify_claim(coin: str, tx_hash: str) -> Tuple[bool, str, Decimal, Decimal]:
    coin = coin.upper().strip()
    prices = get_price_map()

    if coin == "ETH":
        return verify_eth(tx_hash, prices)
    if coin == "BTC":
        return verify_btc_like(tx_hash, "BTC", prices)
    if coin == "LTC":
        return verify_btc_like(tx_hash, "LTC", prices)
    if coin == "SOL":
        return verify_sol(tx_hash, prices)
    if coin == "XRP":
        return verify_xrp(tx_hash, prices)
    if coin == "TRON":
        return verify_tron(tx_hash, prices)
    if coin == "TON":
        return verify_ton(tx_hash, prices)

    return False, "Unsupported coin. Use BTC, ETH, XRP, SOL, TRON, LTC, or TON.", Decimal("0"), Decimal("0")


# =========================
# MESSAGE POSTING
# =========================
def build_public_post(user_id: int, display_name: str, cost: Decimal, message: str) -> str:
    safe_name = html.escape(display_name)
    safe_message = html.escape(message)
    return (
        f'<a href="tg://user?id={user_id}">{safe_name}</a> sent {html.escape(format_usd(cost))} to say:\n\n'
        f'“{safe_message}”'
    )


def build_anonymous_post(cost: Decimal, message: str) -> str:
    safe_message = html.escape(message)
    return f"Anonymous sent {html.escape(format_usd(cost))} to say:\n\n“{safe_message}”"


def can_post_now(user_id: int) -> Tuple[bool, int]:
    user = get_user(user_id)
    last_post_at = int(user["last_post_at"] or 0)
    now = int(time.time())
    remaining = POST_COOLDOWN_SECONDS - (now - last_post_at)
    if remaining > 0:
        return False, remaining
    return True, 0


# =========================
# HANDLERS
# =========================
def handle_start(chat_id: int, user_id: int) -> None:
    set_state(user_id, None, None, None)
    send_message(chat_id, welcome_text(), reply_markup=main_menu_keyboard(), parse_mode="HTML")


def handle_help(chat_id: int) -> None:
    send_message(chat_id, help_text(), reply_markup=main_menu_keyboard(), parse_mode="HTML")


def handle_deposit(chat_id: int) -> None:
    send_message(chat_id, "Choose a coin to deposit with:", reply_markup=deposit_keyboard())


def handle_show_coin(chat_id: int, coin: str, user_id: int) -> None:
    if coin == "ETH":
        address = get_or_create_eth_address(user_id)
        text = (
            f"💎 <b>{coin} Deposit</b>\n\n"
            f"<code>{html.escape(address)}</code>\n\n"
            "Send ETH directly to this address.\n"
            "Your balance updates automatically."
        )
    elif coin == "TRON":
        address = get_or_create_tron_address(user_id)
        text = (
            f"💎 <b>{coin} Deposit</b>\n\n"
            f"<code>{html.escape(address)}</code>\n\n"
            "Send TRX directly to this address.\n"
            "Your balance updates automatically."
        )
    else:
        address = WALLETS[coin]
        text = (
            f"<b>{coin} Deposit</b>\n\n"
            f"<code>{html.escape(address)}</code>\n\n"
            f"After sending, claim with:\n"
            f"<code>/claim {coin} TX_HASH</code>"
        )

    send_message(chat_id, text, reply_markup=deposit_keyboard(), parse_mode="HTML")


def handle_balance(chat_id: int, user_id: int) -> None:
    bal = get_balance(user_id)
    send_message(
        chat_id,
        f"💰 <b>Your Balance</b>\n\n{format_usd(bal)}",
        reply_markup=main_menu_keyboard(),
        parse_mode="HTML",
    )

def handle_send_begin(chat_id: int, user_id: int) -> None:
    bal = get_balance(user_id)
    minimum_cost = PRICE_PER_CHAR * MIN_CHARS
    send_message(
        chat_id,
        (
            "Send the message you want posted.\n\n"
            f"• Price: {format_usd(PRICE_PER_CHAR)} per character\n"
            f"• Minimum: {MIN_CHARS} chars ({format_usd(minimum_cost)})\n"
            f"• Maximum: {MAX_CHARS} chars\n"
            f"• Your balance: {format_usd(bal)}"
        ),
        reply_markup=main_menu_keyboard(),
    )
    set_state(user_id, "awaiting_message")


def handle_claim(chat_id: int, user_id: int, text: str) -> None:
    parts = text.strip().split(maxsplit=2)
    if len(parts) != 3:
        send_message(chat_id, "Use this format:\n/claim COIN TX_HASH", reply_markup=main_menu_keyboard())
        return

    _, coin, tx_hash = parts
    coin = coin.upper()

    # 🔥 DISABLE ETH CLAIMS
    if coin == "ETH":
        send_message(
            chat_id,
            "ETH deposits are detected automatically.\n\nUse Deposit → ETH and send directly to your assigned address.",
            reply_markup=main_menu_keyboard(),
        )
        return

    if coin not in WALLETS:
        send_message(chat_id, "Unsupported coin. Use BTC, ETH, XRP, SOL, TRON, LTC, or TON.")
        return

    if is_tx_already_claimed(tx_hash):
        send_message(chat_id, "This transaction has already been claimed.", reply_markup=main_menu_keyboard())
        return

    send_message(chat_id, f"Checking {coin} transaction...\nThis can take a few seconds.")

    try:
        ok, msg, amount_coin, amount_usd = verify_claim(coin, tx_hash)
    except Exception as e:
        send_message(chat_id, f"Claim check failed.\n{str(e)}", reply_markup=main_menu_keyboard())
        return

    if not ok:
        send_message(chat_id, msg, reply_markup=main_menu_keyboard())
        return

    new_balance = add_balance(user_id, amount_usd)
    save_claim(coin, tx_hash, amount_coin, amount_usd, user_id)

    send_message(
        chat_id,
        (
            f"✅ Deposit claimed\n\n"
            f"Coin: {coin}\n"
            f"Amount: {amount_coin.normalize()} {coin}\n"
            f"Credit added: {format_usd(amount_usd)}\n"
            f"New balance: {format_usd(new_balance)}"
        ),
        reply_markup=main_menu_keyboard(),
    )


def handle_text_message(chat_id: int, user_id: int, text: str, display_name: str) -> None:
    user = get_user(user_id)
    state = user["state"]

    if text.startswith("/start"):
        handle_start(chat_id, user_id)
        return

    if text.startswith("/help"):
        handle_help(chat_id)
        return

    if text.startswith("/balance"):
        handle_balance(chat_id, user_id)
        return

    if text.startswith("/deposit"):
        handle_deposit(chat_id)
        return

    if text.startswith("/send"):
        handle_send_begin(chat_id, user_id)
        return

    if text.startswith("/claim"):
        handle_claim(chat_id, user_id, text)
        return

    if state == "awaiting_message":
        message = text.strip()
        char_count = len(message)

        if char_count < MIN_CHARS:
            send_message(chat_id, f"Your message is too short. Minimum is {MIN_CHARS} characters.")
            return

        if char_count > MAX_CHARS:
            send_message(chat_id, f"Your message is too long. Maximum is {MAX_CHARS} characters.")
            return

        cost = cost_for_message(message)
        balance = get_balance(user_id)

        if balance < cost:
            send_message(
                chat_id,
                (
                    f"❌ Not enough balance.\n\n"
                    f"Message length: {char_count} characters\n"
                    f"Cost: {format_usd(cost)}\n"
                    f"Your balance: {format_usd(balance)}"
                ),
                reply_markup=main_menu_keyboard(),
            )
            set_state(user_id, None, None, None)
            return

        set_state(user_id, "awaiting_post_mode", pending_message=message, pending_cost=str(cost))
        send_message(
            chat_id,
            (
                f"Your message is {char_count} characters.\n"
                f"Cost: {format_usd(cost)}\n\n"
                "Choose how to post it:"
            ),
            reply_markup=post_choice_keyboard(),
        )
        return

    send_message(chat_id, "Use /start to open the menu.", reply_markup=main_menu_keyboard())


def handle_callback(callback_query: Dict[str, Any]) -> None:
    callback_id = callback_query["id"]
    data = callback_query["data"]
    msg = callback_query["message"]
    chat_id = msg["chat"]["id"]
    from_user = callback_query["from"]
    user_id = from_user["id"]
    username = from_user.get("username")
    first_name = from_user.get("first_name") or "User"
    display_name = first_name.strip() or "User"

    update_user_profile(user_id, username, first_name)
    answer_callback(callback_id)

    if data == "menu_home":
        handle_start(chat_id, user_id)
        return
    if data == "menu_deposit":
        handle_deposit(chat_id)
        return
    if data == "menu_balance":
        handle_balance(chat_id, user_id)
        return
    if data == "menu_send":
        handle_send_begin(chat_id, user_id)
        return
    if data == "menu_help":
        handle_help(chat_id)
        return
    if data.startswith("deposit_"):
        coin = data.split("_", 1)[1]
        if coin in WALLETS:
            handle_show_coin(chat_id, coin, user_id)
        return

    user = get_user(user_id)

    if data == "cancel_post":
        set_state(user_id, None, None, None)
        send_message(chat_id, "Posting cancelled.", reply_markup=main_menu_keyboard())
        return

    if data in ("post_public", "post_anon"):
        pending_message = user["pending_message"]
        pending_cost = user["pending_cost"]
        if not pending_message or not pending_cost:
            send_message(
                chat_id,
                "No pending message found. Tap Send Message and try again.",
                reply_markup=main_menu_keyboard(),
            )
            set_state(user_id, None, None, None)
            return

        cost = Decimal(pending_cost)
        if data == "post_public":
            preview = build_public_post(user_id, display_name, cost, pending_message)
            send_message(
                chat_id,
                f"<b>Preview</b>\n\n{preview}",
                parse_mode="HTML",
                reply_markup=confirm_keyboard("public"),
            )
        else:
            preview = build_anonymous_post(cost, pending_message)
            send_message(
                chat_id,
                f"<b>Preview</b>\n\n{html.escape(preview)}",
                parse_mode="HTML",
                reply_markup=confirm_keyboard("anon"),
            )
        return

    if data in ("confirm_public", "confirm_anon"):
        pending_message = user["pending_message"]
        pending_cost = user["pending_cost"]
        if not pending_message or not pending_cost:
            send_message(chat_id, "No pending message found.", reply_markup=main_menu_keyboard())
            set_state(user_id, None, None, None)
            return

        allowed, remaining = can_post_now(user_id)
        if not allowed:
            send_message(
                chat_id,
                f"Please wait {remaining} seconds before posting again.",
                reply_markup=main_menu_keyboard(),
            )
            return

        cost = Decimal(pending_cost)
        ok, new_balance = deduct_balance(user_id, cost)
        if not ok:
            send_message(
                chat_id,
                "Your balance is no longer enough for this post.",
                reply_markup=main_menu_keyboard(),
            )
            set_state(user_id, None, None, None)
            return

        if data == "confirm_public":
            post = build_public_post(user_id, display_name, cost, pending_message)
            send_message(CHANNEL_ID, post, parse_mode="HTML")
        else:
            post = build_anonymous_post(cost, pending_message)
            send_message(CHANNEL_ID, post)

        set_state(user_id, None, None, None)
        send_message(
            chat_id,
            (
                f"✅ Message posted\n"
                f"Charged: {format_usd(cost)}\n"
                f"Remaining balance: {format_usd(new_balance)}"
            ),
            reply_markup=main_menu_keyboard(),
        )


def handle_update(update: Dict[str, Any]) -> None:
    if "message" in update:
        message = update["message"]
        chat = message.get("chat", {})
        if chat.get("type") != "private":
            return

        from_user = message.get("from", {})
        user_id = from_user.get("id")
        if not user_id:
            return

        username = from_user.get("username")
        first_name = from_user.get("first_name") or "User"
        display_name = first_name.strip() or "User"
        update_user_profile(user_id, username, first_name)

        text = message.get("text")
        if text:
            handle_text_message(chat["id"], user_id, text, display_name)

    elif "callback_query" in update:
        handle_callback(update["callback_query"])

def main() -> None:
    init_db()
    print("Fund2Say bot is running...")
    offset: Optional[int] = None

    while True:
        try:
            check_eth_deposits()

            data = get_updates(offset)
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                handle_update(update)

        except Exception as e:
            print(f"Error: {e}")
            time.sleep(3)

        time.sleep(10)


if __name__ == "__main__":
    main()
