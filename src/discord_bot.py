"""
Discord Monitoring Bot for Polymarket Trading Bot
"""
import os
import discord
from discord.ext import commands, tasks
import asyncio
import aiohttp
import subprocess
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _require_env_int(name: str) -> int:
    raw = _require_env(name)
    try:
        return int(raw)
    except ValueError as e:
        raise RuntimeError(f"Environment variable {name} must be an integer") from e


BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
HEALTH_URL = os.getenv("DISCORD_HEALTH_URL", "http://localhost:8080/health").strip()

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
status_message_id = None

class TradingControls(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.primary, custom_id="refresh")
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        embed = await get_status_embed()
        await interaction.message.edit(embed=embed, view=self)
    
    @discord.ui.button(label="🛑 EMERGENCY STOP", style=discord.ButtonStyle.danger, custom_id="stop")
    async def emergency_stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        result = subprocess.run(["sudo", "systemctl", "stop", "polymarket-bot"], capture_output=True, text=True)
        if result.returncode == 0:
            await interaction.followup.send("🛑 **Bot STOPPED!**")
        await asyncio.sleep(1)
        embed = await get_status_embed()
        await interaction.message.edit(embed=embed, view=self)
    
    @discord.ui.button(label="▶️ Start", style=discord.ButtonStyle.success, custom_id="start")
    async def start_bot(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        subprocess.run(["sudo", "systemctl", "start", "polymarket-bot"], capture_output=True)
        await interaction.followup.send("▶️ **Bot STARTED!**")
        await asyncio.sleep(2)
        embed = await get_status_embed()
        await interaction.message.edit(embed=embed, view=self)
    
    @discord.ui.button(label="🔃 Restart", style=discord.ButtonStyle.secondary, custom_id="restart")
    async def restart_bot(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        subprocess.run(["sudo", "systemctl", "restart", "polymarket-bot"], capture_output=True)
        await interaction.followup.send("🔃 **Bot RESTARTED!**")
        await asyncio.sleep(2)
        embed = await get_status_embed()
        await interaction.message.edit(embed=embed, view=self)

async def get_health_data():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(HEALTH_URL, timeout=5) as resp:
                if resp.status == 200:
                    return await resp.json()
    except:
        pass
    return None

def get_service_status():
    result = subprocess.run(["systemctl", "is-active", "polymarket-bot"], capture_output=True, text=True)
    return result.stdout.strip()

async def get_status_embed():
    health = await get_health_data()
    service_status = get_service_status()
    
    if service_status == "active" and health:
        color = discord.Color.green()
        status_text = "🟢 ONLINE"
    elif service_status == "active":
        color = discord.Color.yellow()
        status_text = "🟡 STARTING..."
    else:
        color = discord.Color.red()
        status_text = "🔴 OFFLINE"
    
    embed = discord.Embed(title="Polymarket Trading Bot", description=f"**{status_text}**", color=color, timestamp=datetime.now(timezone.utc))
    
    # Check for paper or live performance
    trading_mode = health.get("trading_mode", "unknown") if health else "unknown"
    perf = None
    
    if health and "paper_performance" in health:
        perf = health["paper_performance"]
        mode_display = "📝 PAPER"
    elif health and "live_performance" in health:
        perf = health["live_performance"]
        mode_display = "💰 LIVE"
    
    if perf:
        engine = health.get("engine", {})
        
        # Mode
        embed.add_field(name="Mode", value=mode_display, inline=True)
        
        if "paper_performance" in health:
            # Paper mode - has full equity/balance tracking
            equity = perf.get("total_equity", 0)
            embed.add_field(name="💵 Equity", value=f"${equity:,.2f}", inline=True)
            
            cash = perf.get("current_balance", 0)
            embed.add_field(name="💰 Cash", value=f"${cash:,.2f}", inline=True)
            
            total_pnl = perf.get("total_pnl", 0)
            pnl_pct = perf.get("pnl_percent", 0)
            pnl_emoji = "📈" if total_pnl >= 0 else "📉"
            pnl_sign = "+" if total_pnl >= 0 else ""
            embed.add_field(name=f"{pnl_emoji} Total P&L", value=f"{pnl_sign}${total_pnl:,.2f} ({pnl_sign}{pnl_pct:.1f}%)", inline=True)
            
            realized = perf.get("realized_pnl", 0)
            unrealized = perf.get("unrealized_pnl", 0)
            embed.add_field(name="Realized", value=f"${realized:,.2f}", inline=True)
            embed.add_field(name="Unrealized", value=f"${unrealized:,.2f}", inline=True)
            
            trades = perf.get("total_trades", 0)
            win_rate = perf.get("win_rate", 0)
            embed.add_field(name="🔄 Trades", value=str(trades), inline=True)
            embed.add_field(name="🎯 Win Rate", value=f"{win_rate:.1f}%", inline=True)
            
            positions = perf.get("open_positions", 0)
            embed.add_field(name="📊 Positions", value=str(positions), inline=True)
            
        else:
            # Live mode - shows trade stats from LiveExecutor
            total_trades = perf.get("total_trades", 0)
            successful = perf.get("successful_trades", 0)
            failed = perf.get("failed_trades", 0)
            success_rate = perf.get("success_rate", 0)
            active_orders = perf.get("active_orders", 0)
            
            embed.add_field(name="🔄 Total Trades", value=str(total_trades), inline=True)
            embed.add_field(name="✅ Successful", value=str(successful), inline=True)
            embed.add_field(name="❌ Failed", value=str(failed), inline=True)
            embed.add_field(name="🎯 Success Rate", value=f"{success_rate:.1f}%", inline=True)
            embed.add_field(name="📋 Active Orders", value=str(active_orders), inline=True)
            embed.add_field(name="💵 Balance", value="Check Polymarket", inline=True)
        
        # Engine stats (common to both modes)
        signals = engine.get("signals_generated", 0)
        executed = engine.get("signals_executed", 0)
        embed.add_field(name="📡 Signals", value=f"{executed:,}/{signals:,}", inline=True)
        
        if "paper_performance" in health:
            maker = perf.get("maker_fills", 0)
            taker = perf.get("taker_fills", 0)
            embed.add_field(name="🏭 Maker/Taker", value=f"{maker}/{taker}", inline=True)
            
            fees = perf.get("total_fees", 0)
            embed.add_field(name="💸 Fees", value=f"${fees:.2f}", inline=True)
    else:
        embed.add_field(name="⚠️", value="No data available", inline=False)
    
    embed.set_footer(text="Last updated")
    return embed


@bot.event
async def on_ready():
    print(f"Discord bot ready: {bot.user}")
    bot.add_view(TradingControls())
    channel = bot.get_channel(CHANNEL_ID)
    if channel:
        embed = await get_status_embed()
        msg = await channel.send(embed=embed, view=TradingControls())
        global status_message_id
        status_message_id = msg.id
    auto_refresh.start()

@tasks.loop(minutes=1)
async def auto_refresh():
    global status_message_id
    if status_message_id:
        channel = bot.get_channel(CHANNEL_ID)
        if channel:
            try:
                msg = await channel.fetch_message(status_message_id)
                embed = await get_status_embed()
                await msg.edit(embed=embed, view=TradingControls())
            except:
                pass

@bot.command(name="status")
async def status_cmd(ctx):
    embed = await get_status_embed()
    await ctx.send(embed=embed, view=TradingControls())

@bot.command(name="pnl")
async def pnl_cmd(ctx):
    health = await get_health_data()
    if health and "paper_performance" in health:
        p = health["paper_performance"]
        await ctx.send(f"📈 **P&L:** ${p.get('total_pnl',0):,.2f} ({p.get('pnl_percent',0):.1f}%)\n💵 Equity: ${p.get('total_equity',0):,.2f}")
    else:
        await ctx.send("❌ Bot offline")

if __name__ == "__main__":
    if not BOT_TOKEN:
        BOT_TOKEN = _require_env("DISCORD_BOT_TOKEN")
    if not CHANNEL_ID:
        CHANNEL_ID = _require_env_int("DISCORD_CHANNEL_ID")
    bot.run(BOT_TOKEN)
