import json
import logging
import os
import random
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks


CONFIG_FILE = Path("config.json")
DEFAULT_MIN_FREQ = 100.0
DEFAULT_MAX_FREQ = 999.9
DEFAULT_TIME = "10:00"
DEFAULT_HISTORY_LIMIT = 10
DEFAULT_NO_REPEAT_WINDOW = 3

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
    last_sent_date: Optional[str] = None
    avoid_repetition: bool = False
    history: list[float] = field(default_factory=list)
    history_limit: int = DEFAULT_HISTORY_LIMIT
    no_repeat_window: int = DEFAULT_NO_REPEAT_WINDOW
    last_message_id: Optional[int] = None


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

            channel = await self.get_configured_channel(guild, config.channel_id)
            if channel is None:
                continue

            frequency = self.generate_frequency(config)
            content = f"📻 **Frequência de hoje:** `{frequency:.1f}`"
            message = await self.replace_last_frequency_message(channel, config, content)
            if message is None:
                continue

            self.register_sent_frequency(config, frequency, current_date, message.id)
            self.config_store.update_guild(guild.id, config)
            logger.info("Frequencia %.1f enviada para guild %s", frequency, guild.id)

    @daily_frequency_task.before_loop
    async def before_daily_frequency_task(self) -> None:
        await self.wait_until_ready()

    async def get_configured_channel(
        self,
        guild: discord.Guild,
        channel_id: int,
    ) -> Optional[discord.TextChannel]:
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                fetched = await guild.fetch_channel(channel_id)
                channel = fetched
            except discord.DiscordException:
                logger.exception("Nao consegui acessar o canal %s da guild %s", channel_id, guild.id)
                return None

        if not isinstance(channel, discord.TextChannel):
            logger.warning("Canal %s da guild %s nao e um canal de texto", channel_id, guild.id)
            return None

        return channel

    async def replace_last_frequency_message(
        self,
        channel: discord.TextChannel,
        config: GuildConfig,
        content: str,
    ) -> Optional[discord.Message]:
        await self.delete_previous_frequency_message(channel, config)

        try:
            return await channel.send(content)
        except discord.DiscordException:
            logger.exception("Falha ao enviar mensagem de frequencia no canal %s", channel.id)
            return None

    async def delete_previous_frequency_message(
        self,
        channel: discord.TextChannel,
        config: GuildConfig,
    ) -> None:
        if not config.last_message_id:
            return

        try:
            previous_message = await channel.fetch_message(config.last_message_id)
        except discord.NotFound:
            config.last_message_id = None
            return
        except discord.Forbidden:
            logger.warning(
                "Sem permissao para buscar/apagar a mensagem anterior no canal %s. Dê Manage Messages ao bot.",
                channel.id,
            )
            return
        except discord.DiscordException:
            logger.exception("Falha ao buscar mensagem anterior no canal %s", channel.id)
            return

        try:
            await previous_message.delete()
            config.last_message_id = None
        except discord.Forbidden:
            logger.warning(
                "Sem permissao para apagar a mensagem anterior no canal %s. Dê Manage Messages ao bot.",
                channel.id,
            )
        except discord.DiscordException:
            logger.exception("Falha ao apagar mensagem anterior no canal %s", channel.id)

    def register_sent_frequency(
        self,
        config: GuildConfig,
        frequency: float,
        sent_date: Optional[str],
        message_id: int,
    ) -> None:
        config.last_frequency = frequency
        config.last_message_id = message_id
        if sent_date is not None:
            config.last_sent_date = sent_date

        config.history.append(frequency)
        if len(config.history) > max(1, config.history_limit):
            config.history = config.history[-config.history_limit :]

    @staticmethod
    def generate_frequency(config: GuildConfig) -> float:
        min_freq = config.min_freq
        max_freq = config.max_freq
        if min_freq >= max_freq:
            raise ValueError("min_freq deve ser menor que max_freq")

        blocked_values: set[float] = set()
        if config.avoid_repetition and config.history:
            blocked_values.update(config.history[-max(1, config.no_repeat_window) :])
        elif config.last_frequency is not None:
            blocked_values.add(config.last_frequency)

        for _ in range(120):
            value = round(random.uniform(min_freq, max_freq), 1)
            if value not in blocked_values:
                return value

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

    channel = await bot.get_configured_channel(interaction.guild, config.channel_id)
    if channel is None:
        await interaction.response.send_message("Nao encontrei o canal configurado. Configure novamente.", ephemeral=True)
        return

    frequency = bot.generate_frequency(config)
    message = await bot.replace_last_frequency_message(channel, config, f"📻 **Frequência extra:** `{frequency:.1f}`")
    if message is None:
        await interaction.response.send_message("Nao consegui enviar a frequencia. Veja os logs do bot.", ephemeral=True)
        return

    bot.register_sent_frequency(config, frequency, None, message.id)
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
    ultima = f"{config.last_frequency:.1f}" if config.last_frequency is not None else "nenhuma"
    evitar = "ativado" if config.avoid_repetition else "desativado"
    await interaction.response.send_message(
        (
            f"Canal: {channel_mention}\n"
            f"Horario: `{config.hour_minute}`\n"
            f"Faixa: `{config.min_freq:.1f}` ate `{config.max_freq:.1f}`\n"
            f"Ultima frequencia: `{ultima}`\n"
            f"Ultimo envio: `{config.last_sent_date if config.last_sent_date else 'nunca'}`\n"
            f"Evitar repeticao: `{evitar}`"
        ),
        ephemeral=True,
    )


@bot.tree.command(name="fixar_frequencia", description="Define e envia uma frequencia manualmente")
@app_commands.describe(frequencia="Frequencia manual, ex: 222.5")
async def fixar_frequencia(
    interaction: discord.Interaction,
    frequencia: app_commands.Range[float, 1, 9999],
) -> None:
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

    channel = await bot.get_configured_channel(interaction.guild, config.channel_id)
    if channel is None:
        await interaction.response.send_message("Nao encontrei o canal configurado. Configure novamente.", ephemeral=True)
        return

    freq = round(float(frequencia), 1)
    message = await bot.replace_last_frequency_message(channel, config, f"📻 **Frequência oficial da noite:** `{freq:.1f}`")
    if message is None:
        await interaction.response.send_message("Nao consegui enviar a frequencia. Veja os logs do bot.", ephemeral=True)
        return

    bot.register_sent_frequency(config, freq, None, message.id)
    bot.config_store.update_guild(interaction.guild.id, config)

    await interaction.response.send_message(
        f"Frequencia fixada e enviada em {channel.mention}: `{freq:.1f}`",
        ephemeral=True,
    )


@bot.tree.command(name="historico_frequencias", description="Mostra as ultimas frequencias usadas")
async def historico_frequencias(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Esse comando so funciona em servidor.", ephemeral=True)
        return

    config = bot.config_store.get_guild(interaction.guild.id)
    if not config.history:
        await interaction.response.send_message("Ainda nao ha historico de frequencias neste servidor.", ephemeral=True)
        return

    historico = "\n".join(f"- `{freq:.1f}`" for freq in reversed(config.history))
    await interaction.response.send_message(f"📜 **Ultimas frequencias:**\n{historico}", ephemeral=True)


@bot.tree.command(name="evitar_repeticao", description="Ativa ou desativa o bloqueio de repeticao recente")
@app_commands.describe(ativado="Escolha se o bot deve evitar repetir frequencias recentes")
async def evitar_repeticao(
    interaction: discord.Interaction,
    ativado: bool,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Esse comando so funciona em servidor.", ephemeral=True)
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Voce precisa ser administrador para usar esse comando.", ephemeral=True)
        return

    config = bot.config_store.get_guild(interaction.guild.id)
    config.avoid_repetition = ativado
    bot.config_store.update_guild(interaction.guild.id, config)

    status = "ativado" if ativado else "desativado"
    await interaction.response.send_message(f"Evitar repeticao foi **{status}**.", ephemeral=True)


@bot.tree.command(name="frequencia_evento", description="Envia a frequencia com mensagem especial de evento")
async def frequencia_evento(interaction: discord.Interaction) -> None:
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

    channel = await bot.get_configured_channel(interaction.guild, config.channel_id)
    if channel is None:
        await interaction.response.send_message("Nao encontrei o canal configurado. Configure novamente.", ephemeral=True)
        return

    frequency = bot.generate_frequency(config)
    content = f"✨ **A Lux abre as portas...**\n📻 **Sintonize:** `{frequency:.1f}`"
    message = await bot.replace_last_frequency_message(channel, config, content)
    if message is None:
        await interaction.response.send_message("Nao consegui enviar a frequencia. Veja os logs do bot.", ephemeral=True)
        return

    bot.register_sent_frequency(config, frequency, None, message.id)
    bot.config_store.update_guild(interaction.guild.id, config)

    await interaction.response.send_message(
        f"Frequencia de evento enviada em {channel.mention}: `{frequency:.1f}`",
        ephemeral=True,
    )


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("Defina a variavel de ambiente DISCORD_TOKEN com o token do bot.")

    bot.run(token)


if __name__ == "__main__":
    main()
