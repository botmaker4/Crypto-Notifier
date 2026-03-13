"""
bot.py — Main entrypoint. Runs the Discord bot and webhook server concurrently.

Architecture:
  - uvicorn (FastAPI) runs as a background asyncio task in the same event loop
  - A shared asyncio.Queue bridges the webhook server → bot notification logic
  - On startup: registers all configured addresses with Tatum
  - Background task: drains the queue and sends Discord DMs
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands, tasks
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

# ── Discord bot setup ──────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = False  # Not needed for DM-only bot

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None,
)

# ── Embed colours ──────────────────────────────────────────────────────────────
COLOR_PENDING   = 0xF4C542   # Gold/Yellow
COLOR_CONFIRMED = 0x2ECC71   # Green
COLOR_ERROR     = 0xE74C3C   # Red

# ── Chain emoji map ───────────────────────────────────────────────────────────
CHAIN_EMOJI: dict[str, str] = {
    "LTC":   "🪙",
    "BSC":   "🔶",
    "MATIC": "🟣",
    "SOL":   "◎",
    "BNB":   "🟡",
}


# ── Embed Builders ────────────────────────────────────────────────────────────

def _short_txid(txid: str, length: int = 16) -> str:
    """Shorten a transaction hash for display."""
    if len(txid) <= length * 2 + 3:
        return txid
    return f"{txid[:length]}...{txid[-8:]}"


def _explorer_link(record: TxRecord) -> str:
    base = tatum_client.EXPLORER_URLS.get(record.chain, "")
    if base:
        return f"[View on Explorer]({base}{record.txid})"
    return "N/A"


def _format_amount(record: TxRecord) -> str:
    """Format amount with chain ticker."""
    tickers = {
        "LTC":   "LTC",
        "BSC":   "USDT",
        "MATIC": "USDT",
        "SOL":   "SOL",
        "BNB":   "BNB",
    }
    ticker = tickers.get(record.chain, "")
    return f"`{record.amount} {ticker}`"


def _build_embed(record: TxRecord, event_type: str) -> discord.Embed:
    """
    Build a rich Discord embed for a transaction event.

    event_type: "new" | "confirmed"
    """
    is_confirmed = event_type == "confirmed"
    chain_label  = tatum_client.CHAIN_LABELS.get(record.chain, record.chain)
    emoji        = CHAIN_EMOJI.get(record.chain, "💰")

    if is_confirmed:
        title  = f"{emoji} Transaction Confirmed"
        colour = COLOR_CONFIRMED
        status = "✅ Confirmed"
    else:
        title  = f"{emoji} New Transaction Detected"
        colour = COLOR_PENDING
        status = "⏳ Pending"

    embed = discord.Embed(
        title=title,
        colour=colour,
        timestamp=datetime.now(timezone.utc),
    )

    # ── Transaction Info ──
    embed.add_field(name="🌐 Network",    value=chain_label,               inline=True)
    embed.add_field(name="📊 Status",     value=status,                    inline=True)
    embed.add_field(name="✅ Confirmations", value=f"`{record.confirmations}`", inline=True)

    embed.add_field(name="💵 Amount",     value=_format_amount(record),    inline=True)
    usd = f"`${record.usd_value}`" if record.usd_value else "`N/A`"
    embed.add_field(name="💲 USD Value",  value=usd,                       inline=True)
    embed.add_field(name="\u200b",        value="\u200b",                  inline=True)  # spacer

    # ── Address & TXID ──
    embed.add_field(
        name="📬 Receiving Address",
        value=f"`{record.address}`",
        inline=False,
    )
    embed.add_field(
        name="🔗 Transaction Hash",
        value=f"`{record.txid}`",
        inline=False,
    )

    # ── Block Details ──
    block_val = f"`{record.block_height}`" if record.block_height else "`N/A`"
    embed.add_field(name="📦 Block Height", value=block_val, inline=True)

    ts_val = f"<t:{record.timestamp}:F>" if record.timestamp and record.timestamp.isdigit() else (record.timestamp or "N/A")
    embed.add_field(name="🕒 Timestamp",    value=ts_val,                  inline=True)
    embed.add_field(name="\u200b",          value="\u200b",                inline=True)  # spacer

    # ── Explorer link ──
    base_url = tatum_client.EXPLORER_URLS.get(record.chain, "")
    if base_url:
        embed.add_field(
            name="🔍 Blockchain Explorer",
            value=f"[View Transaction ↗]({base_url}{record.txid})",
            inline=False,
        )

    embed.set_footer(text="Crypto Notifier  •  Powered by Tatum")
    return embed


# ── DM Sender ─────────────────────────────────────────────────────────────────

async def _send_dm(embed: discord.Embed) -> bool:
    """Fetch the configured user and send them a DM."""
    try:
        user: Optional[discord.User] = await bot.fetch_user(config.DISCORD_USER_ID)
        if user is None:
            log.error("Could not find Discord user ID %d", config.DISCORD_USER_ID)
            return False
        dm = await user.create_dm()
        await dm.send(embed=embed)
        log.info("DM sent to user %d", config.DISCORD_USER_ID)
        return True
    except discord.Forbidden:
        log.error("Cannot send DM to user %d — they may have DMs disabled.", config.DISCORD_USER_ID)
    except discord.HTTPException as e:
        log.error("Discord HTTP error sending DM: %s", e)
    return False


# ── Notification Logic ────────────────────────────────────────────────────────

async def _process_event(record: TxRecord) -> None:
    """
    Decides which (if any) DM to send based on the record's state.

    Rules:
      - Event 1: new tx  →  notified_new == False → send "new tx" DM
      - Event 2: ≥2 confs → notified_confirmed == False → send "confirmed" DM
    """
    from transaction_store import store

    # ── Event 1: First detection ──
    if not record.notified_new:
        embed = _build_embed(record, "new")
        success = await _send_dm(embed)
        if success:
            await store.mark_notified_new(record.txid)
            log.info("Notified (new tx): %s", record.txid)

    # ── Event 2: ≥ 2 confirmations ──
    if record.confirmations >= 2 and not record.notified_confirmed:
        embed = _build_embed(record, "confirmed")
        success = await _send_dm(embed)
        if success:
            await store.mark_notified_confirmed(record.txid)
            log.info("Notified (confirmed): %s", record.txid)


# ── Queue Consumer Task ───────────────────────────────────────────────────────

async def _queue_consumer() -> None:
    """Background coroutine — drains event_queue and processes each tx event."""
    log.info("Event queue consumer started.")
    while True:
        try:
            record: TxRecord = await asyncio.wait_for(event_queue.get(), timeout=5.0)
            await _process_event(record)
            event_queue.task_done()
        except asyncio.TimeoutError:
            continue  # No events — keep looping
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
    """Run the FastAPI/uvicorn server in the same event loop as discord.py."""
    server_config = uvicorn.Config(
        app=webhook_server.app,
        host="0.0.0.0",
        port=config.WEBHOOK_PORT,
        log_level="warning",  # uvicorn access logs suppressed; our logger handles it
        loop="none",          # Use the existing event loop
    )
    server = uvicorn.Server(server_config)
    log.info("Webhook server starting on port %d", config.WEBHOOK_PORT)
    await server.serve()


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    # Inject the shared queue into the webhook server
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
