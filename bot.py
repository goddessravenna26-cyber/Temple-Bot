#!/usr/bin/env python3
"""
bot.py — Monero-gated Telegram subscription bot.

Two interchangeable payment backends, selected by PAYMENT_PROVIDER:

  wallet_rpc   (default, RECOMMENDED)
      Non-custodial. View-only monero-wallet-rpc reachable over HTTP(S) at any
      URL — loopback, Railway private network, or a private VPS. Generates a
      unique subaddress per user_id and verifies payments against confirmed
      on-chain transfers via get_transfers. Nobody but you sees your payments.

  nowpayments
      Custodial-adjacent gateway over pure HTTPS. No Monero binary anywhere.
      Requires a KYC'd merchant account. NOWPayments creates the invoice,
      observes the payment, and forwards to your payout wallet. They retain a
      record of every transaction. See the TRADE-OFFS block below.

WHY THERE IS NO THIRD OPTION
----------------------------
There is no public Monero explorer API that can list incoming transactions for
an address. Monero's RPC protocol addresses blocks and transactions by hash or
height, never by address — address-indexed lookup is exactly what the protocol
exists to prevent. Any code claiming to poll an explorer for "payments to my
subaddress" cannot work.

Light wallet servers (MyMonero-style, NOWNodes, etc.) do scan on your behalf,
but require handing over your PRIVATE VIEW KEY. A view key cannot be rotated.
That grants a third party a permanent, irrevocable audit log of every payment
every member ever makes. This bot deliberately does not implement that path.

TRADE-OFFS OF nowpayments MODE — READ BEFORE ENABLING
-----------------------------------------------------
  * The gateway sees the fiat value, timing, and payout destination of every
    membership payment, and links them to your merchant identity.
  * Merchant onboarding is KYC'd. High-value recurring invoices from anonymous
    payers are a standard AML trigger; expect review, holds, or account closure.
  * Payments route through gateway-controlled addresses, not your subaddresses.
  * IMPORTANT: in this mode the welcome message's claim that "your anonymity is
    held sacred" is no longer true of the payment path. If you enable this
    backend, amend START_TEMPLATE accordingly — see the note above it.

DEPLOYMENT (wallet_rpc on Railway)
----------------------------------
Deploy monero-wallet-rpc as a SECOND service using deploy/Dockerfile.walletrpc,
then point the bot at it over the private network. No blockchain sync required —
wallet-rpc queries a remote daemon:

    MONERO_RPC_URL=http://walletrpc.railway.internal:18083/json_rpc

Environment variables
---------------------
  Common (always required):
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, ADMIN_USER_ID, ADMIN_PASSWORD
  Common (optional):
    PAYMENT_PROVIDER        wallet_rpc | nowpayments      (default wallet_rpc)
    SUBSCRIPTION_USD        default 5000
    SUBSCRIPTION_DAYS       default 30
    RENEWAL_WINDOW_DAYS     default 7
    AMOUNT_TOLERANCE_PCT    default 2.0
    QUOTE_TTL_HOURS         default 24
    INVITE_TTL_HOURS        default 24
    POLL_INTERVAL_SECONDS   default 60
    DB_PATH                 default ./membership.db

  PAYMENT_PROVIDER=wallet_rpc:
    MONERO_RPC_URL          e.g. http://walletrpc.railway.internal:18083/json_rpc
    MONERO_PRIMARY_ADDRESS  primary address of the view-only wallet
    MONERO_RPC_USER         digest auth user                      (optional)
    MONERO_RPC_PASSWORD     digest auth password                  (optional)
    MONERO_ACCOUNT_INDEX    default 0
    REQUIRED_CONFIRMATIONS  default 10

  PAYMENT_PROVIDER=nowpayments:
    NOWPAYMENTS_API_KEY     merchant API key
    NOWPAYMENTS_API_BASE    default https://api.nowpayments.io/v1
    NOWPAYMENTS_PAY_CURRENCY  default xmr
"""

from __future__ import annotations

import abc
import asyncio
import hashlib
import hmac
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Any, Iterable, Optional

import aiosqlite
import requests
from requests.auth import HTTPDigestAuth
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
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
PROVIDER_WALLET_RPC = "wallet_rpc"
PROVIDER_NOWPAYMENTS = "nowpayments"
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


def _first_env(*names: str) -> Optional[str]:
    """Return the first env var that is set. Tolerates naming drift between
    the bot service and the wallet service (e.g. MONERO_VIEW_KEY vs
    MONERO_PRIVATE_VIEW_KEY) instead of silently booting without a key."""
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def _require_first(*names: str) -> str:
    value = _first_env(*names)
    if not value:
        raise SystemExit(
            "FATAL: one of these environment variables must be set: "
            + ", ".join(names)
        )
    return value


def normalize_rpc_url(raw: str) -> str:
    """Validate and repair MONERO_RPC_URL.

    Misconfigured RPC URLs are the single most common cause of "the bot is up
    but nothing works", because a DNS failure and an auth failure and a wrong
    path all surface identically as a generic request exception. This fails
    loudly at boot with the specific problem instead.
    """
    from urllib.parse import urlparse, urlunparse

    candidate = raw.strip()
    if "://" not in candidate:
        candidate = "http://" + candidate

    parsed = urlparse(candidate)
    host = parsed.hostname or ""

    if not host:
        raise SystemExit(f"FATAL: MONERO_RPC_URL has no host: {raw!r}")

    # A bare private-network suffix with no service name can never resolve.
    if host in ("railway.internal", "internal", "localhost.railway.internal"):
        raise SystemExit(
            "FATAL: MONERO_RPC_URL is missing the service name.\n"
            f"  Configured: {raw}\n"
            "  Railway private DNS is <service-name>.railway.internal, so a bare\n"
            "  'railway.internal' resolves to nothing and every RPC call fails at\n"
            "  DNS. If your wallet service is named 'amiable-curiosity', use:\n"
            "    http://amiable-curiosity.railway.internal:18083/json_rpc"
        )

    port = parsed.port
    if port is None:
        port = 18083
        log.warning(
            "MONERO_RPC_URL had no port; assuming %d. Set it explicitly.", port
        )

    path = parsed.path or ""
    if not path.rstrip("/").endswith("/json_rpc"):
        log.warning(
            "MONERO_RPC_URL path was %r; appending /json_rpc. monero-wallet-rpc "
            "serves JSON-RPC at that path only.",
            path,
        )
        path = "/json_rpc"

    return urlunparse((parsed.scheme or "http", f"{host}:{port}", path, "", "", ""))


@dataclass(frozen=True)
class Config:
    bot_token: str
    channel_id: int
    admin_user_id: int
    admin_password: str
    provider: str
    subscription_usd: Decimal
    subscription_days: int
    renewal_window_days: int
    tolerance_pct: Decimal
    quote_ttl_hours: int
    invite_ttl_hours: int
    poll_interval: int
    db_path: str
    # wallet_rpc
    rpc_url: Optional[str] = None
    rpc_user: Optional[str] = None
    rpc_password: Optional[str] = None
    primary_address: Optional[str] = None
    account_index: int = 0
    required_confirmations: int = 10
    # wallet lifecycle (the bot opens/creates the wallet over RPC at boot)
    view_key: Optional[str] = None
    wallet_name: str = "viewonly"
    wallet_password: str = ""
    restore_height: int = 0
    rpc_boot_timeout: int = 180
    # nowpayments
    np_api_key: Optional[str] = None
    np_api_base: str = "https://api.nowpayments.io/v1"
    np_pay_currency: str = "xmr"

    @classmethod
    def from_env(cls) -> "Config":
        try:
            channel_id = int(_require("TELEGRAM_CHANNEL_ID"))
            admin_user_id = int(_require("ADMIN_USER_ID"))
        except ValueError:
            raise SystemExit(
                "FATAL: TELEGRAM_CHANNEL_ID and ADMIN_USER_ID must be integers."
            )

        provider = os.getenv("PAYMENT_PROVIDER", PROVIDER_WALLET_RPC).strip().lower()
        if provider not in (PROVIDER_WALLET_RPC, PROVIDER_NOWPAYMENTS):
            raise SystemExit(
                f"FATAL: PAYMENT_PROVIDER must be "
                f"{PROVIDER_WALLET_RPC!r} or {PROVIDER_NOWPAYMENTS!r}, got {provider!r}"
            )

        common = dict(
            bot_token=_require("TELEGRAM_BOT_TOKEN"),
            channel_id=channel_id,
            admin_user_id=admin_user_id,
            admin_password=_require("ADMIN_PASSWORD"),
            provider=provider,
            subscription_usd=_opt_dec("SUBSCRIPTION_USD", "5000"),
            subscription_days=_opt_int("SUBSCRIPTION_DAYS", 30),
            renewal_window_days=_opt_int("RENEWAL_WINDOW_DAYS", 7),
            tolerance_pct=_opt_dec("AMOUNT_TOLERANCE_PCT", "2.0"),
            quote_ttl_hours=_opt_int("QUOTE_TTL_HOURS", 24),
            invite_ttl_hours=_opt_int("INVITE_TTL_HOURS", 24),
            poll_interval=_opt_int("POLL_INTERVAL_SECONDS", 60),
            db_path=os.getenv("DB_PATH", "membership.db"),
        )

        if provider == PROVIDER_WALLET_RPC:
            return cls(
                **common,
                rpc_url=normalize_rpc_url(_require("MONERO_RPC_URL")),
                rpc_user=_first_env("MONERO_RPC_USER", "MONERO_RPC_USERNAME"),
                rpc_password=_first_env("MONERO_RPC_PASSWORD", "MONERO_RPC_PASS"),
                primary_address=_require("MONERO_PRIMARY_ADDRESS"),
                account_index=_opt_int("MONERO_ACCOUNT_INDEX", 0),
                required_confirmations=_opt_int("REQUIRED_CONFIRMATIONS", 10),
                # Optional: only needed if the wallet must be CREATED. A
                # container running --wallet-file already has its wallet and
                # never needs this. Enforced at creation time, not boot time.
                view_key=_first_env("MONERO_VIEW_KEY", "MONERO_PRIVATE_VIEW_KEY"),
                wallet_name=os.getenv("MONERO_WALLET_NAME", "viewonly"),
                wallet_password=os.getenv("MONERO_WALLET_PASSWORD", ""),
                restore_height=_opt_int("MONERO_RESTORE_HEIGHT", 0),
                rpc_boot_timeout=_opt_int("MONERO_RPC_BOOT_TIMEOUT", 180),
            )

        return cls(
            **common,
            np_api_key=_require("NOWPAYMENTS_API_KEY"),
            np_api_base=os.getenv(
                "NOWPAYMENTS_API_BASE", "https://api.nowpayments.io/v1"
            ).rstrip("/"),
            np_pay_currency=os.getenv("NOWPAYMENTS_PAY_CURRENCY", "xmr").lower(),
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


def xmr_to_atomic(value: Any) -> int:
    """Convert an XMR quantity (str/float/Decimal) to integer piconero."""
    return int(
        (Decimal(str(value)) * ATOMIC_UNITS).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )


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
# Price oracle
# --------------------------------------------------------------------------- #


class PriceError(RuntimeError):
    pass


class PriceOracle:
    """CoinGecko spot price with short-lived cache and bounded retries.

    Deliberately does NOT serve a stale price beyond the cache window — a wrong
    quote on a high-value invoice is worse than a failed quote.
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
                value = Decimal(str(resp.json()["monero"]["usd"]))
                if value <= 0:
                    raise PriceError(f"implausible XMR price: {value}")
                return value
            except Exception as exc:  # noqa: BLE001 - retried, re-raised below
                last_exc = exc
                time.sleep(1.5 * (attempt + 1))
        raise PriceError(f"could not obtain XMR/USD price: {last_exc}")

    async def xmr_usd(self) -> Decimal:
        async with self._lock:
            if self._cached is not None and now_ts() - self._cached_at < self.CACHE_TTL:
                return self._cached
            self._cached = await asyncio.to_thread(self._fetch_sync)
            self._cached_at = now_ts()
            return self._cached

    async def usd_to_atomic(self, usd: Decimal) -> tuple[int, Decimal]:
        rate = await self.xmr_usd()
        xmr = (usd / rate).quantize(Decimal("0.000000000001"), rounding=ROUND_HALF_UP)
        return int(xmr * ATOMIC_UNITS), rate


# --------------------------------------------------------------------------- #
# Payment provider interface
# --------------------------------------------------------------------------- #


class PaymentError(RuntimeError):
    pass


@dataclass
class Quote:
    """What the user must send, and where."""

    pay_address: str
    required_atomic: int
    xmr_usd_rate: Decimal
    provider_ref: Optional[str] = None  # gateway invoice id, if any
    expires_at: Optional[int] = None


@dataclass
class ObservedPayment:
    """A confirmed inbound payment, normalised across providers."""

    unique_id: str  # txid (rpc) or payment_id (gateway) — idempotency key
    user_id: Optional[int]
    amount_atomic: int
    confirmations: int
    block_height: Optional[int] = None
    subaddr_index: Optional[int] = None
    meta: dict = field(default_factory=dict)


class PaymentProvider(abc.ABC):
    """Abstract backend. Renewal logic never touches provider internals."""

    name: str = "abstract"

    @abc.abstractmethod
    async def verify(self) -> None:
        """Startup health check. Raise PaymentError to abort boot."""

    @abc.abstractmethod
    async def prepare_quote(self, db: "Database", user_id: int, usd: Decimal) -> Quote:
        """Produce a payable target and required amount for this user."""

    @abc.abstractmethod
    async def poll(self, db: "Database") -> list[ObservedPayment]:
        """Return confirmed inbound payments. Must be safe to call repeatedly."""

    async def diagnostics(self) -> str:
        """Human-readable connectivity report. Overridden per provider."""
        return f"provider={self.name} (no diagnostics implemented)"


# --------------------------------------------------------------------------- #
# Provider 1: view-only monero-wallet-rpc  (non-custodial, recommended)
# --------------------------------------------------------------------------- #


class WalletRPCProvider(PaymentProvider):
    """JSON-RPC client for monero-wallet-rpc.

    Transport-agnostic: MONERO_RPC_URL may be loopback, a Railway private
    hostname, or HTTPS through a tunnel. Uses `requests` per spec, but every
    call is dispatched via asyncio.to_thread so the event loop never blocks.
    """

    name = PROVIDER_WALLET_RPC

    def __init__(self, cfg: Config, oracle: PriceOracle) -> None:
        if not cfg.rpc_url or not cfg.primary_address:
            raise PaymentError("wallet_rpc provider requires URL and primary address")
        self._cfg = cfg
        self._oracle = oracle
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
            raise PaymentError(f"transport failure calling {method}: {exc}") from exc
        except ValueError as exc:
            raise PaymentError(f"non-JSON response from {method}") from exc

        if body.get("error"):
            err = body["error"]
            raise PaymentError(
                f"{method} failed: [{err.get('code')}] {err.get('message')}"
            )
        return body.get("result", {}) or {}

    async def call(self, method: str, params: Optional[dict] = None) -> dict:
        async with self._lock:
            return await asyncio.to_thread(self._call_sync, method, params)

    # -- boot: wait for the sidecar, then open or create the wallet --------- #

    async def _wait_for_rpc(self, timeout: int) -> None:
        """Block until the wallet RPC answers at all.

        On Railway the two services boot concurrently, so the bot routinely wins
        the race and would otherwise crash-loop against a sibling that is 20
        seconds from ready. `get_version` needs no wallet loaded, which makes it
        the right liveness probe: it separates "RPC is down" from "RPC is up but
        has no wallet".
        """
        deadline = time.monotonic() + timeout
        attempt = 0
        last: Optional[Exception] = None

        while time.monotonic() < deadline:
            attempt += 1
            try:
                version = await self.call("get_version")
                log.info(
                    "wallet-rpc reachable at %s (version %s) after %d attempt(s).",
                    self._url,
                    version.get("version", "?"),
                    attempt,
                )
                return
            except PaymentError as exc:
                last = exc
                # An RPC-level error means the daemon is up and talking.
                if "failed:" in str(exc):
                    log.info("wallet-rpc is up (responded with an RPC error).")
                    return
                if attempt == 1 or attempt % 5 == 0:
                    log.info(
                        "Waiting for wallet-rpc at %s … (%s)",
                        self._url,
                        str(exc)[:160],
                    )
                await asyncio.sleep(3)

        raise PaymentError(
            f"wallet-rpc at {self._url} did not respond within {timeout}s.\n"
            f"  Last error: {last}\n"
            "  Check: is the wallet service running, is the service name in\n"
            "  MONERO_RPC_URL correct, and is it listening on that port?"
        )

    @staticmethod
    def _is_missing_wallet_error(exc: PaymentError) -> bool:
        text = str(exc).lower()
        return any(
            marker in text
            for marker in (
                "no wallet file",
                "-13",
                "not open",
                "no wallet",
                "wallet is not opened",
            )
        )

    async def _open_wallet(self) -> None:
        await self.call(
            "open_wallet",
            {
                "filename": self._cfg.wallet_name,
                "password": self._cfg.wallet_password,
            },
        )
        log.info("Opened existing wallet %r.", self._cfg.wallet_name)

    async def _create_view_only_wallet(self) -> None:
        """Restore a view-only wallet from primary address + private view key.

        Omitting `spendkey` is what makes this view-only: the resulting wallet
        can derive subaddresses and observe incoming transfers, but cannot
        construct a spend. A compromise of this container costs you visibility,
        not funds.
        """
        if not self._cfg.view_key:
            raise PaymentError(
                "A wallet must be created, but no private view key is configured.\n"
                "  Set MONERO_VIEW_KEY (or MONERO_PRIVATE_VIEW_KEY) on the BOT\n"
                "  service. In --wallet-dir mode the bot creates the wallet, so\n"
                "  the key belongs on the bot, not the wallet container."
            )
        log.info(
            "No wallet found; restoring view-only wallet %r from keys "
            "(restore_height=%d).",
            self._cfg.wallet_name,
            self._cfg.restore_height,
        )
        await self.call(
            "generate_from_keys",
            {
                "filename": self._cfg.wallet_name,
                "address": self._cfg.primary_address,
                "viewkey": self._cfg.view_key,
                "password": self._cfg.wallet_password,
                "restore_height": self._cfg.restore_height,
                "autosave_current": True,
            },
        )
        log.info("View-only wallet created.")

    async def ensure_wallet_ready(self) -> None:
        """Idempotent: guarantees a wallet is loaded before any polling starts."""
        try:
            await self.call("get_address", {"account_index": self._account})
            log.info("A wallet is already open.")
            return
        except PaymentError as exc:
            if not self._is_missing_wallet_error(exc):
                raise
            log.info("No wallet loaded in RPC — opening or creating one.")

        try:
            await self._open_wallet()
        except PaymentError as open_exc:
            log.info("open_wallet did not succeed (%s); creating.", str(open_exc)[:120])
            try:
                await self._create_view_only_wallet()
            except PaymentError as create_exc:
                raise PaymentError(
                    "Could not open or create the view-only wallet.\n"
                    f"  open_wallet:        {open_exc}\n"
                    f"  generate_from_keys: {create_exc}\n"
                    "  Most likely causes: wallet-rpc was started with --wallet-file\n"
                    "  instead of --wallet-dir (which forbids these calls), the\n"
                    "  wallet directory is not writable, or the view key does not\n"
                    "  correspond to the primary address."
                ) from create_exc

        # Kick off a background refresh so the first poll is not stale.
        try:
            await self.call("refresh", {"start_height": self._cfg.restore_height})
        except PaymentError as exc:
            log.warning("Initial refresh returned: %s", exc)

    async def verify(self) -> None:
        await self._wait_for_rpc(self._cfg.rpc_boot_timeout)
        await self.ensure_wallet_ready()

        result = await self.call("get_address", {"account_index": self._account})
        actual = result.get("address", "")
        if actual != self._cfg.primary_address:
            raise PaymentError(
                "The wallet that wallet-rpc loaded is NOT the wallet you configured.\n"
                f"  RPC reports:   {actual[:16]}…\n"
                f"  Env expects:   {(self._cfg.primary_address or '')[:16]}…\n"
                "  Payments would be watched on the wrong wallet, so boot is aborted.\n"
                "  If you changed MONERO_PRIMARY_ADDRESS, delete the old wallet file\n"
                "  from the volume (or set MONERO_WALLET_NAME to a new name)."
            )
        log.info(
            "monero-wallet-rpc verified at %s (account %d, wallet %r).",
            self._url,
            self._account,
            self._cfg.wallet_name,
        )

    async def diagnostics(self) -> str:
        """Human-readable connectivity report for the /diag admin command."""
        lines = [f"RPC URL: {self._url}"]
        auth = "digest" if self._cfg.rpc_user else "NONE"
        lines.append(f"RPC auth: {auth}")
        try:
            version = await self.call("get_version")
            lines.append(f"get_version: ok ({version.get('version', '?')})")
        except PaymentError as exc:
            lines.append(f"get_version: FAILED — {str(exc)[:220]}")
            return "\n".join(lines)
        try:
            addr = await self.call("get_address", {"account_index": self._account})
            lines.append(f"wallet open: yes ({addr.get('address', '')[:16]}…)")
            lines.append(f"subaddresses: {len(addr.get('addresses', []))}")
        except PaymentError as exc:
            lines.append(f"wallet open: NO — {str(exc)[:220]}")
            return "\n".join(lines)
        try:
            height = await self.call("get_height")
            lines.append(f"wallet height: {height.get('height', '?')}")
        except PaymentError as exc:
            lines.append(f"get_height: FAILED — {str(exc)[:160]}")
        return "\n".join(lines)

    async def _create_subaddress(self, label: str) -> tuple[str, int]:
        result = await self.call(
            "create_address", {"account_index": self._account, "label": label}
        )
        address, index = result.get("address"), result.get("address_index")
        if not address or index is None:
            raise PaymentError("create_address returned an incomplete result")
        return address, int(index)

    async def ensure_subaddress(self, db: "Database", user_id: int) -> tuple[str, int]:
        row = await db.get_user(user_id)
        if row is not None and row["subaddress"]:
            return row["subaddress"], int(row["subaddress_index"])
        address, index = await self._create_subaddress(f"tg:{user_id}")
        await db.set_subaddress(user_id, address, index)
        return address, index

    async def prepare_quote(self, db: "Database", user_id: int, usd: Decimal) -> Quote:
        address, _index = await self.ensure_subaddress(db, user_id)
        atomic, rate = await self._oracle.usd_to_atomic(usd)
        return Quote(pay_address=address, required_atomic=atomic, xmr_usd_rate=rate)

    async def poll(self, db: "Database") -> list[ObservedPayment]:
        indices = sorted({i for i in await db.all_subaddress_indices()})
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

        observed: list[ObservedPayment] = []
        for transfer in result.get("in", []) or []:
            txid = transfer.get("txid")
            if not txid:
                continue
            confirmations = int(transfer.get("confirmations") or 0)
            if confirmations < self._cfg.required_confirmations:
                continue
            if transfer.get("double_spend_seen"):
                log.warning("Ignoring tx %s: double spend seen.", txid)
                continue
            minor = (transfer.get("subaddr_index") or {}).get("minor")
            if minor is None:
                continue
            row = await db.get_user_by_subaddr_index(int(minor))
            observed.append(
                ObservedPayment(
                    unique_id=txid,
                    user_id=int(row["user_id"]) if row is not None else None,
                    amount_atomic=int(transfer.get("amount") or 0),
                    confirmations=confirmations,
                    block_height=transfer.get("height"),
                    subaddr_index=int(minor),
                )
            )
        return observed


# --------------------------------------------------------------------------- #
# Provider 2: NOWPayments gateway  (pure HTTPS, no binary — see TRADE-OFFS)
# --------------------------------------------------------------------------- #


class NowPaymentsProvider(PaymentProvider):
    """NOWPayments merchant API.

    Endpoints used:
        GET  /status                 health check
        POST /payment               create an invoice
        GET  /payment/{payment_id}  poll invoice status

    Statuses: waiting, confirming, confirmed, sending, partially_paid,
              finished, failed, refunded, expired.
    Only `finished` is treated as settled — it is the only state that
    guarantees funds reached the payout wallet.
    """

    name = PROVIDER_NOWPAYMENTS
    TERMINAL_PAID = {"finished"}
    TERMINAL_DEAD = {"failed", "refunded", "expired"}

    def __init__(self, cfg: Config, oracle: PriceOracle) -> None:
        if not cfg.np_api_key:
            raise PaymentError("nowpayments provider requires NOWPAYMENTS_API_KEY")
        self._cfg = cfg
        self._oracle = oracle
        self._base = cfg.np_api_base
        self._session = requests.Session()
        self._session.headers.update(
            {
                "x-api-key": cfg.np_api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )
        self._lock = asyncio.Lock()

    def _request_sync(self, method: str, path: str, **kwargs: Any) -> dict:
        url = f"{self._base}{path}"
        try:
            resp = self._session.request(method, url, timeout=30, **kwargs)
        except requests.RequestException as exc:
            raise PaymentError(f"transport failure on {method} {path}: {exc}") from exc

        if resp.status_code >= 400:
            raise PaymentError(
                f"{method} {path} returned HTTP {resp.status_code}: {resp.text[:300]}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise PaymentError(f"non-JSON response from {method} {path}") from exc

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        async with self._lock:
            return await asyncio.to_thread(self._request_sync, method, path, **kwargs)

    async def verify(self) -> None:
        body = await self._request("GET", "/status")
        if str(body.get("message", "")).lower() != "ok":
            raise PaymentError(f"NOWPayments status endpoint unhealthy: {body}")
        log.warning(
            "NOWPayments backend active. This gateway retains a record of every "
            "membership payment and is subject to AML review. The anonymity claim "
            "in START_TEMPLATE does not hold for this payment path."
        )

    async def prepare_quote(self, db: "Database", user_id: int, usd: Decimal) -> Quote:
        payload = {
            "price_amount": float(usd),
            "price_currency": "usd",
            "pay_currency": self._cfg.np_pay_currency,
            "order_id": f"tg-{user_id}-{now_ts()}",
            "order_description": f"{self._cfg.subscription_days}-day membership",
        }
        body = await self._request("POST", "/payment", json=payload)

        payment_id = body.get("payment_id")
        pay_address = body.get("pay_address")
        pay_amount = body.get("pay_amount")
        if not payment_id or not pay_address or pay_amount is None:
            raise PaymentError(f"incomplete create-payment response: {body}")

        required_atomic = xmr_to_atomic(pay_amount)
        if required_atomic <= 0:
            raise PaymentError(f"gateway quoted a non-positive amount: {pay_amount}")

        # Derive the effective rate from the gateway's own quote so the ledger
        # records what the user was actually charged, not a second CoinGecko read.
        rate = (usd / Decimal(str(pay_amount))).quantize(Decimal("0.01"))
        expires_at = now_ts() + self._cfg.quote_ttl_hours * 3600

        await db.upsert_invoice(
            payment_id=str(payment_id),
            user_id=user_id,
            required_atomic=required_atomic,
            pay_address=str(pay_address),
            status=str(body.get("payment_status") or "waiting"),
            expires_at=expires_at,
        )
        return Quote(
            pay_address=str(pay_address),
            required_atomic=required_atomic,
            xmr_usd_rate=rate,
            provider_ref=str(payment_id),
            expires_at=expires_at,
        )

    async def diagnostics(self) -> str:
        lines = [f"Gateway: {self._base}", f"Pay currency: {self._cfg.np_pay_currency}"]
        try:
            body = await self._request("GET", "/status")
            lines.append(f"status: {body.get('message', '?')}")
        except PaymentError as exc:
            lines.append(f"status: FAILED — {str(exc)[:220]}")
        return "\n".join(lines)

    async def poll(self, db: "Database") -> list[ObservedPayment]:
        observed: list[ObservedPayment] = []
        for invoice in await db.open_invoices():
            payment_id = invoice["payment_id"]
            try:
                body = await self._request("GET", f"/payment/{payment_id}")
            except PaymentError as exc:
                log.error("Invoice %s poll failed: %s", payment_id, exc)
                continue

            status = str(body.get("payment_status") or "").lower()
            if status != invoice["status"]:
                await db.set_invoice_status(payment_id, status)

            if status in self.TERMINAL_DEAD:
                await db.close_invoice(payment_id)
                continue
            if status not in self.TERMINAL_PAID:
                continue

            # `actually_paid` reflects what arrived; fall back to the quote.
            paid = body.get("actually_paid") or body.get("pay_amount") or 0
            await db.close_invoice(payment_id)
            observed.append(
                ObservedPayment(
                    unique_id=f"np:{payment_id}",
                    user_id=int(invoice["user_id"]),
                    amount_atomic=xmr_to_atomic(paid),
                    confirmations=self._cfg.required_confirmations,
                    meta={"provider": self.name, "payment_id": payment_id},
                )
            )
        return observed


def build_provider(cfg: Config, oracle: PriceOracle) -> PaymentProvider:
    if cfg.provider == PROVIDER_WALLET_RPC:
        return WalletRPCProvider(cfg, oracle)
    return NowPaymentsProvider(cfg, oracle)


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
#
# Schema note: the original `users`, `quotes`, `payments` and `audit_log` tables
# are preserved. Two changes were unavoidable to support a gateway backend:
#   * users.subaddress / users.subaddress_index are now NULLable, because in
#     gateway mode there is no persistent per-user subaddress. Uniqueness is
#     still enforced, but SQLite permits multiple NULLs in a UNIQUE column.
#   * a new `invoices` table tracks open gateway invoices.
# _migrate() below rebuilds a pre-existing `users` table in place if it still
# carries the old NOT NULL constraints, so upgrades do not lose data.

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    user_id           INTEGER PRIMARY KEY,
    username          TEXT,
    subaddress        TEXT UNIQUE,
    subaddress_index  INTEGER UNIQUE,
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
    provider_ref    TEXT,
    created_at      INTEGER NOT NULL,
    expires_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quotes_user ON quotes(user_id, expires_at);

CREATE TABLE IF NOT EXISTS invoices (
    payment_id      TEXT PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    required_atomic INTEGER NOT NULL,
    pay_address     TEXT NOT NULL,
    status          TEXT NOT NULL,
    open            INTEGER NOT NULL DEFAULT 1,
    created_at      INTEGER NOT NULL,
    expires_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_invoices_open ON invoices(open, expires_at);

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

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        await self._migrate()
        log.info("SQLite ready at %s", self._path)

    async def _migrate(self) -> None:
        """Additive, idempotent migrations for databases created by v1."""
        async with self.conn.execute("PRAGMA table_info(quotes)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        if "provider_ref" not in cols:
            await self.conn.execute("ALTER TABLE quotes ADD COLUMN provider_ref TEXT")
            await self.conn.commit()
            log.info("Migration: added quotes.provider_ref")

        async with self.conn.execute("PRAGMA table_info(users)") as cur:
            info = await cur.fetchall()
        needs_relax = any(
            row[1] in ("subaddress", "subaddress_index") and row[3] == 1 for row in info
        )
        if not needs_relax:
            return

        log.info("Migration: relaxing NOT NULL on users.subaddress[_index]")
        await self.conn.executescript(
            """
            PRAGMA foreign_keys=OFF;
            BEGIN;
            CREATE TABLE users_new (
                user_id           INTEGER PRIMARY KEY,
                username          TEXT,
                subaddress        TEXT UNIQUE,
                subaddress_index  INTEGER UNIQUE,
                status            TEXT NOT NULL DEFAULT 'pending',
                expiration_date   INTEGER,
                created_at        INTEGER NOT NULL,
                updated_at        INTEGER NOT NULL
            );
            INSERT INTO users_new SELECT user_id, username, subaddress,
                subaddress_index, status, expiration_date, created_at, updated_at
                FROM users;
            DROP TABLE users;
            ALTER TABLE users_new RENAME TO users;
            COMMIT;
            PRAGMA foreign_keys=ON;
            """
        )
        await self.conn.commit()
        log.info("Migration complete.")

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

    async def ensure_user(self, user_id: int, username: Optional[str]) -> aiosqlite.Row:
        ts = now_ts()
        await self.conn.execute(
            "INSERT INTO users (user_id, username, status, created_at, updated_at) "
            "VALUES (?,?,'pending',?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, "
            "updated_at=excluded.updated_at",
            (user_id, username, ts, ts),
        )
        await self.conn.commit()
        row = await self.get_user(user_id)
        assert row is not None
        return row

    async def set_subaddress(self, user_id: int, subaddress: str, index: int) -> None:
        await self.conn.execute(
            "UPDATE users SET subaddress = ?, subaddress_index = ?, updated_at = ? "
            "WHERE user_id = ?",
            (subaddress, index, now_ts(), user_id),
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
        async with self.conn.execute(
            "SELECT subaddress_index FROM users WHERE subaddress_index IS NOT NULL"
        ) as cur:
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
        self, user_id: int, usd: Decimal, quote: Quote, ttl_hours: int
    ) -> None:
        ts = now_ts()
        await self.conn.execute(
            "INSERT INTO quotes (user_id, usd_amount, xmr_usd_rate, required_atomic, "
            "provider_ref, created_at, expires_at) VALUES (?,?,?,?,?,?,?)",
            (
                user_id,
                str(usd),
                str(quote.xmr_usd_rate),
                quote.required_atomic,
                quote.provider_ref,
                ts,
                quote.expires_at or (ts + ttl_hours * 3600),
            ),
        )
        await self.conn.commit()

    async def active_quotes(self, user_id: int) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            "SELECT * FROM quotes WHERE user_id = ? AND expires_at >= ? "
            "ORDER BY created_at DESC",
            (user_id, now_ts()),
        ) as cur:
            return list(await cur.fetchall())

    # -- invoices (gateway mode) ------------------------------------------- #

    async def upsert_invoice(
        self,
        payment_id: str,
        user_id: int,
        required_atomic: int,
        pay_address: str,
        status: str,
        expires_at: int,
    ) -> None:
        await self.conn.execute(
            "INSERT INTO invoices (payment_id, user_id, required_atomic, pay_address, "
            "status, open, created_at, expires_at) VALUES (?,?,?,?,?,1,?,?) "
            "ON CONFLICT(payment_id) DO UPDATE SET status=excluded.status",
            (
                payment_id,
                user_id,
                required_atomic,
                pay_address,
                status,
                now_ts(),
                expires_at,
            ),
        )
        await self.conn.commit()

    async def open_invoices(self) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            "SELECT * FROM invoices WHERE open = 1 ORDER BY created_at ASC"
        ) as cur:
            return list(await cur.fetchall())

    async def set_invoice_status(self, payment_id: str, status: str) -> None:
        await self.conn.execute(
            "UPDATE invoices SET status = ? WHERE payment_id = ?", (status, payment_id)
        )
        await self.conn.commit()

    async def close_invoice(self, payment_id: str) -> None:
        await self.conn.execute(
            "UPDATE invoices SET open = 0 WHERE payment_id = ?", (payment_id,)
        )
        await self.conn.commit()

    # -- payments ---------------------------------------------------------- #

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
        """Insert a payment. Returns False if already recorded (idempotency)."""
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

    async def payment_seen(self, txid: str) -> bool:
        async with self.conn.execute(
            "SELECT 1 FROM payments WHERE txid = ?", (txid,)
        ) as cur:
            return await cur.fetchone() is not None

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
#
# NOTE: This text is reproduced verbatim from the specification. If you switch
# PAYMENT_PROVIDER to `nowpayments`, the anonymity claim below becomes false —
# the gateway records every payment and links it to your merchant identity.
# Amend it before taking money under that backend.

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

RECEIPT_REJECTION = (
    "This bot does not accept receipts, screenshots, transaction hashes, or any "
    "user-submitted proof of payment. Verification happens exclusively against "
    "confirmed transactions. Once your tribute settles, your invitation is issued "
    "automatically."
)


# --------------------------------------------------------------------------- #
# Core service — provider-agnostic membership logic
# --------------------------------------------------------------------------- #


class TempleService:
    def __init__(
        self, cfg: Config, db: Database, provider: PaymentProvider, oracle: PriceOracle
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.provider = provider
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

    async def issue_quote(self, user_id: int, username: Optional[str]) -> Quote:
        await self.db.ensure_user(user_id, username)
        quote = await self.provider.prepare_quote(
            self.db, user_id, self.cfg.subscription_usd
        )
        await self.db.insert_quote(
            user_id, self.cfg.subscription_usd, quote, self.cfg.quote_ttl_hours
        )
        return quote

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
            "Your tribute has been confirmed.\n\n"
            f"Your single-use invitation:\n{link.invite_link}\n\n"
            "This link admits exactly one account and expires at "
            f"{expire_at.strftime('%Y-%m-%d %H:%M UTC')}.",
        )
        await self.db.log_event(
            "invite_issued",
            user_id,
            f"delivered={delivered} expires={fmt_ts(int(expire_at.timestamp()))}",
        )
        return link.invite_link

    # -- payment classification (unchanged from v1) ------------------------ #

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

    async def process_payment(self, app: Application, obs: ObservedPayment) -> None:
        if await self.db.payment_seen(obs.unique_id):
            return

        # --- payment we cannot attribute to a user -------------------------- #
        if obs.user_id is None:
            await self.db.record_payment(
                obs.unique_id, None, obs.subaddr_index, obs.amount_atomic,
                obs.confirmations, obs.block_height, "bonus_tribute",
                "unattributable payment",
            )
            await self.notify_admin(
                app,
                f"*Bonus Tribute \\(unattributed\\)*\n"
                f"Amount: `{escape_md(fmt_xmr(obs.amount_atomic))}` XMR\n"
                f"Ref: `{escape_md(obs.unique_id)}`",
            )
            return

        user = await self.db.get_user(obs.user_id)
        if user is None:
            return
        user_id = int(user["user_id"])
        handle = escape_md(user["username"] or "n/a")
        expiration = user["expiration_date"]
        quote = await self._matching_quote(user_id, obs.amount_atomic)

        # --- wrong amount -> bonus tribute --------------------------------- #
        if quote is None:
            if not await self.db.record_payment(
                obs.unique_id, user_id, obs.subaddr_index, obs.amount_atomic,
                obs.confirmations, obs.block_height, "bonus_tribute",
                "amount did not match any active quote",
            ):
                return
            await self.db.log_event(
                "bonus_tribute", user_id,
                f"amount={fmt_xmr(obs.amount_atomic)} ref={obs.unique_id}",
            )
            await self.notify_admin(
                app,
                f"*Bonus Tribute*\n"
                f"User: `{user_id}` \\(@{handle}\\)\n"
                f"Amount: `{escape_md(fmt_xmr(obs.amount_atomic))}` XMR\n"
                f"Reason: amount outside quoted tolerance\n"
                f"Membership: *not extended*\n"
                f"Ref: `{escape_md(obs.unique_id)}`",
            )
            await self.dm_user(
                app, user_id,
                "A payment has been confirmed, but the amount does not match your "
                "outstanding tribute. It has been logged as a bonus tribute and your "
                "membership was not altered. Contact the keeper if this is unexpected.",
            )
            return

        threshold = now_ts() + self.cfg.renewal_window_days * 86400

        # --- correct amount, but membership not near expiry ----------------- #
        if expiration is not None and int(expiration) > threshold:
            if not await self.db.record_payment(
                obs.unique_id, user_id, obs.subaddr_index, obs.amount_atomic,
                obs.confirmations, obs.block_height, "bonus_tribute",
                f"membership valid beyond {self.cfg.renewal_window_days}d window",
            ):
                return
            await self.db.log_event(
                "bonus_tribute", user_id, f"early payment ref={obs.unique_id}"
            )
            await self.notify_admin(
                app,
                f"*Bonus Tribute*\n"
                f"User: `{user_id}` \\(@{handle}\\)\n"
                f"Amount: `{escape_md(fmt_xmr(obs.amount_atomic))}` XMR\n"
                f"Reason: expiry is {escape_md(fmt_ts(int(expiration)))}, outside the "
                f"{self.cfg.renewal_window_days}\\-day renewal window\n"
                f"Membership: *not extended*\n"
                f"Ref: `{escape_md(obs.unique_id)}`",
            )
            await self.dm_user(
                app, user_id,
                "Your tribute has been confirmed, but your membership does not yet "
                f"fall within the {self.cfg.renewal_window_days}-day renewal window "
                f"(current expiry: {fmt_ts(int(expiration))}). It has been recorded as "
                "a bonus tribute and your membership was not extended.",
            )
            return

        # --- activation or renewal ------------------------------------------ #
        is_renewal = expiration is not None
        base = max(int(expiration), now_ts()) if is_renewal else now_ts()
        new_expiration = base + self.cfg.subscription_days * 86400
        classification = "renewal" if is_renewal else "activation"

        if not await self.db.record_payment(
            obs.unique_id, user_id, obs.subaddr_index, obs.amount_atomic,
            obs.confirmations, obs.block_height, classification,
            f"new_expiration={new_expiration}",
        ):
            return

        await self.db.set_membership(user_id, "active", new_expiration)
        await self.db.log_event(
            classification, user_id,
            f"expires={fmt_ts(new_expiration)} ref={obs.unique_id}",
        )

        title = "Membership Renewed" if is_renewal else "New Membership"
        await self.notify_admin(
            app,
            f"*{escape_md(title)}*\n"
            f"User: `{user_id}` \\(@{handle}\\)\n"
            f"Amount: `{escape_md(fmt_xmr(obs.amount_atomic))}` XMR\n"
            f"Expires: {escape_md(fmt_ts(new_expiration))}\n"
            f"Ref: `{escape_md(obs.unique_id)}`",
        )
        await self.issue_invite(app, user_id)

    # -- expulsion --------------------------------------------------------- #

    async def expel(self, app: Application, user_id: int, reason: str) -> bool:
        """Remove a user from the channel without a permanent ban."""
        ok = True
        try:
            await app.bot.ban_chat_member(
                chat_id=self.cfg.channel_id, user_id=user_id, revoke_messages=False
            )
            await asyncio.sleep(1)
            await app.bot.unban_chat_member(
                chat_id=self.cfg.channel_id, user_id=user_id, only_if_banned=True
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
        observed = await svc.provider.poll(svc.db)
    except PaymentError as exc:
        log.error("Payment poll failed: %s", exc)
        return

    for payment in observed:
        try:
            await svc.process_payment(context.application, payment)
        except Exception:  # noqa: BLE001 - one bad payment must not kill the loop
            log.exception("Error processing payment %s", payment.unique_id)


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
            context.application, user_id,
            "Your 30-day membership has lapsed and your access to the inner channel "
            "has been withdrawn. Send /start to receive a fresh tribute address and "
            "restore your standing.",
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


GENERATE_CALLBACK = "generate_subaddress"

GATE_PROMPT = (
    "The gates of the Temple are before you.\n\n"
    "Tap below to generate your unique payment address and current tribute quote."
)


def _gate_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Generate Sacred Subaddress", callback_data=GENERATE_CALLBACK)]]
    )


async def _deliver_quote(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    username: Optional[str],
) -> None:
    """Single code path for /start and the inline button.

    Failures are logged with their real cause and reported to the admin, because
    a generic 'try again later' shown to the user with nothing in the log is how
    an outage stays invisible for days.
    """
    svc: TempleService = context.application.bot_data["svc"]

    try:
        quote = await svc.issue_quote(user_id, username)
    except PaymentError as exc:
        log.error("Quote generation FAILED for %s: %s", user_id, exc)
        await svc.db.log_event("quote_failed", user_id, str(exc)[:500])
        await context.bot.send_message(
            chat_id,
            "The Temple's ledger is momentarily unreachable. Please try again in a "
            "few minutes.",
        )
        await svc.notify_admin(
            context.application,
            "*Subaddress generation FAILED*\n"
            f"User: `{user_id}`\n"
            f"Error: `{escape_md(str(exc)[:400])}`\n"
            "Run `/diag` for a connectivity report\\.",
        )
        return
    except PriceError as exc:
        log.error("Price lookup FAILED for %s: %s", user_id, exc)
        await svc.db.log_event("quote_failed", user_id, f"price: {exc}"[:500])
        await context.bot.send_message(
            chat_id,
            "The tribute rate could not be established just now. Please try again "
            "shortly.",
        )
        return

    await context.bot.send_message(
        chat_id,
        START_TEMPLATE.format(
            subaddress=quote.pay_address,
            xmr_amount=fmt_xmr(quote.required_atomic),
        ),
        disable_web_page_preview=True,
    )
    await svc.db.log_event(
        "quote_issued",
        user_id,
        f"required={fmt_xmr(quote.required_atomic)} XMR ref={quote.provider_ref}",
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.effective_chat.type != "private":
        return
    user = update.effective_user
    if user is None:
        return
    await update.message.reply_text(GATE_PROMPT, reply_markup=_gate_keyboard())


async def on_generate_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or update.effective_user is None:
        return
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass
    await _deliver_quote(
        context,
        query.message.chat_id,
        update.effective_user.id,
        update.effective_user.username,
    )


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
        days = max(0, (int(row["expiration_date"]) - now_ts()) // 86400)
        await update.message.reply_text(
            f"Status: active\n"
            f"Expires: {fmt_ts(int(row['expiration_date']))} ({days} day(s) left)"
        )
    else:
        await update.message.reply_text(
            f"Status: {row['status']}\nSend /start to receive a current tribute quote."
        )


async def reject_submissions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.effective_chat.type != "private":
        return
    if update.effective_user is not None and update.effective_user.id == CFG.admin_user_id:
        return
    await update.message.reply_text(RECEIPT_REJECTION)


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
    "`/diag` — payment backend connectivity report\n"
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
    if svc.admin_sessions.get(update.effective_user.id, 0) < time.monotonic():
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
        await context.bot.send_message(
            update.effective_chat.id, "Authentication failed."
        )
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
            f"   status={row['status']}  expires={fmt_ts(row['expiration_date'])}"
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
            f"   ref={str(row['txid'])[:28]}…\n"
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
        target, days = int(context.args[0]), int(context.args[1])
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

    if await svc.db.get_user(target) is None:
        await update.message.reply_text(f"No such user: {target}")
        return

    ok = await svc.expel(context.application, target, "manual revocation by admin")
    await svc.dm_user(
        context.application, target,
        "Your access to the inner channel has been withdrawn by the keeper.",
    )
    await update.message.reply_text(
        f"User {target} revoked. Channel removal: {'ok' if ok else 'see logs'}"
    )


async def cmd_diag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_ok(update, context):
        return
    svc: TempleService = context.application.bot_data["svc"]
    await update.message.reply_text("Probing payment backend…")
    try:
        report = await svc.provider.diagnostics()
    except Exception as exc:  # noqa: BLE001 - diagnostics must never throw
        report = f"diagnostics raised: {exc}"
    counts = await svc.db.count_by_status()
    await update.message.reply_text(
        f"Provider: {svc.provider.name}\n"
        f"{report}\n\n"
        f"Members: {counts or '(none)'}"
    )


async def cmd_sweep(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_ok(update, context):
        return
    await update.message.reply_text("Running expiry sweep…")
    await job_enforce_expiry(context)
    await update.message.reply_text("Sweep complete.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled exception in handler", exc_info=context.error)


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


async def post_init(app: Application) -> None:
    db = Database(CFG.db_path)
    await db.connect()

    oracle = PriceOracle()
    rate = await oracle.xmr_usd()
    log.info("Price oracle online: 1 XMR = $%s", rate)

    provider = build_provider(CFG, oracle)
    try:
        await provider.verify()
    except PaymentError as exc:
        log.critical("PAYMENT BACKEND UNAVAILABLE — refusing to start.\n%s", exc)
        raise SystemExit(1) from exc
    log.info("Payment provider: %s", provider.name)

    svc = TempleService(CFG, db, provider, oracle)
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
        job_poll_payments, interval=CFG.poll_interval, first=10, name="poll_payments"
    )
    app.job_queue.run_repeating(
        job_enforce_expiry, interval=86400, first=60, name="enforce_expiry"
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
    app.add_handler(CommandHandler("diag", cmd_diag))

    app.add_handler(
        CallbackQueryHandler(on_generate_click, pattern=f"^{GENERATE_CALLBACK}$")
    )

    # Any non-command private message is an attempted receipt submission.
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, reject_submissions)
    )
    app.add_error_handler(on_error)

    log.info("Starting polling loop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
