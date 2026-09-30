"""
SharpTimer Leaderboard Bot
--------------------------
Lee el ranking a través de la ranking API (https://github.com/josesilvaruiz/sharptimer-ranking-api),
que es la única que toca la base de datos MariaDB de SharpTimer — el bot ya no se
conecta a ella directamente. Muestra rankings en Discord con slash commands.

Comandos:
    /top [cantidad]            -> Ranking global por puntos (sistema propio del bot)
    /maptop <mapa> [cantidad]  -> Top tiempos de un mapa (PlayerRecords)
    /rank <jugador>            -> Posición y puntos de un jugador (nombre o SteamID)
    /maps                      -> Lista todos los mapas con records guardados

Requisitos:
    pip install --break-system-packages -U discord.py aiohttp

Configura las variables en el bloque CONFIG más abajo.
"""

import os
import time
import re
import asyncio
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

# ============== CONFIG ==============
# Token y API key se leen del entorno (nunca hardcodeados aquí, para poder subir este
# fichero a un repo git). Configúralos en el servicio systemd / .env antes de arrancar:
#   DISCORD_TOKEN, RANKING_API_KEY
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]

# El bot ya no toca MariaDB directamente: todas las consultas de ranking pasan por la
# ranking API (https://github.com/josesilvaruiz/sharptimer-ranking-api), que es la
# misma que consume la landing page, para que ambas muestren siempre lo mismo.
RANKING_API_URL = os.environ.get("RANKING_API_URL", "http://127.0.0.1:8088")
RANKING_API_KEY = os.environ["RANKING_API_KEY"]

GUILD_ID = None  # opcional: ID de tu server de Discord (número, sin comillas), para que los comandos aparezcan al instante
DEFAULT_TOP_N = 10

# Canales donde el bot puede responder NORMAL (público). Pon los IDs de canal aquí, ej: [123456789012345678]
# Si la lista está vacía, el bot responde en público en CUALQUIER canal (sin restricción).
ALLOWED_CHANNEL_IDS = [1517987446443217007,1511327127637852400]

# Fuera de esos canales, el bot SIGUE funcionando pero solo responde en privado (susurro/ephemeral),
# visible solo para quien usó el comando. Para desactivar esto y simplemente no responder fuera de
# los canales permitidos, pon ALLOW_WHISPER_OUTSIDE = False.
ALLOW_WHISPER_OUTSIDE = True

# Anti-spam: segundos que debe esperar un usuario antes de repetir el MISMO comando.
COOLDOWN_SECONDS = 10

# Canal donde se publican las notas de parche de CS2 en cuanto Valve saca una nueva.
PATCHNOTES_CHANNEL_ID = 1529548031291293870
PATCHNOTES_CHECK_MINUTES = 30
LAST_BUILDID_FILE = "./last_known_buildid.txt"
LAST_MESSAGE_ID_FILE = "./last_patchnotes_message_id.txt"
CS2_KUBE_NAMESPACE = "cs2"
CS2_KUBE_DEPLOYMENT = "cs2-server"
# Header oficial de CS2 en la store de Steam: se usa como imagen grande si el
# post de Steam no trae ninguna [img] propia, para que el embed no quede escueto.
CS2_FALLBACK_IMAGE = "https://cdn.cloudflare.steamstatic.com/steam/apps/730/header.jpg"
# =====================================


intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


def is_allowed_channel(interaction: discord.Interaction) -> bool:
    """True si el canal está en la lista blanca (o si no hay restricción configurada)."""
    if not ALLOWED_CHANNEL_IDS:
        return True
    return interaction.channel_id in ALLOWED_CHANNEL_IDS


def resolve_ephemeral(interaction: discord.Interaction):
    """
    Decide si la respuesta debe ser pública o susurro (privada), según el canal.
    Devuelve True (susurro), False (público), o None si no se debe responder en absoluto.
    """
    if is_allowed_channel(interaction):
        return False
    if ALLOW_WHISPER_OUTSIDE:
        return True
    return None


# ---------- Acceso a datos (via la ranking API, nunca MariaDB directamente) ----------

async def _api_get(path: str, **params):
    url = f"{RANKING_API_URL}{path}"
    headers = {"X-Api-Key": RANKING_API_KEY}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            return await resp.json()


async def fetch_global_top(limit: int):
    rows = await _api_get("/top", limit=limit)
    return [(r["steamId"], r["name"], r["points"]) for r in rows]


async def fetch_player_rank(query: str):
    rows = await _api_get("/rank", q=query)
    return [(r["name"], r["points"], r["steamId"], r["position"]) for r in rows]


async def fetch_map_top(map_name: str, limit: int):
    rows = await _api_get("/maptop", map=map_name, limit=limit)
    return [(r["name"], r["time"], r["finishes"]) for r in rows]


async def fetch_player_pb_on_map(map_name: str, query: str):
    """
    Busca el PB de un jugador en un mapa concreto, junto a su posición en ese mapa.
    Acepta SteamID exacto o nombre parcial. Devuelve lista de coincidencias:
    [(PlayerName, SteamID, FormattedTime, TimesFinished, Position, TotalPlayers), ...]
    """
    rows = await _api_get("/pb", map=map_name, q=query)
    return [(r["name"], r["steamId"], r["time"], r["finishes"], r["position"], r["total"]) for r in rows]


async def list_known_maps_with_counts():
    rows = await _api_get("/maps")
    return [(r["map"], r["players"]) for r in rows]


def build_help_embed() -> discord.Embed:
    embed = discord.Embed(
        title="📖 Comandos disponibles",
        description="Todos los comandos leen en vivo la base de datos de SharpTimer.",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="/top",
        value="Muestra el top 10 global de jugadores por puntos (sistema propio: premia mejor posición por mapa, no solo cantidad de runs).",
        inline=False,
    )
    embed.add_field(
        name="/maptop `mapa`",
        value="Top 10 tiempos de un mapa concreto. El nombre del mapa debe ser exacto (usa `/maps` para verlos todos).",
        inline=False,
    )
    embed.add_field(
        name="/pb `mapa` [jugador]",
        value="Consulta tu mejor tiempo y posición en un mapa. Si no pones `jugador`, busca tu propio nombre de Discord.",
        inline=False,
    )
    embed.add_field(
        name="/rank `jugador`",
        value="Busca la posición global y puntos de un jugador, por nombre (parcial) o SteamID64.",
        inline=False,
    )
    embed.add_field(
        name="/maps",
        value="Lista todos los mapas que tienen records guardados, con cuántos jugadores tiene cada uno.",
        inline=False,
    )
    embed.set_footer(text=f"Cooldown: {COOLDOWN_SECONDS}s entre usos del mismo comando")
    return embed


# Cooldown simple para menciones/DMs (separado del cooldown de los slash commands)
_mention_cooldowns = {}


def mention_on_cooldown(user_id: int) -> bool:
    now = time.monotonic()
    last = _mention_cooldowns.get(user_id, 0)
    if now - last < COOLDOWN_SECONDS:
        return True
    _mention_cooldowns[user_id] = now
    return False


# ---------- Notas de parche de CS2 (buildid real -> Steam news -> Discord) ----------
#
# El disparador es el buildid REALMENTE instalado en el server (el mismo .buildid
# que ya usa el cronjob cs2-auto-update), no "la última noticia de Steam": así solo
# se publica cuando hay un release de verdad aplicado, nunca por otro tipo de post
# de Steam (blog, esports, etc.) ni se repite si no cambió nada.

CS2_APPID = 730


def _load_last_buildid():
    try:
        with open(LAST_BUILDID_FILE, "r") as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def _save_last_buildid(buildid: str):
    with open(LAST_BUILDID_FILE, "w") as f:
        f.write(buildid)


def _load_last_message_id():
    try:
        with open(LAST_MESSAGE_ID_FILE, "r") as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def _save_last_message_id(message_id: int):
    with open(LAST_MESSAGE_ID_FILE, "w") as f:
        f.write(str(message_id))


def _get_installed_buildid():
    """Lee el buildid instalado vía kubectl (el bot corre en el mismo host que el clúster)."""
    try:
        result = subprocess.run(
            [
                "kubectl", "exec", "-n", CS2_KUBE_NAMESPACE, f"deployment/{CS2_KUBE_DEPLOYMENT}",
                "--", "cat", "/home/steam/cs2/.buildid",
            ],
            capture_output=True, text=True, timeout=15,
        )
        buildid = result.stdout.strip()
        return buildid if buildid.isdigit() else None
    except Exception as e:
        print(f"[patchnotes] Error leyendo buildid instalado: {e}")
        return None


def _clean_steam_markup(text: str) -> str:
    """Las noticias de Steam vienen con su propio BBCode ([img], [url], [list], [*]...),
    que Discord no entiende, y este endpoint además devuelve el texto sin separadores
    reales entre frases/apartados. Lo reformateamos a Markdown legible en Discord."""
    text = re.sub(r"\[img\].*?\[/img\]", "", text, flags=re.DOTALL)
    text = re.sub(r"\[url=(.*?)\](.*?)\[/url\]", r"\2 (\1)", text)
    text = re.sub(r"\[\*\]", "• ", text)
    text = re.sub(r"\[/?\w+(=[^\]]*)?\]", "", text)  # cualquier otra etiqueta: [b], [/b], [list]...
    text = re.sub(r"\.(?=[A-Z])", ". ", text)  # "audible.The" -> "audible. The"
    # Steam separa "párrafos"/apartados con un "\" suelto en vez de un salto de línea real;
    # sin esto, todo el texto queda pegado en un único bloque ilegible.
    text = re.sub(r"\\(?=\S)", "\n\n", text)
    # Las listas de mapas de Workshop vienen sin separador entre el nombre del mapa y
    # la frase siguiente ("FachwerkUpdated to..."): las convertimos en viñetas en negrita.
    text = re.sub(
        r"(\w+)(Updated to the latest version from the Community Workshop\s*(?:\(Update Notes\))?)",
        r"\n• **\1** — \2",
        text,
    )
    # Encabezado de sección para el bloque de mapas: es lo único que podemos detectar
    # de forma fiable (Steam no expone categorías tipo Jugabilidad/Miscelánea en este
    # endpoint, ni siquiera en inglés).
    text = re.sub(r"\n• \*\*", "\n\n🗺️ **Mapas**\n• **", text, count=1)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


_WEEKDAYS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_MONTHS_ES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]

_TAG_LABELS_ES = {"patchnotes": "Notas de parche"}


def _format_steam_date(unix_ts: int) -> str:
    dt = datetime.fromtimestamp(unix_ts, tz=ZoneInfo("Europe/Madrid"))
    weekday = _WEEKDAYS_ES[dt.weekday()]
    month = _MONTHS_ES[dt.month - 1]
    return f"{weekday}, {dt.day} de {month} de {dt.year} a las {dt.hour}:{dt.minute:02d} {dt.strftime('%Z')}"


def _format_tags_es(tags) -> str:
    if not tags:
        return "Actualización"
    return ", ".join(_TAG_LABELS_ES.get(t, t) for t in tags)


async def _translate_to_spanish(text: str) -> str:
    """Traduce EN->ES con el endpoint gratuito de Google Translate (sin API key).
    Si falla (endpoint caído, cambia de formato, etc.) se deja el texto en inglés
    en vez de romper la publicación."""
    if not text:
        return text
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://translate.googleapis.com/translate_a/single",
                params={"client": "gtx", "sl": "en", "tl": "es", "dt": "t", "q": text},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()
        return "".join(segment[0] for segment in data[0] if segment[0])
    except Exception as e:
        print(f"[patchnotes] Error traduciendo, se deja en inglés: {e}")
        return text


async def _fetch_latest_steam_news():
    """Devuelve (title, url, contents_limpio, image_url) del último post de Steam para CS2, o None."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/",
                # l=spanish no cambia el contenido de este post en concreto (comprobado:
                # Valve no publica traducción para estos anuncios vía este endpoint),
                # pero se manda por si Valve lo soporta para otros posts. La traducción
                # real la hace _translate_to_spanish más abajo.
                params={"appid": CS2_APPID, "count": 1, "maxlength": 3000, "format": "json", "l": "spanish"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()
    except Exception as e:
        print(f"[patchnotes] Error consultando noticias de Steam: {e}")
        return None

    items = data.get("appnews", {}).get("newsitems", [])
    if not items:
        return None

    latest = items[0]
    title = latest.get("title", "Actualización de CS2")
    url = latest.get("url", "")
    raw_contents = latest.get("contents", "")

    # Si el post trae una imagen propia la usamos; si no, el header oficial de CS2
    # como fallback para que el embed siempre tenga una imagen grande.
    img_match = re.search(r"\[img\](.*?)\[/img\]", raw_contents, flags=re.DOTALL)
    image_url = img_match.group(1).strip() if img_match else CS2_FALLBACK_IMAGE

    # date y tags están en TODOS los posts de este endpoint (no son específicos de
    # ningún parche en particular), así que esto generaliza siempre.
    date_str = _format_steam_date(latest["date"]) if latest.get("date") else "Fecha desconocida"
    type_str = _format_tags_es(latest.get("tags"))

    contents = _clean_steam_markup(raw_contents)
    if len(contents) > 1500:
        contents = contents[:1500].rstrip() + "…"

    # Traducimos ya formateado (Markdown incluido) para que las regex de arriba,
    # que buscan frases concretas en inglés, sigan funcionando.
    title = await _translate_to_spanish(title)
    contents = await _translate_to_spanish(contents)

    return title, url, contents, image_url, date_str, type_str


@tasks.loop(minutes=PATCHNOTES_CHECK_MINUTES)
async def check_cs2_patchnotes():
    current_buildid = await asyncio.to_thread(_get_installed_buildid)
    if current_buildid is None:
        return  # server caído, kubectl falló, etc.: reintentamos en el próximo ciclo

    last_buildid = _load_last_buildid()

    if last_buildid is None:
        # Primera vez que corre esto: fijamos la base, no publicamos el
        # build actual como si fuera un release nuevo recién salido.
        _save_last_buildid(current_buildid)
        print(f"[patchnotes] Base de buildid fijada en {current_buildid}, no se publica nada todavía")
        return

    if current_buildid == last_buildid:
        return  # sin cambios reales: nada que anunciar

    # Hubo un release real (buildid cambiado). IMPORTANTE: current_buildid NO se
    # marca como "visto" aquí, solo al final si se publica con éxito. Si algo falla
    # a mitad (Steam no responde, canal no encontrado, Discord caído...) el próximo
    # ciclo (30 min) reintenta desde cero solo, sin que nadie tenga que tocar nada.
    channel = bot.get_channel(PATCHNOTES_CHANNEL_ID)
    if channel is None:
        print(f"[patchnotes] No encuentro el canal {PATCHNOTES_CHANNEL_ID}, reintento en el próximo ciclo")
        return

    patch = await _fetch_latest_steam_news()
    if patch is None:
        print(f"[patchnotes] Release detectado (build {current_buildid}) pero no pude obtener las notas de Steam, reintento en el próximo ciclo")
        return

    title, url, contents, image_url, date_str, type_str = patch
    embed = discord.Embed(
        title=f"📋 {title}",
        description=contents or "(sin descripción)",
        url=url,
        color=discord.Color.orange(),
    )
    embed.set_image(url=image_url)
    embed.add_field(name="📅 Publicado", value=date_str, inline=True)
    embed.add_field(name="🏷️ Tipo", value=type_str, inline=True)
    embed.set_footer(text=f"Counter-Strike 2 — build {current_buildid}")

    try:
        # Borramos el anuncio del release anterior antes de publicar el nuevo, para
        # que el canal no vaya acumulando una nota de parche detrás de otra.
        previous_id = _load_last_message_id()
        if previous_id is not None:
            try:
                old_message = await channel.fetch_message(previous_id)
                await old_message.delete()
            except discord.NotFound:
                pass  # ya no existe (lo borraron a mano, etc.): no pasa nada

        sent = await channel.send(embed=embed)
        _save_last_message_id(sent.id)
        _save_last_buildid(current_buildid)  # solo AHORA que se publicó con éxito
        print(f"[patchnotes] Publicado release {current_buildid}: {title}")
    except Exception as e:
        print(f"[patchnotes] Error publicando en Discord (build {current_buildid}), reintento en el próximo ciclo: {e}")


@check_cs2_patchnotes.before_loop
async def before_check_cs2_patchnotes():
    await bot.wait_until_ready()


# ---------- Eventos ----------

@bot.event
async def on_ready():
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    else:
        await bot.tree.sync()
    if not check_cs2_patchnotes.is_running():
        check_cs2_patchnotes.start()
    print(f"Conectado como {bot.user} | Ranking API: {RANKING_API_URL}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mention = bot.user in message.mentions if bot.user else False

    if not (is_dm or is_mention):
        return

    if mention_on_cooldown(message.author.id):
        return  # ignoramos en silencio para no generar más spam todavía

    try:
        await message.reply(embed=build_help_embed(), mention_author=False)
    except discord.HTTPException:
        # Algunos DMs no permiten "reply" -> fallback a mensaje normal
        await message.channel.send(embed=build_help_embed())


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        await interaction.response.send_message(
            f"⏳ Espera {error.retry_after:.1f}s antes de repetir este comando.",
            ephemeral=True,
        )
    else:
        try:
            await interaction.response.send_message(
                f"Ocurrió un error inesperado: `{error}`", ephemeral=True
            )
        except discord.InteractionResponded:
            pass


# ---------- Comandos ----------

@bot.tree.command(name="help", description="Muestra todos los comandos disponibles del bot (siempre en privado)")
async def help_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(embed=build_help_embed(), ephemeral=True)


@bot.tree.command(name="top", description="Ranking global de jugadores por puntos (top 10)")
@app_commands.checks.cooldown(1, COOLDOWN_SECONDS)
async def top(interaction: discord.Interaction):
    ephemeral = resolve_ephemeral(interaction)
    if ephemeral is None:
        await interaction.response.send_message(
            "Este comando no está disponible en este canal.", ephemeral=True
        )
        return

    try:
        rows = await fetch_global_top(DEFAULT_TOP_N)
    except Exception as e:
        await interaction.response.send_message(f"Error leyendo la base: `{e}`", ephemeral=True)
        return

    if not rows:
        await interaction.response.send_message("No hay datos todavía.", ephemeral=ephemeral)
        return

    embed = discord.Embed(
        title="🏆 Ranking Global - SharpTimer",
        color=discord.Color.gold(),
    )
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (steamid, name, points) in enumerate(rows, start=1):
        prefix = medals[i - 1] if i <= 3 else f"`#{i}`"
        lines.append(f"{prefix} **{name or 'Desconocido'}** — {int(points)} pts")
    embed.description = "\n".join(lines)
    embed.set_footer(text=f"Mostrando top {len(rows)} | Puntos por posición relativa en cada mapa")
    await interaction.response.send_message(embed=embed, ephemeral=ephemeral)


@bot.tree.command(name="maptop", description="Top 10 tiempos de un mapa")
@app_commands.describe(mapa="Nombre exacto del mapa")
@app_commands.checks.cooldown(1, COOLDOWN_SECONDS)
async def maptop(interaction: discord.Interaction, mapa: str):
    ephemeral = resolve_ephemeral(interaction)
    if ephemeral is None:
        await interaction.response.send_message(
            "Este comando no está disponible en este canal.", ephemeral=True
        )
        return

    try:
        rows = await fetch_map_top(mapa, DEFAULT_TOP_N)
    except Exception as e:
        await interaction.response.send_message(f"Error leyendo la base: `{e}`", ephemeral=True)
        return

    if not rows:
        maps = [m[0] for m in await list_known_maps_with_counts()]
        sugerencia = ""
        if maps:
            cercanos = [m for m in maps if mapa.lower() in m.lower()]
            if cercanos:
                sugerencia = f"\n¿Quizás quisiste decir: {', '.join(cercanos[:5])}?"
        await interaction.response.send_message(
            f"No encontré records para el mapa `{mapa}`.{sugerencia}", ephemeral=ephemeral
        )
        return

    embed = discord.Embed(
        title=f"⏱️ Top tiempos - {mapa}",
        color=discord.Color.blue(),
    )
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (name, formatted_time, finishes) in enumerate(rows, start=1):
        prefix = medals[i - 1] if i <= 3 else f"`#{i}`"
        lines.append(f"{prefix} **{name or 'Desconocido'}** — {formatted_time} ({finishes} runs)")
    embed.description = "\n".join(lines)
    await interaction.response.send_message(embed=embed, ephemeral=ephemeral)


@bot.tree.command(name="pb", description="Consulta tu (o de otro jugador) mejor tiempo en un mapa")
@app_commands.describe(mapa="Nombre exacto del mapa", jugador="Nombre o SteamID (déjalo vacío para buscar tu nombre de Discord)")
@app_commands.checks.cooldown(1, COOLDOWN_SECONDS)
async def pb(interaction: discord.Interaction, mapa: str, jugador: str = None):
    ephemeral = resolve_ephemeral(interaction)
    if ephemeral is None:
        await interaction.response.send_message(
            "Este comando no está disponible en este canal.", ephemeral=True
        )
        return

    busqueda = jugador or interaction.user.display_name

    try:
        rows = await fetch_player_pb_on_map(mapa, busqueda)
    except Exception as e:
        await interaction.response.send_message(f"Error leyendo la base: `{e}`", ephemeral=True)
        return

    if not rows:
        await interaction.response.send_message(
            f"No encontré ningún tiempo de `{busqueda}` en el mapa `{mapa}`.", ephemeral=ephemeral
        )
        return

    if len(rows) == 1:
        name, steamid, formatted_time, finishes, position, total = rows[0]
        await interaction.response.send_message(
            f"**{name}** en `{mapa}`: **{formatted_time}** — posición **#{position}/{total}** ({finishes} runs)",
            ephemeral=ephemeral,
        )
        return

    lines = [
        f"`#{position}/{total}` **{name}** — {formatted_time}"
        for (name, steamid, formatted_time, finishes, position, total) in rows
    ]
    await interaction.response.send_message(
        f"Encontré {len(rows)} jugadores que coinciden con `{busqueda}` en `{mapa}`, sé más específico:\n"
        + "\n".join(lines),
        ephemeral=ephemeral,
    )


@bot.tree.command(name="rank", description="Consulta el ranking de un jugador por nombre o SteamID")
@app_commands.describe(jugador="Nombre (parcial) o SteamID64 del jugador")
@app_commands.checks.cooldown(1, COOLDOWN_SECONDS)
async def rank(interaction: discord.Interaction, jugador: str):
    ephemeral = resolve_ephemeral(interaction)
    if ephemeral is None:
        await interaction.response.send_message(
            "Este comando no está disponible en este canal.", ephemeral=True
        )
        return

    try:
        rows = await fetch_player_rank(jugador)
    except Exception as e:
        await interaction.response.send_message(f"Error leyendo la base: `{e}`", ephemeral=True)
        return

    if not rows:
        await interaction.response.send_message(
            f"No encontré a ningún jugador que coincida con `{jugador}`.", ephemeral=ephemeral
        )
        return

    if len(rows) == 1:
        name, points, steamid, position = rows[0]
        await interaction.response.send_message(
            f"**{name}** está en la posición **#{position}** con **{int(points)} puntos**.",
            ephemeral=ephemeral,
        )
        return

    lines = [
        f"`#{position}` **{name}** — {int(points)} pts"
        for (name, points, steamid, position) in rows
    ]
    await interaction.response.send_message(
        f"Encontré {len(rows)} jugadores que coinciden con `{jugador}`, sé más específico:\n"
        + "\n".join(lines),
        ephemeral=ephemeral,
    )


@bot.tree.command(name="maps", description="Lista todos los mapas con records guardados")
@app_commands.checks.cooldown(1, COOLDOWN_SECONDS)
async def maps_cmd(interaction: discord.Interaction):
    ephemeral = resolve_ephemeral(interaction)
    if ephemeral is None:
        await interaction.response.send_message(
            "Este comando no está disponible en este canal.", ephemeral=True
        )
        return

    try:
        rows = await list_known_maps_with_counts()
    except Exception as e:
        await interaction.response.send_message(f"Error leyendo la base: `{e}`", ephemeral=True)
        return

    if not rows:
        await interaction.response.send_message("No hay mapas con records todavía.", ephemeral=ephemeral)
        return

    lines = [f"**{name}** — {count} jugador{'es' if count != 1 else ''}" for name, count in rows]

    chunks = []
    current = []
    current_len = 0
    for line in lines:
        if current_len + len(line) + 1 > 3800:
            chunks.append(current)
            current = []
            current_len = 0
        current.append(line)
        current_len += len(line) + 1
    if current:
        chunks.append(current)

    for i, chunk in enumerate(chunks, start=1):
        embed = discord.Embed(
            title=f"🗺️ Mapas con records ({len(rows)} en total)"
            + (f" — parte {i}/{len(chunks)}" if len(chunks) > 1 else ""),
            description="\n".join(chunk),
            color=discord.Color.green(),
        )
        if i == 1:
            await interaction.response.send_message(embed=embed, ephemeral=ephemeral)
        else:
            await interaction.followup.send(embed=embed, ephemeral=ephemeral)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
