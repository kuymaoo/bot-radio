import asyncio
import json
import logging
import os
import random
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks


CONFIG_FILE = Path("config.json")
DEFAULT_MIN_FREQ = 100.0
DEFAULT_MAX_FREQ = 999.9
DEFAULT_TIME = "10:00"  # horario local do servidor onde o bot estiver rodando

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("radio-frequency-bot")


@dataclass
class GuildConfig:
    channel_id: Optional[int] = None
    hour_minute: str = DEFAULT_TIME
    min_freq: float = DEFAULT_MIN_FREQ
    max_freq: float = DEFAULT_MAX_FREQ
    last_frequency: Optional[float] = None
    last_sent_date: Optional[str] = None  # YYYY-MM-DD em horario local do bot


class ConfigStore:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, GuildConfig] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.data = {}
            return

        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.data = {
            guild_id: GuildConfig(**config)
            for guild_id, config in raw.items()
        }

    def save(self) -> None:
        serializable = {
            guild_id: asdict(config)
            for guild_id, config in self.data.items()
        }
        self.path.write_text(
            json.dumps(serializable, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_guild(self, guild_id: int) -> GuildConfig:
        key = str(guild_id)
        if key not in self.data:
            self.data[key] = GuildConfig()
            self.save()
        return self.data[key]

    def update_guild(self, guild_id: int, config: GuildConfig) -> None:
        self.data[str(guild_id)] = config
        self.save()


class RadioBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.config_store = ConfigStore(CONFIG_FILE)

    async def setup_hook(self) -> None:
        self.daily_frequency_task.start()
        if guild_id := os.getenv("TEST_GUILD_ID"):
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("Slash commands sincronizados na guild de teste %s", guild_id)
        else:
            await self.tree.sync()
            logger.info("Slash commands globais sincronizados")

    async def on_ready(self) -> None:
        if self.user:
            logger.info("Logado como %s (%s)", self.user, self.user.id)

    @tasks.loop(minutes=1)
    async def daily_frequency_task(self) -> None:
        now = datetime.now().astimezone()
        current_date = now.strftime("%Y-%m-%d")
        current_hm = now.strftime("%H:%M")

        for guild in self.guilds:
            config = self.config_store.get_guild(guild.id)

            if not config.channel_id:
                continue
            if config.hour_minute != current_hm:
                continue
            if config.last_sent_date == current_date:
                continue

            channel = guild.get_channel(config.channel_id)
            if channel is None:
                try:
                    fetched = await guild.fetch_channel(config.channel_id)
                    channel = fetched
                except discord.DiscordException:
                    logger.exception("Nao consegui acessar o canal %s da guild %s", config.channel_id, guild.id)
                    continue

            if not isinstance(channel, discord.abc.Messageable):
                continue

            frequency = self.generate_frequency(config.min_freq, config.max_freq, config.last_frequency)
            message = f"📻 **Frequência de hoje:** `{frequency:.1f}`"

            try:
                await channel.send(message)
            except discord.DiscordException:
                logger.exception("Falha ao enviar frequencia para a guild %s", guild.id)
                continue

            config.last_frequency = frequency
            config.last_sent_date = current_date
            self.config_store.update_guild(guild.id, config)
            logger.info("Frequencia %.1f enviada para guild %s", frequency, guild.id)

    @daily_frequency_task.before_loop
    async def before_daily_frequency_task(self) -> None:
        await self.wait_until_ready()

    @staticmethod
    def generate_frequency(min_freq: float, max_freq: float, previous: Optional[float]) -> float:
        if min_freq >= max_freq:
            raise ValueError("min_freq deve ser menor que max_freq")

        for _ in range(50):
            value = round(random.uniform(min_freq, max_freq), 1)
            if previous is None or value != previous:
                return value

        # fallback improvavel
        return round(random.uniform(min_freq, max_freq), 1)


bot = RadioBot()


@bot.tree.command(name="configurar_radio", description="Define o canal, horario e faixa de frequencia diaria")
@app_commands.describe(
    canal="Canal onde o bot vai enviar a frequencia",
    horario="Horario diario no formato HH:MM, ex: 20:00",
    frequencia_min="Numero minimo da frequencia, ex: 100.0",
    frequencia_max="Numero maximo da frequencia, ex: 999.9",
)
async def configurar_radio(
    interaction: discord.Interaction,
    canal: discord.TextChannel,
    horario: str,
    frequencia_min: app_commands.Range[float, 1, 9999] = DEFAULT_MIN_FREQ,
    frequencia_max: app_commands.Range[float, 1, 9999] = DEFAULT_MAX_FREQ,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Esse comando so funciona em servidor.", ephemeral=True)
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Voce precisa ser administrador para configurar o bot.", ephemeral=True)
        return

    try:
        datetime.strptime(horario, "%H:%M")
    except ValueError:
        await interaction.response.send_message("Use o horario no formato **HH:MM**. Exemplo: `20:00`.", ephemeral=True)
        return

    if frequencia_min >= frequencia_max:
        await interaction.response.send_message("A frequencia minima precisa ser menor que a maxima.", ephemeral=True)
        return

    config = bot.config_store.get_guild(interaction.guild.id)
    config.channel_id = canal.id
    config.hour_minute = horario
    config.min_freq = round(float(frequencia_min), 1)
    config.max_freq = round(float(frequencia_max), 1)
    bot.config_store.update_guild(interaction.guild.id, config)

    await interaction.response.send_message(
        (
            "Configuração salva.\n"
            f"Canal: {canal.mention}\n"
            f"Horario: `{config.hour_minute}`\n"
            f"Faixa: `{config.min_freq:.1f}` ate `{config.max_freq:.1f}`"
        ),
        ephemeral=True,
    )


@bot.tree.command(name="frequencia_agora", description="Envia a frequencia do dia imediatamente")
async def frequencia_agora(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Esse comando so funciona em servidor.", ephemeral=True)
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Voce precisa ser administrador para usar esse comando.", ephemeral=True)
        return

    config = bot.config_store.get_guild(interaction.guild.id)
    if not config.channel_id:
        await interaction.response.send_message("Configure primeiro com `/configurar_radio`.", ephemeral=True)
        return

    channel = interaction.guild.get_channel(config.channel_id)
    if channel is None:
        await interaction.response.send_message("Nao encontrei o canal configurado. Configure novamente.", ephemeral=True)
        return

    frequency = bot.generate_frequency(config.min_freq, config.max_freq, config.last_frequency)
    await channel.send(f"📻 **Frequência extra:** `{frequency:.1f}`")

    config.last_frequency = frequency
    bot.config_store.update_guild(interaction.guild.id, config)

    await interaction.response.send_message(
        f"Enviei uma frequencia extra em {channel.mention}: `{frequency:.1f}`",
        ephemeral=True,
    )


@bot.tree.command(name="ver_radio", description="Mostra a configuracao atual da radio")
async def ver_radio(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Esse comando so funciona em servidor.", ephemeral=True)
        return

    config = bot.config_store.get_guild(interaction.guild.id)
    if not config.channel_id:
        await interaction.response.send_message("Ainda nao ha configuracao salva para este servidor.", ephemeral=True)
        return

    channel_mention = f"<#{config.channel_id}>"
    await interaction.response.send_message(
        (
            f"Canal: {channel_mention}\n"
            f"Horario: `{config.hour_minute}`\n"
            f"Faixa: `{config.min_freq:.1f}` ate `{config.max_freq:.1f}`\n"
            f"Ultima frequencia: `{config.last_frequency if config.last_frequency is not None else 'nenhuma'}`\n"
            f"Ultimo envio: `{config.last_sent_date if config.last_sent_date else 'nunca'}`"
        ),
        ephemeral=True,
    )


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("Defina a variavel de ambiente DISCORD_TOKEN com o token do bot.")

    bot.run(token)


if __name__ == "__main__":
    main()
