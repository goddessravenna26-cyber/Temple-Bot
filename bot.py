#!/usr/bin/env python3
"""
bot.py — Monero-gated Telegram subscription bot.

Architecture
------------
  * python-telegram-bot v20+ (JobQueue for background loops)
  * monero-wallet-rpc in VIEW-ONLY mode, bound to loopback (see SECURITY below)
  * aiosqlite for embedded persistence
  * CoinGecko public API for USD->XMR quoting

SECURITY NOTES (read before deploying)
--------------------------------------
  1. There is no such thing as a safe "public wallet RPC". monero-wallet-rpc
     exposes wallet control. Run it yourself, bound to 127.0.0.1, with digest
     auth, and point it at a remote *daemon* if you don't want to run a node:

       monero-wallet-rpc \
         --rpc-bind-ip 127.0.0.1 --rpc-bind-port 18083 \
         --rpc-login user:pass \
         --daemon-address https://node.example.org:18081 \
         --wallet-file /srv/xmr/viewonly.keys --password-file /srv/xmr/pw \
         --disable-rpc-login=0

  2. Use a VIEW-ONLY wallet. Primary address + private view key is sufficient
     to derive subaddresses and observe incoming transfers, and it means a
     host compromise cannot spend your funds. Create it with:
       monero-wallet-cli --generate-from-view-key viewonly

  3. Admin auth over Telegram is inherently weak — the password lands in
     Telegram's message history and on their servers. This script hashes the
     secret, compares in constant time, deletes the command message, and
     expires sessions. It is still second-class to an out-of-band channel.

Environment variables (all required unless noted)
-------------------------------------------------
  TELEGRAM_BOT_TOKEN      Bot token from @BotFather
  TELEGRAM_CHANNEL_ID     Target channel, e.g. -1001234567890
  ADMIN_USER_ID           Numeric Telegram user id of the admin
  ADMIN_PASSWORD          Admin panel secret
  MONERO_RPC_URL          e.g. http://127.0.0.1:18083/json_rpc
  MONERO_RPC_USER         Digest auth user           (optional)
  MONERO_RPC_PASSWORD     Digest auth password       (optional)
  MONERO_PRIMARY_ADDRESS  Primary (standard) address of the view-only wallet
  MONERO_ACCOUNT_INDEX    Wallet account index, default 0
  SUBSCRIPTION_USD        Default 5000
  SUBSCRIPTION_DAYS       Default 30
  RENEWAL_WINDOW_DAYS     Default 7
  REQUIRED_CONFIRMATIONS  Default 10
  AMOUNT_TOLERANCE_PCT    Default 2.0
  QUOTE_TTL_HOURS         Default 24
  INVITE_TTL_HOURS        Default 24
  POLL_INTERVAL_SECONDS   Default 60
  DB_PATH                 Default ./membership.db
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Any, Iterable, Optional

import aiosqlite
import requests
from requests.auth import HTTPDigestAuth
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

ATOMIC_UNITS = Decimal(10) ** 12  # 1 XMR == 1e12 piconero
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

logging.basicConfig(format=LOG_FORMAT, level=logging.INFO, stream=sys.stdout)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
log = logging.getLogger("temple-bot")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"FATAL: required environment variable {name} is not set.")
    return value


def _opt_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"FATAL: {name} must be an integer, got {raw!r}")


def _opt_dec(name: str, default: str) -> Decimal:
    raw = os.getenv(name, default)
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise SystemExit(f"FATAL: {name} must be numeric, got {raw!r}")


@dataclass(frozen=True)
class Config:
    bot_token: str
    channel_id: int
    admin_user_id: int
    admin_password: str
    rpc_url: str
    rpc_user: Optional[str]
    rpc_password: Optional[str]
    primary_address: str
    account_index: int
    subscription_usd: Decimal
    subscription_days: int
    renewal_window_days: int
    required_confirmations: int
    tolerance_pct: Decimal
    quote_ttl_hours: int
    invite_ttl_hours: int
    poll_interval: int
    db_path: str

    @classmethod
    def from_env(cls) -> "Config":
        try:
            channel_id = int(_require("TELEGRAM_CHANNEL_ID"))
            admin_user_id = int(_require("ADMIN_USER_ID"))
        except ValueError:
            raise SystemExit(
                "FATAL: TELEGRAM_CHANNEL_ID and ADMIN_USER_ID must be integers."
            )
        return cls(
            bot_token=_require("TELEGRAM_BOT_TOKEN"),
            channel_id=channel_id,
            admin_user_id=admin_user_id,
            admin_password=_require("ADMIN_PASSWORD"),
            rpc_url=_require("MONERO_RPC_URL"),
            rpc_user=os.getenv("MONERO_RPC_USER") or None,
            rpc_password=os.getenv("MONERO_RPC_PASSWORD") or None,
            primary_address=_require("MONERO_PRIMARY_ADDRESS"),
            account_index=_opt_int("MONERO_ACCOUNT_INDEX", 0),
            subscription_usd=_opt_dec("SUBSCRIPTION_USD", "5000"),
            subscription_days=_opt_int("SUBSCRIPTION_DAYS", 30),
            renewal_window_days=_opt_int("RENEWAL_WINDOW_DAYS", 7),
            required_confirmations=_opt_int("REQUIRED_CONFIRMATIONS", 10),
            tolerance_pct=_opt_dec("AMOUNT_TOLERANCE_PCT", "2.0"),
            quote_ttl_hours=_opt_int("QUOTE_TTL_HOURS", 24),
            invite_ttl_hours=_opt_int("INVITE_TTL_HOURS", 24),
            poll_interval=_opt_int("POLL_INTERVAL_SECONDS", 60),
            db_path=os.getenv("DB_PATH", "membership.db"),
        )


CFG = Config.from_env()
ADMIN_PW_DIGEST = hashlib.sha256(CFG.admin_password.encode("utf-8")).digest()
ADMIN_SESSION_TTL = 900  # seconds


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def now_ts() -> int:
    return int(time.time())


def fmt_ts(ts: Optional[int]) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def atomic_to_xmr(atomic: int) -> Decimal:
    return (Decimal(atomic) / ATOMIC_UNITS).quantize(Decimal("0.000000000001"))


def fmt_xmr(atomic: int) -> str:
    """Render atomic units as a trimmed XMR string."""
    s = format(atomic_to_xmr(atomic), "f").rstrip("0").rstrip(".")
    return s or "0"


def escape_md(text: Any) -> str:
    """Escape for Telegram MarkdownV2."""
    out = str(text)
    for ch in r"_*[]()~`>#+-=|{}.!\\":
        out = out.replace(ch, "\\" + ch)
    return out


# --------------------------------------------------------------------------- #
# Monero wallet RPC client
# --------------------------------------------------------------------------- #


class MoneroRPCError(RuntimeError):
    pass


class MoneroWallet:
    """Thin JSON-RPC client for monero-wallet-rpc.

    Uses `requests` per spec, but every call is dispatched through
    asyncio.to_thread so the Telegram event loop is never blocked.
    """

    def __init__(self, cfg: Config) -> None:
        self._url = cfg.rpc_url
        self._account = cfg.account_index
        self._session = requests.Session()
        if cfg.rpc_user is not None:
            self._session.auth = HTTPDigestAuth(cfg.rpc_user, cfg.rpc_password or "")
        self._rpc_id = 0
        self._lock = asyncio.Lock()

    def _call_sync(self, method: str, params: Optional[dict] = None) -> dict:
        self._rpc_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": str(self._rpc_id),
            "method": method,
            "params": params or {},
        }
        try:
            resp = self._session.post(self._url, json=payload, timeout=30)
            resp.raise_for_status()
            body = resp.json()
        except requests.RequestException as exc:
            raise MoneroRPCError(f"transport failure calling {method}: {exc}") from exc
        except ValueError as exc:
            raise MoneroRPCError(f"non-JSON response from {method}") from exc

        if "error" in body and body["error"]:
            err = body["error"]
            raise MoneroRPCError(
                f"{method} failed: [{err.get('code')}] {err.get('message')}"
            )
        return body.get("result", {}) or {}

    async def call(self, method: str, params: Optional[dict] = None) -> dict:
        async with self._lock:
            return await asyncio.to_thread(self._call_sync, method, params)

    async def verify(self, expected_primary: str) -> None:
        """Startup sanity check: wallet is reachable and is the right wallet."""
        result = await self.call("get_address", {"account_index": self._account})
        actual = result.get("address", "")
        if actual != expected_primary:
            raise MoneroRPCError(
                "wallet RPC primary address does not match MONERO_PRIMARY_ADDRESS. "
                f"RPC reports {actual[:12]}…, env expects {expected_primary[:12]}…"
            )
        log.info("Monero wallet RPC verified (account %d).", self._account)

    async def create_subaddress(self, label: str) -> tuple[str, int]:
        result = await self.call(
            "create_address", {"account_index": self._account, "label": label}
        )
        address = result.get("address")
        index = result.get("address_index")
        if not address or index is None:
            raise MoneroRPCError("create_address returned an incomplete result")
        return address, int(index)

    async def incoming_transfers(self, subaddr_indices: Iterable[int]) -> list[dict]:
        indices = sorted(set(int(i) for i in subaddr_indices))
        if not indices:
            return []
        result = await self.call(
            "get_transfers",
            {
                "in": True,
                "out": False,
                "pending": False,
                "failed": False,
                "pool": False,
                "account_index": self._account,
                "subaddr_indices": indices,
            },
        )
        return list(result.get("in", []) or [])


# --------------------------------------------------------------------------- #
# Price oracle
# --------------------------------------------------------------------------- #


class PriceError(RuntimeError):
    pass


class PriceOracle:
    """CoinGecko spot price with short-lived cache and bounded retries.

    Deliberately does NOT serve a stale price beyond the cache window — a
    wrong quote on a high-value invoice is worse than a failed quote.
    """

    CACHE_TTL = 60  # seconds

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        self._cached: Optional[Decimal] = None
        self._cached_at = 0
        self._lock = asyncio.Lock()

    def _fetch_sync(self) -> Decimal:
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                resp = self._session.get(
                    COINGECKO_URL,
                    params={"ids": "monero", "vs_currencies": "usd"},
                    timeout=15,
                )
                resp.raise_for_status()
                price = resp.json()["monero"]["usd"]
                value = Decimal(str(price))
                if value <= 0:
                    raise PriceError(f"implausible XMR price: {value}")
                return value
            except Exception as exc:  # noqa: BLE001 - retried and re-raised below
                last_exc = exc
                time.sleep(1.5 * (attempt + 1))
        raise PriceError(f"could not obtain XMR/USD price: {last_exc}")

    async def xmr_usd(self) -> Decimal:
        async with self._lock:
            if self._cached is not None and now_ts() - self._cached_at < self.CACHE_TTL:
                return self._cached
            value = await asyncio.to_thread(self._fetch_sync)
            self._cached = value
            self._cached_at = now_ts()
            return value

    async def usd_to_atomic(self, usd: Decimal) -> tuple[int, Decimal]:
        """Return (atomic_units, xmr_usd_rate) for a USD amount."""
        rate = await self.xmr_usd()
        xmr = (usd / rate).quantize(Decimal("0.000000000001"), rounding=ROUND_HALF_UP)
        return int(xmr * ATOMIC_UNITS), rate


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    user_id           INTEGER PRIMARY KEY,
    username          TEXT,
    subaddress        TEXT NOT NULL UNIQUE,
    subaddress_index  INTEGER NOT NULL UNIQUE,
    status            TEXT NOT NULL DEFAULT 'pending',
    expiration_date   INTEGER,
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS quotes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    usd_amount      TEXT NOT NULL,
    xmr_usd_rate    TEXT NOT NULL,
    required_atomic INTEGER NOT NULL,
    created_at      INTEGER NOT NULL,
    expires_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quotes_user ON quotes(user_id, expires_at);

CREATE TABLE IF NOT EXISTS payments (
    txid            TEXT PRIMARY KEY,
    user_id         INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    subaddr_index   INTEGER,
    amount_atomic   INTEGER NOT NULL,
    confirmations   INTEGER NOT NULL,
    block_height    INTEGER,
    classification  TEXT NOT NULL,          -- activation | renewal | bonus_tribute
    detail          TEXT,
    processed_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id, processed_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER NOT NULL,
    event      TEXT NOT NULL,
    user_id    INTEGER,
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
"""


class Database:
    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        log.info("SQLite ready at %s", self._path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("database not connected")
        return self._conn

    # -- audit ------------------------------------------------------------- #

    async def log_event(
        self, event: str, user_id: Optional[int] = None, detail: str = ""
    ) -> None:
        await self.conn.execute(
            "INSERT INTO audit_log (ts, event, user_id, detail) VALUES (?,?,?,?)",
            (now_ts(), event, user_id, detail),
        )
        await self.conn.commit()
        log.info("AUDIT %s user=%s %s", event, user_id, detail)

    # -- users ------------------------------------------------------------- #

    async def get_user(self, user_id: int) -> Optional[aiosqlite.Row]:
        async with self.conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            return await cur.fetchone()

    async def get_user_by_subaddr_index(self, index: int) -> Optional[aiosqlite.Row]:
        async with self.conn.execute(
            "SELECT * FROM users WHERE subaddress_index = ?", (index,)
        ) as cur:
            return await cur.fetchone()

    async def create_user(
        self, user_id: int, username: Optional[str], subaddress: str, index: int
    ) -> None:
        ts = now_ts()
        await self.conn.execute(
            "INSERT INTO users (user_id, username, subaddress, subaddress_index, "
            "status, expiration_date, created_at, updated_at) "
            "VALUES (?,?,?,?,'pending',NULL,?,?)",
            (user_id, username, subaddress, index, ts, ts),
        )
        await self.conn.commit()

    async def touch_username(self, user_id: int, username: Optional[str]) -> None:
        await self.conn.execute(
            "UPDATE users SET username = ?, updated_at = ? WHERE user_id = ?",
            (username, now_ts(), user_id),
        )
        await self.conn.commit()

    async def set_membership(
        self, user_id: int, status: str, expiration: Optional[int]
    ) -> None:
        await self.conn.execute(
            "UPDATE users SET status = ?, expiration_date = ?, updated_at = ? "
            "WHERE user_id = ?",
            (status, expiration, now_ts(), user_id),
        )
        await self.conn.commit()

    async def all_subaddress_indices(self) -> list[int]:
        async with self.conn.execute("SELECT subaddress_index FROM users") as cur:
            return [int(r[0]) for r in await cur.fetchall()]

    async def list_users(self, limit: int = 50) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            "SELECT * FROM users ORDER BY "
            "CASE status WHEN 'active' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END, "
            "expiration_date IS NULL, expiration_date ASC LIMIT ?",
            (limit,),
        ) as cur:
            return list(await cur.fetchall())

    async def count_by_status(self) -> dict[str, int]:
        async with self.conn.execute(
            "SELECT status, COUNT(*) FROM users GROUP BY status"
        ) as cur:
            return {row[0]: int(row[1]) for row in await cur.fetchall()}

    async def expired_active_users(self) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            "SELECT * FROM users WHERE status = 'active' "
            "AND expiration_date IS NOT NULL AND expiration_date <= ?",
            (now_ts(),),
        ) as cur:
            return list(await cur.fetchall())

    # -- quotes ------------------------------------------------------------ #

    async def insert_quote(
        self,
        user_id: int,
        usd: Decimal,
        rate: Decimal,
        required_atomic: int,
        ttl_hours: int,
    ) -> None:
        ts = now_ts()
        await self.conn.execute(
            "INSERT INTO quotes (user_id, usd_amount, xmr_usd_rate, required_atomic, "
            "created_at, expires_at) VALUES (?,?,?,?,?,?)",
            (user_id, str(usd), str(rate), required_atomic, ts, ts + ttl_hours * 3600),
        )
        await self.conn.commit()

    async def active_quotes(self, user_id: int) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            "SELECT * FROM quotes WHERE user_id = ? AND expires_at >= ? "
            "ORDER BY created_at DESC",
            (user_id, now_ts()),
        ) as cur:
            return list(await cur.fetchall())

    # -- payments ---------------------------------------------------------- #

    async def payment_seen(self, txid: str) -> bool:
        async with self.conn.execute(
            "SELECT 1 FROM payments WHERE txid = ?", (txid,)
        ) as cur:
            return await cur.fetchone() is not None

    async def record_payment(
        self,
        txid: str,
        user_id: Optional[int],
        subaddr_index: Optional[int],
        amount_atomic: int,
        confirmations: int,
        block_height: Optional[int],
        classification: str,
        detail: str,
    ) -> bool:
        """Insert a payment. Returns False if the txid was already recorded."""
        try:
            await self.conn.execute(
                "INSERT INTO payments (txid, user_id, subaddr_index, amount_atomic, "
                "confirmations, block_height, classification, detail, processed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    txid,
                    user_id,
                    subaddr_index,
                    amount_atomic,
                    confirmations,
                    block_height,
                    classification,
                    detail,
                    now_ts(),
                ),
            )
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def list_payments(self, limit: int = 25) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            "SELECT * FROM payments ORDER BY processed_at DESC LIMIT ?", (limit,)
        ) as cur:
            return list(await cur.fetchall())

    async def list_audit(self, limit: int = 25) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            "SELECT * FROM audit_log ORDER BY ts DESC LIMIT ?", (limit,)
        ) as cur:
            return list(await cur.fetchall())


# --------------------------------------------------------------------------- #
# Message templates
# --------------------------------------------------------------------------- #

START_TEMPLATE = (
    "The gates of the Temple are before you. Your presence has been recognized, "
    "and your anonymity is held sacred.\n\n"
    "To complete your entry and establish your 30-day membership, your unique "
    "subaddress has been generated below.\n\n"
    "Your Sacred Payment Address:\n{subaddress}\n\n"
    "Required Tribute: Exactly {xmr_amount} XMR "
    "(Calculated dynamically to equal $5,000 USD)\n\n"
    "Our system is currently monitoring the Monero blockchain. The moment your "
    "transaction receives full confirmation, this bot will automatically DM you a "
    "single-use, private invitation link to enter the inner channel.\n\n"
    "Note: The invitation link will expire exactly 24 hours after issuance. "
    "Subaddresses and burner Telegram accounts are fully welcome to preserve the "
    "highest echelon of protection."
)


# --------------------------------------------------------------------------- #
# Core service
# --------------------------------------------------------------------------- #


class TempleService:
    def __init__(self, cfg: Config, db: Database, wallet: MoneroWallet,
                 oracle: PriceOracle) -> None:
        self.cfg = cfg
        self.db = db
        self.wallet = wallet
        self.oracle = oracle
        self.admin_sessions: dict[int, float] = {}

    # -- notifications ----------------------------------------------------- #

    async def notify_admin(self, app: Application, text: str) -> None:
        try:
            await app.bot.send_message(
                chat_id=self.cfg.admin_user_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except TelegramError as exc:
            log.error("Failed to notify admin: %s", exc)

    async def dm_user(self, app: Application, user_id: int, text: str) -> bool:
        try:
            await app.bot.send_message(chat_id=user_id, text=text)
            return True
        except Forbidden:
            log.warning("User %s has blocked the bot; DM not delivered.", user_id)
        except TelegramError as exc:
            log.error("DM to %s failed: %s", user_id, exc)
        return False

    # -- onboarding -------------------------------------------------------- #

    async def ensure_user(self, user_id: int, username: Optional[str]) -> aiosqlite.Row:
        row = await self.db.get_user(user_id)
        if row is not None:
            await self.db.touch_username(user_id, username)
            return row
        address, index = await self.wallet.create_subaddress(f"tg:{user_id}")
        await self.db.create_user(user_id, username, address, index)
        await self.db.log_event(
            "user_registered", user_id, f"subaddr_index={index}"
        )
        row = await self.db.get_user(user_id)
        assert row is not None
        return row

    async def issue_quote(self, user_id: int) -> tuple[int, Decimal]:
        atomic, rate = await self.oracle.usd_to_atomic(self.cfg.subscription_usd)
        await self.db.insert_quote(
            user_id, self.cfg.subscription_usd, rate, atomic, self.cfg.quote_ttl_hours
        )
        return atomic, rate

    # -- invites ----------------------------------------------------------- #

    async def issue_invite(self, app: Application, user_id: int) -> Optional[str]:
        expire_at = datetime.now(timezone.utc) + timedelta(
            hours=self.cfg.invite_ttl_hours
        )
        try:
            link = await app.bot.create_chat_invite_link(
                chat_id=self.cfg.channel_id,
                expire_date=expire_at,
                member_limit=1,
                name=f"u{user_id}-{now_ts()}",
            )
        except TelegramError as exc:
            log.error("createChatInviteLink failed for %s: %s", user_id, exc)
            await self.db.log_event("invite_failed", user_id, str(exc))
            return None

        delivered = await self.dm_user(
            app,
            user_id,
            "Your tribute has been confirmed on the Monero blockchain.\n\n"
            f"Your single-use invitation:\n{link.invite_link}\n\n"
            f"This link admits exactly one account and expires at "
            f"{expire_at.strftime('%Y-%m-%d %H:%M UTC')}.",
        )
        await self.db.log_event(
            "invite_issued",
            user_id,
            f"delivered={delivered} expires={fmt_ts(int(expire_at.timestamp()))}",
        )
        return link.invite_link

    # -- payment classification ------------------------------------------- #

    def _within_tolerance(self, received: int, required: int) -> bool:
        if required <= 0:
            return False
        delta = abs(Decimal(received) - Decimal(required))
        return (delta / Decimal(required)) * 100 <= self.cfg.tolerance_pct

    async def _matching_quote(
        self, user_id: int, amount_atomic: int
    ) -> Optional[aiosqlite.Row]:
        for quote in await self.db.active_quotes(user_id):
            if self._within_tolerance(amount_atomic, int(quote["required_atomic"])):
                return quote
        return None

    async def process_transfer(self, app: Application, transfer: dict) -> None:
        txid = transfer.get("txid")
        if not txid:
            return

        confirmations = int(transfer.get("confirmations") or 0)
        if confirmations < self.cfg.required_confirmations:
            return
        if transfer.get("double_spend_seen"):
            log.warning("Ignoring tx %s: double spend seen.", txid)
            return
        if await self.db.payment_seen(txid):
            return

        amount = int(transfer.get("amount") or 0)
        height = transfer.get("height")
        subaddr = transfer.get("subaddr_index") or {}
        minor = subaddr.get("minor")
        if minor is None:
            return

        user = await self.db.get_user_by_subaddr_index(int(minor))
        if user is None:
            await self.db.record_payment(
                txid, None, int(minor), amount, confirmations, height,
                "bonus_tribute", "unmapped subaddress index",
            )
            await self.notify_admin(
                app,
                f"*Bonus Tribute \\(unmapped\\)*\n"
                f"Amount: `{escape_md(fmt_xmr(amount))}` XMR\n"
                f"Subaddress index: `{escape_md(minor)}`\n"
                f"Tx: `{escape_md(txid)}`",
            )
            return

        user_id = int(user["user_id"])
        expiration = user["expiration_date"]
        quote = await self._matching_quote(user_id, amount)

        # --- wrong amount -> bonus tribute --------------------------------- #
        if quote is None:
            if not await self.db.record_payment(
                txid, user_id, int(minor), amount, confirmations, height,
                "bonus_tribute", "amount did not match any active quote",
            ):
                return
            await self.db.log_event(
                "bonus_tribute", user_id, f"amount={fmt_xmr(amount)} tx={txid}"
            )
            await self.notify_admin(
                app,
                f"*Bonus Tribute*\n"
                f"User: `{user_id}` \\(@{escape_md(user['username'] or 'n/a')}\\)\n"
                f"Amount: `{escape_md(fmt_xmr(amount))}` XMR\n"
                f"Reason: amount outside quoted tolerance\n"
                f"Membership: *not extended*\n"
                f"Tx: `{escape_md(txid)}`",
            )
            await self.dm_user(
                app,
                user_id,
                "A transfer to your subaddress has been confirmed, but the amount "
                "does not match your outstanding tribute. It has been logged as a "
                "bonus tribute and your membership was not altered. Contact the "
                "keeper if this is unexpected.",
            )
            return

        threshold = now_ts() + self.cfg.renewal_window_days * 86400

        # --- correct amount, but membership not near expiry ----------------- #
        if expiration is not None and int(expiration) > threshold:
            if not await self.db.record_payment(
                txid, user_id, int(minor), amount, confirmations, height,
                "bonus_tribute",
                f"membership valid beyond {self.cfg.renewal_window_days}d window",
            ):
                return
            await self.db.log_event(
                "bonus_tribute", user_id, f"early payment tx={txid}"
            )
            await self.notify_admin(
                app,
                f"*Bonus Tribute*\n"
                f"User: `{user_id}` \\(@{escape_md(user['username'] or 'n/a')}\\)\n"
                f"Amount: `{escape_md(fmt_xmr(amount))}` XMR\n"
                f"Reason: expiry is "
                f"{escape_md(fmt_ts(int(expiration)))}, outside the "
                f"{self.cfg.renewal_window_days}\\-day renewal window\n"
                f"Membership: *not extended*\n"
                f"Tx: `{escape_md(txid)}`",
            )
            await self.dm_user(
                app,
                user_id,
                "Your tribute has been confirmed, but your membership does not yet "
                f"fall within the {self.cfg.renewal_window_days}-day renewal window "
                f"(current expiry: {fmt_ts(int(expiration))}). It has been recorded "
                "as a bonus tribute and your membership was not extended.",
            )
            return

        # --- activation or renewal ------------------------------------------ #
        is_renewal = expiration is not None
        base = max(int(expiration), now_ts()) if is_renewal else now_ts()
        new_expiration = base + self.cfg.subscription_days * 86400
        classification = "renewal" if is_renewal else "activation"

        if not await self.db.record_payment(
            txid, user_id, int(minor), amount, confirmations, height,
            classification, f"new_expiration={new_expiration}",
        ):
            return

        await self.db.set_membership(user_id, "active", new_expiration)
        await self.db.log_event(
            classification, user_id, f"expires={fmt_ts(new_expiration)} tx={txid}"
        )

        title = "Membership Renewed" if is_renewal else "New Membership"
        await self.notify_admin(
            app,
            f"*{escape_md(title)}*\n"
            f"User: `{user_id}` \\(@{escape_md(user['username'] or 'n/a')}\\)\n"
            f"Amount: `{escape_md(fmt_xmr(amount))}` XMR\n"
            f"Expires: {escape_md(fmt_ts(new_expiration))}\n"
            f"Tx: `{escape_md(txid)}`",
        )
        await self.issue_invite(app, user_id)

    # -- expulsion --------------------------------------------------------- #

    async def expel(self, app: Application, user_id: int, reason: str) -> bool:
        """Remove a user from the channel without a permanent ban."""
        ok = True
        try:
            await app.bot.ban_chat_member(
                chat_id=self.cfg.channel_id,
                user_id=user_id,
                revoke_messages=False,
            )
            await asyncio.sleep(1)
            await app.bot.unban_chat_member(
                chat_id=self.cfg.channel_id,
                user_id=user_id,
                only_if_banned=True,
            )
        except BadRequest as exc:
            log.warning("Expulsion of %s returned: %s", user_id, exc)
            ok = False
        except TelegramError as exc:
            log.error("Expulsion of %s failed: %s", user_id, exc)
            ok = False

        await self.db.set_membership(user_id, "expired", None)
        await self.db.log_event("expelled", user_id, f"{reason} channel_removed={ok}")
        return ok


# --------------------------------------------------------------------------- #
# Background jobs
# --------------------------------------------------------------------------- #


async def job_poll_payments(context: ContextTypes.DEFAULT_TYPE) -> None:
    svc: TempleService = context.application.bot_data["svc"]
    try:
        indices = await svc.db.all_subaddress_indices()
        transfers = await svc.wallet.incoming_transfers(indices)
    except MoneroRPCError as exc:
        log.error("Payment poll failed: %s", exc)
        return

    for transfer in transfers:
        try:
            await svc.process_transfer(context.application, transfer)
        except Exception:  # noqa: BLE001 - one bad tx must not kill the loop
            log.exception("Error processing transfer %s", transfer.get("txid"))


async def job_enforce_expiry(context: ContextTypes.DEFAULT_TYPE) -> None:
    svc: TempleService = context.application.bot_data["svc"]
    expired = await svc.db.expired_active_users()
    if not expired:
        log.info("Expiry sweep: nothing to do.")
        return

    log.info("Expiry sweep: removing %d lapsed member(s).", len(expired))
    removed: list[str] = []
    for row in expired:
        user_id = int(row["user_id"])
        await svc.expel(context.application, user_id, "subscription lapsed")
        await svc.dm_user(
            context.application,
            user_id,
            "Your 30-day membership has lapsed and your access to the inner "
            "channel has been withdrawn. Send /start to receive a fresh tribute "
            "address and restore your standing.",
        )
        removed.append(f"`{user_id}` \\(@{escape_md(row['username'] or 'n/a')}\\)")

    await svc.notify_admin(
        context.application,
        "*Auto\\-Expulsion Sweep*\n"
        f"Removed {len(removed)} lapsed member\\(s\\):\n" + "\n".join(removed),
    )


# --------------------------------------------------------------------------- #
# User handlers
# --------------------------------------------------------------------------- #


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.effective_chat.type != "private":
        return
    user = update.effective_user
    if user is None:
        return
    svc: TempleService = context.application.bot_data["svc"]

    try:
        row = await svc.ensure_user(user.id, user.username)
    except MoneroRPCError as exc:
        log.error("Subaddress generation failed for %s: %s", user.id, exc)
        await update.message.reply_text(
            "The Temple's ledger is momentarily unreachable. Please try /start again "
            "in a few minutes."
        )
        return

    try:
        atomic, _rate = await svc.issue_quote(user.id)
    except PriceError as exc:
        log.error("Quote failed for %s: %s", user.id, exc)
        await update.message.reply_text(
            "The tribute rate could not be established just now. Please try /start "
            "again shortly."
        )
        return

    await update.message.reply_text(
        START_TEMPLATE.format(
            subaddress=row["subaddress"], xmr_amount=fmt_xmr(atomic)
        ),
        disable_web_page_preview=True,
    )
    await svc.db.log_event("quote_issued", user.id, f"required={fmt_xmr(atomic)} XMR")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.effective_chat.type != "private":
        return
    user = update.effective_user
    if user is None:
        return
    svc: TempleService = context.application.bot_data["svc"]

    row = await svc.db.get_user(user.id)
    if row is None:
        await update.message.reply_text("You are not yet known here. Send /start.")
        return

    if row["status"] == "active" and row["expiration_date"]:
        remaining = int(row["expiration_date"]) - now_ts()
        days = max(0, remaining // 86400)
        await update.message.reply_text(
            f"Status: active\n"
            f"Expires: {fmt_ts(int(row['expiration_date']))} ({days} day(s) left)\n"
            f"Your subaddress: {row['subaddress']}"
        )
    else:
        await update.message.reply_text(
            f"Status: {row['status']}\n"
            f"Your subaddress: {row['subaddress']}\n"
            "Send /start to receive a current tribute quote."
        )


# --------------------------------------------------------------------------- #
# Admin panel
# --------------------------------------------------------------------------- #

ADMIN_MENU = (
    "*Temple Administration*\n"
    "`/members` — membership roster\n"
    "`/payments` — payment ledger\n"
    "`/audit` — recent audit log\n"
    "`/extend <user_id> <days>` — grant or add time\n"
    "`/revoke <user_id>` — remove access immediately\n"
    "`/sweep` — run the expiry sweep now\n"
    "`/logout` — end this admin session\n\n"
    f"_Session expires after {ADMIN_SESSION_TTL // 60} minutes of inactivity\\._"
)


def _is_admin(update: Update) -> bool:
    return (
        update.effective_user is not None
        and update.effective_user.id == CFG.admin_user_id
    )


async def _admin_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Gate: correct user id AND a live authenticated session."""
    if not _is_admin(update):
        return False
    svc: TempleService = context.application.bot_data["svc"]
    expiry = svc.admin_sessions.get(update.effective_user.id, 0)
    if expiry < time.monotonic():
        svc.admin_sessions.pop(update.effective_user.id, None)
        await update.message.reply_text(
            "Admin session expired. Re-authenticate with /admin <password>."
        )
        return False
    svc.admin_sessions[update.effective_user.id] = time.monotonic() + ADMIN_SESSION_TTL
    return True


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.effective_chat.type != "private":
        return
    svc: TempleService = context.application.bot_data["svc"]

    # Scrub the password from chat history immediately.
    try:
        await update.message.delete()
    except TelegramError:
        pass

    if not _is_admin(update):
        await svc.db.log_event(
            "admin_auth_denied",
            update.effective_user.id if update.effective_user else None,
            "non-admin user id",
        )
        return

    if not context.args:
        await context.bot.send_message(
            update.effective_chat.id, "Usage: /admin <password>"
        )
        return

    supplied = hashlib.sha256(" ".join(context.args).encode("utf-8")).digest()
    if not hmac.compare_digest(supplied, ADMIN_PW_DIGEST):
        await svc.db.log_event(
            "admin_auth_failed", update.effective_user.id, "bad password"
        )
        await context.bot.send_message(update.effective_chat.id, "Authentication failed.")
        return

    svc.admin_sessions[update.effective_user.id] = time.monotonic() + ADMIN_SESSION_TTL
    await svc.db.log_event("admin_auth_ok", update.effective_user.id, "session opened")
    await context.bot.send_message(
        update.effective_chat.id, ADMIN_MENU, parse_mode=ParseMode.MARKDOWN_V2
    )


async def cmd_logout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update):
        return
    svc: TempleService = context.application.bot_data["svc"]
    svc.admin_sessions.pop(update.effective_user.id, None)
    await update.message.reply_text("Admin session closed.")


async def cmd_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_ok(update, context):
        return
    svc: TempleService = context.application.bot_data["svc"]

    counts = await svc.db.count_by_status()
    rows = await svc.db.list_users(limit=50)
    if not rows:
        await update.message.reply_text("No members on record.")
        return

    lines = [
        "Membership roster",
        "  ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "(empty)",
        "",
    ]
    for row in rows:
        handle = f"@{row['username']}" if row["username"] else "(no handle)"
        lines.append(
            f"{row['user_id']} {handle}\n"
            f"   status={row['status']}  expires={fmt_ts(row['expiration_date'])}\n"
            f"   subaddr_index={row['subaddress_index']}"
        )
    await update.message.reply_text("\n".join(lines)[:4000])


async def cmd_payments(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_ok(update, context):
        return
    svc: TempleService = context.application.bot_data["svc"]

    rows = await svc.db.list_payments(limit=25)
    if not rows:
        await update.message.reply_text("No payments recorded.")
        return

    lines = ["Payment ledger (most recent 25)", ""]
    for row in rows:
        lines.append(
            f"{fmt_ts(row['processed_at'])}  [{row['classification']}]\n"
            f"   user={row['user_id']}  amount={fmt_xmr(row['amount_atomic'])} XMR\n"
            f"   tx={row['txid'][:24]}…\n"
            f"   {row['detail'] or ''}"
        )
    await update.message.reply_text("\n".join(lines)[:4000])


async def cmd_audit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_ok(update, context):
        return
    svc: TempleService = context.application.bot_data["svc"]

    rows = await svc.db.list_audit(limit=30)
    if not rows:
        await update.message.reply_text("Audit log is empty.")
        return

    lines = ["Audit log (most recent 30)", ""]
    for row in rows:
        lines.append(
            f"{fmt_ts(row['ts'])}  {row['event']}  user={row['user_id']}\n"
            f"   {row['detail'] or ''}"
        )
    await update.message.reply_text("\n".join(lines)[:4000])


async def cmd_extend(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_ok(update, context):
        return
    svc: TempleService = context.application.bot_data["svc"]

    if len(context.args) != 2:
        await update.message.reply_text("Usage: /extend <user_id> <days>")
        return
    try:
        target = int(context.args[0])
        days = int(context.args[1])
    except ValueError:
        await update.message.reply_text("Both arguments must be integers.")
        return
    if days <= 0:
        await update.message.reply_text("Days must be positive.")
        return

    row = await svc.db.get_user(target)
    if row is None:
        await update.message.reply_text(f"No such user: {target}")
        return

    base = max(int(row["expiration_date"] or 0), now_ts())
    new_expiration = base + days * 86400
    await svc.db.set_membership(target, "active", new_expiration)
    await svc.db.log_event(
        "manual_extend", target, f"+{days}d by admin -> {fmt_ts(new_expiration)}"
    )

    invite = await svc.issue_invite(context.application, target)
    await update.message.reply_text(
        f"User {target} extended by {days} day(s).\n"
        f"New expiry: {fmt_ts(new_expiration)}\n"
        f"Invite issued: {'yes' if invite else 'no (see logs)'}"
    )


async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_ok(update, context):
        return
    svc: TempleService = context.application.bot_data["svc"]

    if len(context.args) != 1:
        await update.message.reply_text("Usage: /revoke <user_id>")
        return
    try:
        target = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id must be an integer.")
        return

    row = await svc.db.get_user(target)
    if row is None:
        await update.message.reply_text(f"No such user: {target}")
        return

    ok = await svc.expel(context.application, target, "manual revocation by admin")
    await svc.dm_user(
        context.application,
        target,
        "Your access to the inner channel has been withdrawn by the keeper.",
    )
    await update.message.reply_text(
        f"User {target} revoked. Channel removal: {'ok' if ok else 'see logs'}"
    )


async def cmd_sweep(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_ok(update, context):
        return
    await update.message.reply_text("Running expiry sweep…")
    await job_enforce_expiry(context)
    await update.message.reply_text("Sweep complete.")


# --------------------------------------------------------------------------- #
# Receipt rejection — payments are verified on-chain only
# --------------------------------------------------------------------------- #

RECEIPT_REJECTION = (
    "This bot does not accept receipts, screenshots, transaction hashes, or any "
    "user-submitted proof of payment. Verification happens exclusively against "
    "confirmed transactions observed on the Monero blockchain. Once your tribute "
    "reaches the required confirmations, your invitation is issued automatically."
)


async def reject_submissions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.effective_chat.type != "private":
        return
    if _is_admin(update):
        return
    await update.message.reply_text(RECEIPT_REJECTION)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled exception in handler", exc_info=context.error)


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


async def post_init(app: Application) -> None:
    db = Database(CFG.db_path)
    await db.connect()

    wallet = MoneroWallet(CFG)
    await wallet.verify(CFG.primary_address)

    oracle = PriceOracle()
    rate = await oracle.xmr_usd()
    log.info("Price oracle online: 1 XMR = $%s", rate)

    svc = TempleService(CFG, db, wallet, oracle)
    app.bot_data["svc"] = svc
    app.bot_data["db"] = db

    me = await app.bot.get_me()
    log.info("Authenticated as @%s (%s)", me.username, me.id)

    try:
        member = await app.bot.get_chat_member(CFG.channel_id, me.id)
        if member.status != "administrator":
            log.warning(
                "Bot is not an administrator of channel %s — invite links and "
                "expulsions will fail.",
                CFG.channel_id,
            )
    except TelegramError as exc:
        log.warning("Could not verify channel permissions: %s", exc)

    app.job_queue.run_repeating(
        job_poll_payments,
        interval=CFG.poll_interval,
        first=10,
        name="poll_payments",
    )
    app.job_queue.run_repeating(
        job_enforce_expiry,
        interval=86400,
        first=60,
        name="enforce_expiry",
    )
    log.info(
        "Jobs scheduled: payment poll every %ds, expiry sweep every 24h.",
        CFG.poll_interval,
    )


async def post_shutdown(app: Application) -> None:
    db: Optional[Database] = app.bot_data.get("db")
    if db is not None:
        await db.close()
    log.info("Shutdown complete.")


def main() -> None:
    app = (
        ApplicationBuilder()
        .token(CFG.bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))

    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("logout", cmd_logout))
    app.add_handler(CommandHandler("members", cmd_members))
    app.add_handler(CommandHandler("payments", cmd_payments))
    app.add_handler(CommandHandler("audit", cmd_audit))
    app.add_handler(CommandHandler("extend", cmd_extend))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("sweep", cmd_sweep))

    # Anything that is not a command in a private chat is treated as an attempted
    # receipt submission and refused.
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & ~filters.COMMAND, reject_submissions
        )
    )

    app.add_error_handler(on_error)

    log.info("Starting polling loop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
