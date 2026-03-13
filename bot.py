"""
bot.py — Main entrypoint. Runs the Discord bot and webhook server concurrently.

Architecture:
  - uvicorn (FastAPI) runs as a background asyncio task in the same event loop
  - A shared asyncio.Queue bridges the webhook server → bot notification logic
  - On startup: registers all configured addresses with Tatum
  - Background task: drains the queue and sends Discord DMs
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import discord
import httpx
from discord.ext import commands
import uvicorn

import config
import tatum_client
import webhook_server
from transaction_store import TxRecord

# ── Initialise config (validates env vars) ────────────────────────────────────
config.validate()
log = logging.getLogger("crypto-notifier.bot")

# ── Shared event queue ────────────────────────────────────────────────────────
event_queue: asyncio.Queue = asyncio.Queue()

# ── Discord bot setup ─────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = False

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ── Embed colours ─────────────────────────────────────────────────────────────
COLOR_PENDING   = 0xF4C430   # Vivid gold
COLOR_CONFIRMED = 0x2ECC71   # Emerald green

# ── Emoji loader ──────────────────────────────────────────────────────────────
_EMOJI_PATH = os.path.join(os.path.dirname(__file__), "emojis.json")

def _load_emojis() -> dict[str, str]:
    try:
        with open(_EMOJI_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning("Could not load emojis.json: %s — using defaults", e)
        return {}

EMOJIS: dict[str, str] = _load_emojis()

def E(key: str, fallback: str = "") -> str:
    """Get an emoji by key from emojis.json."""
    return EMOJIS.get(key, fallback)


# ── CoinGecko USD price fetcher ───────────────────────────────────────────────
# Maps asset symbol (from Tatum payload) to CoinGecko coin ID.
# Stablecoins (USDT, USDC) map to None meaning $1.00 per unit.
COINGECKO_IDS: dict[str, Optional[str]] = {
    "LTC":   "litecoin",
    "SOL":   "solana",
    "BNB":   "binancecoin",
    "MATIC": "matic-network",
    "USDT":  None,   # stablecoin, always $1
    "USDC":  None,
    "BUSD":  None,
}

# Simple price cache to avoid hammering CoinGecko on every tx
_price_cache: dict[str, float] = {}
_price_cache_ts: dict[str, float] = {}
_PRICE_TTL = 120  # seconds


async def _get_usd_price(asset_symbol: str) -> Optional[float]:
    """
    Fetch the current USD price for a given asset symbol.
    Stablecoins (USDT, USDC, BUSD) return 1.0 immediately.
    Uses a 2-minute in-memory cache per asset.
    """
    symbol = asset_symbol.upper()

    # Stablecoin shortcut
    if COINGECKO_IDS.get(symbol) is None and symbol in COINGECKO_IDS:
        return 1.0

    coin_id = COINGECKO_IDS.get(symbol)
    if not coin_id:
        log.debug("No CoinGecko mapping for asset %s", symbol)
        return None

    now = asyncio.get_running_loop().time()
    if symbol in _price_cache:
        if now - _price_cache_ts.get(symbol, 0) < _PRICE_TTL:
            return _price_cache[symbol]

    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": coin_id, "vs_currencies": "usd"},
            )
            resp.raise_for_status()
            data = resp.json()
            price: float = data[coin_id]["usd"]
            _price_cache[symbol] = price
            _price_cache_ts[symbol] = now
            log.debug("CoinGecko price for %s: $%.4f", symbol, price)
            return price
    except Exception as e:
        log.warning("Could not fetch USD price for %s: %s", symbol, e)
        return None


async def _compute_usd_value(record: TxRecord) -> Optional[str]:
    """Return formatted USD value string, or None if unavailable."""
    # If Tatum already gave us a USD value, use it
    if record.usd_value:
        try:
            return f"${float(record.usd_value):,.2f}"
        except ValueError:
            pass

    # Determine the asset symbol to price:
    # prefer record.asset (e.g. 'SOL' on BSC), fallback to chain-default ticker
    asset_symbol = (
        record.asset
        or tatum_client.CHAIN_TICKERS.get(record.chain, "")
    ).upper()

    try:
        price = await _get_usd_price(asset_symbol)
        amount = float(record.amount)
        if price is not None:
            return f"${price * amount:,.2f}"
    except Exception as e:
        log.debug("USD calc failed: %s", e)

    return None


# ── Embed Builder ─────────────────────────────────────────────────────────────

def _short_hash(h: str, head: int = 10, tail: int = 8) -> str:
    if len(h) <= head + tail + 3:
        return h
    return f"{h[:head]}...{h[-tail:]}"


def _build_embed(record: TxRecord, event_type: str, usd_value: Optional[str]) -> discord.Embed:
    """
    Build a rich, cleanly formatted Discord embed.
    event_type: "new" | "confirmed"
    """
    is_confirmed  = event_type == "confirmed"
    chain_label   = tatum_client.CHAIN_LABELS.get(record.chain, record.chain)
    # Use asset symbol from payload (e.g. 'SOL' on BSC) as the displayed ticker
    ticker = (
        record.asset
        or tatum_client.CHAIN_TICKERS.get(record.chain, "")
    )
    chain_emoji   = E(record.chain.lower(), E("fallback", "💎"))
    explorer_base = tatum_client.EXPLORER_URLS.get(record.chain, "")

    if is_confirmed:
        title       = f"{chain_emoji}  Transaction Confirmed"
        colour      = COLOR_CONFIRMED
        status_val  = f"{E('confirmed', '✅')}  Confirmed"
        footer_text = f"Transaction Confirmed  •  Made by xavierlol"
    else:
        title       = f"{chain_emoji}  New Transaction Detected"
        colour      = COLOR_PENDING
        status_val  = f"{E('pending', '⏳')}  Pending"
        footer_text = f"Transaction Detected  •  Made by xavierlol"

    embed = discord.Embed(
        title=title,
        colour=colour,
        timestamp=datetime.now(timezone.utc),
    )

    # ── Row 1: Network | Status | Confirmations ──
    embed.add_field(
        name=f"{E('network', '🌐')}  Network",
        value=f"**{chain_label}**",
        inline=True,
    )
    embed.add_field(
        name=f"{E('status', '📊')}  Status",
        value=status_val,
        inline=True,
    )
    embed.add_field(
        name=f"{E('confirmations', '✅')}  Confirmations",
        value=f"**{record.confirmations}**",
        inline=True,
    )

    # ── Row 2: Amount | USD Value | blank ──
    amount_str = f"**{record.amount} {ticker}**" if ticker else f"**{record.amount}**"
    usd_str    = f"**{usd_value}**" if usd_value else "*Unavailable*"

    embed.add_field(
        name=f"{E('amount', '💰')}  Amount",
        value=amount_str,
        inline=True,
    )
    embed.add_field(
        name=f"{E('usd', '💲')}  USD Value",
        value=usd_str,
        inline=True,
    )
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    # ── Row 3: Receiving Address (full width) ──
    embed.add_field(
        name=f"{E('address', '📬')}  Receiving Address",
        value=f"```{record.address}```",
        inline=False,
    )

    # ── Row 4: Transaction Hash (full width) ──
    embed.add_field(
        name=f"{E('txid', '🔗')}  Transaction Hash",
        value=f"```{record.txid}```",
        inline=False,
    )

    # ── Row 5: Block Height | Timestamp ──
    block_val = f"**{record.block_height}**" if record.block_height else "*Unknown*"

    if record.timestamp and record.timestamp.isdigit():
        ts_val = f"<t:{record.timestamp}:F>"
    else:
        ts_val = record.timestamp or "*Unknown*"

    embed.add_field(
        name=f"{E('block', '📦')}  Block Height",
        value=block_val,
        inline=True,
    )
    embed.add_field(
        name=f"{E('time', '🕒')}  Time",
        value=ts_val,
        inline=True,
    )
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    # ── Row 6: Explorer link ──
    if explorer_base:
        embed.add_field(
            name=f"{E('explorer', '🔍')}  Blockchain Explorer",
            value=f"[{E('explorer', '🔍')} View Transaction ↗]({explorer_base}{record.txid})",
            inline=False,
        )

    embed.set_footer(text=footer_text)
    return embed


# ── DM Sender ─────────────────────────────────────────────────────────────────

async def _send_dm(embed: discord.Embed) -> bool:
    try:
        log.info("Fetching Discord user %d to send DM...", config.DISCORD_USER_ID)
        user: Optional[discord.User] = await bot.fetch_user(config.DISCORD_USER_ID)
        if user is None:
            log.error("Could not find Discord user ID %d", config.DISCORD_USER_ID)
            return False
        log.info("Opening DM channel with %s...", user)
        dm = await user.create_dm()
        await dm.send(embed=embed)
        log.info("DM sent successfully to %s (ID: %d)", user, config.DISCORD_USER_ID)
        return True
    except discord.Forbidden:
        log.error(
            "Cannot send DM to user %d — make sure the bot shares a server with that user "
            "or has already received a message from them.",
            config.DISCORD_USER_ID,
        )
    except discord.HTTPException as e:
        log.error("Discord HTTP error sending DM (status=%s): %s", e.status, e)
    except Exception as e:
        log.exception("Unexpected error in _send_dm: %s", e)
    return False


# ── Notification Logic ────────────────────────────────────────────────────────

async def _process_event(record: TxRecord) -> None:
    from transaction_store import store

    log.info(
        "Processing event: txid=%s chain=%s asset=%s amount=%s confirmations=%d "
        "notified_new=%s notified_confirmed=%s",
        record.txid[:16], record.chain, record.asset, record.amount,
        record.confirmations, record.notified_new, record.notified_confirmed,
    )

    # Fetch USD value once for both potential embeds
    usd_value = await _compute_usd_value(record)
    log.info("USD value computed: %s", usd_value or "N/A")

    # ── Event 1: First detection ──
    if not record.notified_new:
        log.info("Sending 'new tx' DM for %s...", record.txid[:16])
        embed = _build_embed(record, "new", usd_value)
        success = await _send_dm(embed)
        if success:
            await store.mark_notified_new(record.txid)
            log.info("Notified (new tx): %s", record.txid)
    else:
        log.debug("Skipping 'new tx' DM — already notified for %s", record.txid[:16])

    # ── Event 2: ≥ 2 confirmations ──
    if record.confirmations >= 2 and not record.notified_confirmed:
        log.info("Sending 'confirmed' DM for %s...", record.txid[:16])
        embed = _build_embed(record, "confirmed", usd_value)
        success = await _send_dm(embed)
        if success:
            await store.mark_notified_confirmed(record.txid)
            log.info("Notified (confirmed): %s", record.txid)
    elif record.confirmations < 2:
        log.debug("Not yet confirmed (%d/2): %s", record.confirmations, record.txid[:16])


# ── Queue Consumer Task ───────────────────────────────────────────────────────

async def _queue_consumer() -> None:
    log.info("Event queue consumer started.")
    while True:
        try:
            record: TxRecord = await asyncio.wait_for(event_queue.get(), timeout=5.0)
            await _process_event(record)
            event_queue.task_done()
        except asyncio.TimeoutError:
            continue
        except Exception as e:
            log.exception("Error processing event from queue: %s", e)


# ── Discord Bot Events ────────────────────────────────────────────────────────

@bot.event
async def on_ready() -> None:
    log.info("Discord bot ready. Logged in as %s (ID: %s)", bot.user, bot.user.id)
    log.info("Setting up Tatum subscriptions...")
    await tatum_client.setup_all_subscriptions()
    log.info("Tatum setup complete.")


@bot.event
async def on_error(event: str, *args, **kwargs) -> None:
    log.exception("Discord error in event '%s'", event)


# ── Uvicorn Server ────────────────────────────────────────────────────────────

async def _run_webhook_server() -> None:
    server_config = uvicorn.Config(
        app=webhook_server.app,
        host="0.0.0.0",
        port=config.WEBHOOK_PORT,
        log_level="warning",
        loop="none",
    )
    server = uvicorn.Server(server_config)
    log.info("Webhook server starting on port %d", config.WEBHOOK_PORT)
    await server.serve()


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    webhook_server.set_event_queue(event_queue)
    async with bot:
        await asyncio.gather(
            bot.start(config.DISCORD_TOKEN),
            _run_webhook_server(),
            _queue_consumer(),
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down Crypto Notifier Bot.")
