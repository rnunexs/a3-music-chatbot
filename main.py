import asyncio
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Any
import uuid
import davey
import discord
from discord.ext import commands
from google import genai
from google.genai import types
from dotenv import load_dotenv
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import yt_dlp
import random
from discord import app_commands

load_dotenv()

ARQUIVO_AGENTE = Path(__file__).parent / "agente.txt"
HISTORICO = Path(__file__).parent / "historico.json"
LIMITE = 200  # mensagens guardadas por canal (use número par)

intents = discord.Intents.all()
bot = commands.Bot("!", intents=intents, help_command=None)

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
MODELO = "gemini-3.5-flash-lite"

carregando = set()

sp = None
if os.getenv("SPOTIFY_CLIENT_ID") and os.getenv("SPOTIFY_CLIENT_SECRET"):
    auth_sp = SpotifyOAuth(
        client_id=os.getenv("SPOTIFY_CLIENT_ID"),
        client_secret=os.getenv("SPOTIFY_CLIENT_SECRET"),
        redirect_uri="http://127.0.0.1:8888/callback",
        scope="playlist-read-private playlist-read-collaborative",
    )
    auth_sp.get_access_token(as_dict=False)
    sp = spotipy.Spotify(auth_manager=auth_sp)
SPOTIFY_REGEX = re.compile(
    r"open\.spotify\.com/(?:intl-[a-z]+/)?(track|playlist|album)/([a-zA-Z0-9]+)"
)



# ---------- HISTÓRICO ----------



def carregar():
    if HISTORICO.exists():
        return json.loads(HISTORICO.read_text(encoding="utf-8"))
    return {}


def salvar():
    HISTORICO.write_text(json.dumps(memoria, ensure_ascii=False), encoding="utf-8")


memoria = carregar()


@bot.event
async def on_ready():
    await bot.tree.sync()
    print("Musik inicializado com sucesso")

# ---------- IA ----------

@bot.hybrid_command(name="ask", description="Conversa com a IA ou pede para ela controlar a música")
@app_commands.describe(pergunta="O que você quer perguntar ou pedir para o bot fazer")
async def ask(ctx, *, pergunta: str):
    await ctx.defer()
    agente = ARQUIVO_AGENTE.read_text(encoding="utf-8")
    cid = str(ctx.channel.id)
    msgs = memoria.get(cid, [])

    pergunta_com_nome = f"[{ctx.author.display_name}]: {pergunta}"

    chat = client.aio.chats.create(
        model=MODELO,
        config=types.GenerateContentConfig(
            system_instruction=agente,
            tools=FERRAMENTAS_BOT,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        ),
        history=[
            types.Content(role=m["role"], parts=[types.Part(text=m["text"])])
            for m in msgs
        ],
    )

    try:
        resposta = await chat.send_message(pergunta_com_nome)

        # Ia pedindo pro bot

        print("DEBUG function_calls:", resposta.function_calls)
        if resposta.function_calls:
            respostas_tools = []
            for call in resposta.function_calls:
                resultado = await executar_ferramenta(ctx, call.name, dict(call.args or {}))
                respostas_tools.append(
                    types.Part.from_function_response(
                        name=call.name,
                        response={"resultado": resultado},
                    )
                )
            # Devolve o resultado para o Gemini gerar uma resposta natural
            resposta = await chat.send_message(respostas_tools)

        texto = (resposta.text or "").strip()
    except Exception as e:
        print(f"Erro Gemini: {e}")
        await ctx.send("Deu erro ao falar com o Gemini, tenta de novo daqui a pouco.")
        return

    if not texto:
        return

    msgs += [{"role": "user", "text": pergunta_com_nome}, {"role": "model", "text": texto}]
    memoria[cid] = msgs[-LIMITE:]
    salvar()

    for i in range(0, len(texto), 2000):
        await ctx.send(texto[i:i + 2000])


@bot.command()
async def reset(ctx):
    memoria.pop(str(ctx.channel.id), None)
    salvar()
    await ctx.send("Histórico apagado.")


# ---------- MÚSICA ----------

LIMITE_PLAYLIST = 200

YDL_OPTS: Any = {
    "format": "bestaudio/best",
    "noplaylist": True,            # <-- Deixe True!
    "extract_flat": "in_playlist",
    "quiet": True,
    "default_search": "ytsearch",
}


filas = {}
tocando_agora = {}
historico_musicas = {}
autoplay_ativo = set()
autoshuffle_ativo = set()
proxima_forcada = {}
ignorar_historico = set()


def extrair_spotify(url):
    if not sp:
        raise RuntimeError("Chaves do Spotify não configuradas no .env")
    match = SPOTIFY_REGEX.search(url)
    if not match:
        return []

    tipo, sp_id = match.groups()
    musicas = []

    if tipo == "track":
        t = sp.track(sp_id)
        artistas = ", ".join(a["name"] for a in t["artists"])
        titulo = f"{artistas} - {t['name']}"
        link = t["external_urls"]["spotify"]
        musicas.append({
            "titulo": titulo,
            "pagina": link,
            "download": f"ytsearch1:{titulo}",
        })

    elif tipo == "album":
        resultados = sp.album_tracks(sp_id, limit=50)
        itens = resultados["items"]
        while resultados["next"]:
            resultados = sp.next(resultados)
            itens.extend(resultados["items"])

        for t in itens:
            artistas = ", ".join(a["name"] for a in t.get("artists", []))
            titulo = f"{artistas} - {t['name']}"
            link = t.get("external_urls", {}).get("spotify", url)
            musicas.append({
                "titulo": titulo,
                "pagina": link,
                "download": f"ytsearch1:{titulo}",
            })

    elif tipo == "playlist":
        resultados = sp.playlist_items(sp_id, limit=50, additional_types=("track",))
        itens = resultados["items"]
        # Continua buscando até o final da playlist (seja 100, 800 ou mais músicas)
        while resultados["next"]:
            resultados = sp.next(resultados)
            itens.extend(resultados["items"])

        for item in itens:
            t = item.get("track") or item.get("item")
            if not t or not t.get("name"):
                continue
            artistas = ", ".join(a["name"] for a in t.get("artists", []))
            titulo = f"{artistas} - {t['name']}"
            link = t.get("external_urls", {}).get("spotify", url)
            musicas.append({
                "titulo": titulo,
                "pagina": link,
                "download": f"ytsearch1:{titulo}",
            })

    return musicas


def extrair(consulta):
    if "open.spotify.com" in consulta:
        return extrair_spotify(consulta)

    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        info: Any = ydl.extract_info(consulta, download=False)
        if "entries" in info:
            entradas = list(info["entries"])
        else:
            entradas = [info]

        musicas = []
        for item in entradas:
            if not item:
                continue

            pagina = item.get("webpage_url") or item.get("url")
            if not pagina:
                continue

            titulo = item.get("title")

            if not titulo and "soundcloud.com" in pagina:
                partes = pagina.split("?")[0].rstrip("/").split("/")
                if "api" not in pagina and len(partes) >= 2:
                    titulo = f"{partes[-2]} - {partes[-1]}".replace("-", " ").title()
                else:
                    titulo = f"SoundCloud Track #{item.get('id', '')}"

            if not titulo or titulo in ("[Private video]", "[Deleted video]"):
                continue

            if not pagina.startswith("http"):
                pagina = f"https://www.youtube.com/watch?v={pagina}"

            musicas.append({
                "titulo": titulo,
                "pagina": pagina,
                "download": pagina,
            })

        return musicas


PASTA = Path(__file__).parent / "temp"
PASTA.mkdir(exist_ok=True)


def baixar(alvo):
    uid = uuid.uuid4().hex[:8]
    opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "default_search": "ytsearch",
        "outtmpl": str(PASTA / f"{uid}_%(id)s.%(ext)s"),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(alvo, download=True)
        if "entries" in info:
            info = info["entries"][0]
        return ydl.prepare_filename(info)


FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")


async def tocar_proxima(ctx):
    gid = ctx.guild.id
    if gid in carregando:
        return
    if not ctx.voice_client:
        tocando_agora.pop(gid, None)
        return

    fila = filas.get(gid, [])
    veio_do_autoplay = False

    if gid in proxima_forcada:
        musica = proxima_forcada.pop(gid)
    elif fila:
        musica = fila.pop(0)
    elif gid in autoplay_ativo:
        carregando.add(gid)
        musica = await recomendar_musica(gid)
        carregando.discard(gid)
        if not musica:
            tocando_agora.pop(gid, None)
            await ctx.channel.send("Não consegui recomendar a próxima música no autoplay.")
            return
        veio_do_autoplay = True
    else:
        tocando_agora.pop(gid, None)
        return

    carregando.add(gid)
    loop = asyncio.get_running_loop()
    try:
        alvo = musica.get("download", musica["pagina"])
        arquivo = await loop.run_in_executor(None, baixar, alvo)
    except Exception as e:
        carregando.discard(gid)
        print(f"Erro yt-dlp: {e}")
        await ctx.channel.send(f"Não consegui tocar **{musica['titulo']}**, pulando.")
        await tocar_proxima(ctx)
        return
    carregando.discard(gid)

    if not ctx.voice_client:
        try:
            os.remove(arquivo)
        except OSError:
            pass
        return

    fonte = discord.FFmpegPCMAudio(arquivo, executable=FFMPEG_PATH, options="-vn")

    def depois(erro):
        if erro:
            print(f"Erro no áudio: {erro}")
        try:
            os.remove(arquivo)
        except OSError:
            pass
        if gid in ignorar_historico:
            ignorar_historico.discard(gid)
        else:
            historico_musicas.setdefault(gid, []).append(musica)
        asyncio.run_coroutine_threadsafe(tocar_proxima(ctx), loop)

    tocando_agora[gid] = musica
    ctx.voice_client.play(fonte, after=depois)
    prefixo = "🤖 Autoplay tocando" if veio_do_autoplay else "▶️ Tocando"
    await ctx.channel.send(f"{prefixo}: **{musica['titulo']}**")

@bot.hybrid_command()
@commands.guild_only()
async def play(ctx, *, busca: str):
    if ctx.interaction and not ctx.interaction.response.is_done():
        await ctx.defer()
    if not ctx.author.voice:
        await ctx.send("Entra em um canal de voz primeiro.")
        return
    canal = ctx.author.voice.channel
    if ctx.voice_client is None:
        await canal.connect()
    elif ctx.voice_client.channel != canal:
        await ctx.voice_client.move_to(canal)

    async with ctx.typing():
        try:
            loop = asyncio.get_running_loop()
            musicas = await loop.run_in_executor(None, extrair, busca)
        except Exception as e:
            print(f"Erro na extração: {e}")
            await ctx.send("Não consegui achar essa música ou playlist.")
            return

    if not musicas:
        await ctx.send("Nenhuma música válida encontrada.")
        return

    if not musicas:
        await ctx.send("Nenhuma música válida encontrada.")
        return

    filas.setdefault(ctx.guild.id, []).extend(musicas)
    if ctx.guild.id in autoshuffle_ativo:
        random.shuffle(filas[ctx.guild.id])
    ocupado = ctx.voice_client.is_playing() or ctx.voice_client.is_paused() or ctx.guild.id in carregando

    if len(musicas) > 1:
        await ctx.send(f"Adicionadas **{len(musicas)} músicas** da playlist à fila!")
        if not ocupado:
            await tocar_proxima(ctx)
    else:
        if ocupado:
            await ctx.send(f"Adicionado à fila: **{musicas[0]['titulo']}**")
        else:
            await tocar_proxima(ctx)

@bot.command(name="current-song", aliases=["now-playing", "atual", "np"])
@commands.guild_only()
async def current_song(ctx):
    musica = tocando_agora.get(ctx.guild.id)
    if not ctx.voice_client or not (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()) or not musica:
        await ctx.send("Não há música tocando neste momento")
        return

    status = "⏸️ Pausada" if ctx.voice_client.is_paused() else "▶️ Tocando agora"
    await ctx.send(f"{status}: **{musica['titulo']}**\n\n{musica['pagina']}")

class FilaView(discord.ui.View):

    def __init__(self, guild_id: int, pagina: int = 1):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.pagina = pagina
        self.itens_por_pagina = 10
        self.atualizar_botoes()

    def total_paginas(self) -> int:
        fila = filas.get(self.guild_id, [])
        return max(1, math.ceil(len(fila) / self.itens_por_pagina))

    def atualizar_botoes(self):
        total = self.total_paginas()
        self.pagina = max(1, min(self.pagina, total))
        self.btn_anterior.disabled = self.pagina <= 1
        self.btn_proxima.disabled = self.pagina >= total

    def criar_embed(self) -> discord.Embed:
        atual = tocando_agora.get(self.guild_id)
        fila = filas.get(self.guild_id, [])
        total = self.total_paginas()
        self.atualizar_botoes()
        partes = []
        if atual:
            partes.append(f"**▶️ Tocando agora:**\n[{atual['titulo']}]({atual['pagina']})\n")
        if fila:
            inicio = (self.pagina - 1) * self.itens_por_pagina
            fim = inicio + self.itens_por_pagina
            linhas = []
            for i, m in enumerate(fila[inicio:fim], start=inicio + 1):
                # Corta títulos muito grandes e remove colchetes que quebram o link
                titulo = m["titulo"].replace("[", "(").replace("]", ")")
                if len(titulo) > 60:
                    titulo = titulo[:57] + "..."
                linhas.append(f"**{i}.** [{titulo}]({m['pagina']})")
            partes.append(f"**📋 Próximas na fila ({len(fila)} no total):**\n" + "\n".join(linhas))
        else:
            partes.append("**📋 Próximas na fila:**\nNenhuma música na fila.")
        embed = discord.Embed(
            title="🎵 Fila de Músicas",
            description="\n".join(partes),
            color=discord.Color.from_str("#1C5FF1"),
        )
        if fila:
            embed.set_footer(text=f"Página {self.pagina}/{total}")
        return embed

    @discord.ui.button(label="Anterior", style=discord.ButtonStyle.secondary)
    async def btn_anterior(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.pagina -= 1
        embed = self.criar_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Próxima", style=discord.ButtonStyle.secondary)
    async def btn_proxima(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.pagina += 1
        embed = self.criar_embed()
        await interaction.response.edit_message(embed=embed, view=self)

@bot.hybrid_command(aliases=["fila", "q"])
@commands.guild_only()
async def queue(ctx, pagina: int = 1):
    atual = tocando_agora.get(ctx.guild.id)
    fila = filas.get(ctx.guild.id, [])
    if not atual and not fila:
        await ctx.send("A fila está vazia.")
        return
    view = FilaView(ctx.guild.id, pagina)
    embed = view.criar_embed()
    # Só mostra os botões se houver mais de 1 página
    if view.total_paginas() > 1:
        await ctx.send(embed=embed, view=view)
    else:
        await ctx.send(embed=embed)

@bot.hybrid_command()
@commands.guild_only()
async def skip(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()  # o "after" chama a próxima sozinho
        await ctx.send("Pulei.")

@bot.hybrid_command()
@commands.guild_only()
async def pause(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("Música pausada.")
    elif ctx.voice_client and ctx.voice_client.is_paused():
        await ctx.send("A música já está pausada.")
    else:
        await ctx.send("Não há nada tocando no momento.")

@bot.hybrid_command()
@commands.guild_only()
async def resume(ctx):
    vc = ctx.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await ctx.send("Música retomada.")
    else:
        await ctx.send("A música não está pausada.")

@bot.hybrid_command()
@commands.guild_only()
async def stop(ctx):
    filas[ctx.guild.id] = []
    if ctx.voice_client:
        ctx.voice_client.stop()
    await ctx.send("Parei e Limpei a fila!")

@bot.hybrid_command()
@commands.guild_only()
async def leave(ctx):
    if not ctx.voice_client:
        await ctx.send("Não estou em nenhum canal de voz.")
        return
    filas[ctx.guild.id] = []
    tocando_agora.pop(ctx.guild.id, None)
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.voice_client.disconnect()
        await ctx.send("Parei de tocar e saí do canal.")
    else:
        await ctx.voice_client.disconnect()
        await  ctx.send("Saí do canal.")

@bot.hybrid_command()
@commands.guild_only()
async def beep(ctx):
    if not ctx.author.voice:
        await ctx.send("Entra em um canal de voz primeiro.")
        return
    if ctx.voice_client is None:
        await ctx.author.voice.channel.connect()
    elif ctx.voice_client.channel != ctx.author.voice.channel:
        await ctx.voice_client.move_to(ctx.author.voice.channel)
    vc = ctx.voice_client
    conn = vc._connection
    s = conn.dave_session
    print(f"Iniciando beep | davey={davey.__version__} | mode={vc.mode} | dave_ver={conn.dave_protocol_version} | can_encrypt={conn.can_encrypt}")
    if s:
        print(f" Antes -> status={s.status} | ready={s.ready} | epoch={s.epoch}")
    fonte = discord.FFmpegPCMAudio(
        "sine=frequency=440:duration=5",
        executable=FFMPEG_PATH,
        before_options="-f lavfi",
        options="-vn",
        stderr=sys.stderr,
    )
    def fim(e):
        s2 = conn.dave_session
        print(f"Fim do beep | erro={e} | can_encrypt={conn.can_encrypt} | status={getattr(s2, 'status', None)} | ready={getattr(s2, 'ready', None)} | stats={s2.get_encryption_stats().successes if s2 else None}")
    vc.play(fonte, after=fim)

@bot.hybrid_command(aliases=["rm", "remover"], description="Remove uma música da fila pelo número")
@commands.guild_only()
async def remove(ctx, numero: int):
    fila = filas.get(ctx.guild.id, [])
    if not fila:
        await ctx.send("A fila está vazia.")
        return
    if numero < 1 or numero > len(fila):
        await ctx.send(f"Número inválido. Escolha entre 1 e {len(fila)}.")
        return

    removida = fila.pop(numero - 1)
    await ctx.send(f"🗑️ Removida da fila (#{numero}): **{removida['titulo']}**")

@bot.hybrid_command(description="Ativa ou desativa a ordem aleatória da fila")
@commands.guild_only()
async def autoshuffle(ctx):
    gid = ctx.guild.id
    if gid in autoshuffle_ativo:
        autoshuffle_ativo.discard(gid)
        await ctx.send("🔀 Auto-shuffle **desativado**.")
    else:
        autoshuffle_ativo.add(gid)
        fila = filas.get(gid)
        if fila:
            random.shuffle(fila)
        await ctx.send("🔀 Auto-shuffle **ativado** e fila embaralhada!")

async def recomendar_musica(gid):
    hist = historico_musicas.get(gid, [])
    if not hist:
        return None

    ultimas = [m["titulo"] for m in hist[-10:]]
    prompt = (
        "Com base nestas músicas, recomende APENAS UMA música parecida que NÃO esteja na lista.\n"
        "Responda SOMENTE no formato: Artista - Nome da Música\n\n"
        + "\n".join(ultimas)
    )
    try:
        resp = await client.aio.models.generate_content(model=MODELO, contents=prompt)
        sugestao = (resp.text or "").strip().splitlines()[0].strip(" -*\"'")
        if not sugestao:
            return None
        loop = asyncio.get_running_loop()
        encontradas = await loop.run_in_executor(None, extrair, f"ytsearch1:{sugestao}")
        return encontradas[0] if encontradas else None
    except Exception as e:
        print(f"Erro no autoplay: {e}")
        return None


@bot.hybrid_command(description="Ativa ou desativa músicas recomendadas quando a fila acabar")
@commands.guild_only()
async def autoplay(ctx):
    gid = ctx.guild.id
    if gid in autoplay_ativo:
        autoplay_ativo.discard(gid)
        await ctx.send("🤖 Autoplay **desativado**.")
    else:
        autoplay_ativo.add(gid)
        await ctx.send("🤖 Autoplay **ativado**!")

@bot.hybrid_command(aliases=["prev", "anterior"], description="Toca a música anterior")
@commands.guild_only()
async def previous(ctx):
    if ctx.interaction and not ctx.interaction.response.is_done():
        await ctx.defer()
    gid = ctx.guild.id
    hist = historico_musicas.get(gid)
    if not hist:
        await ctx.send("Não há nenhuma música anterior no histórico.")
        return

    if not ctx.author.voice:
        await ctx.send("Entra em um canal de voz primeiro.")
        return

    anterior = hist.pop()
    atual = tocando_agora.get(gid)
    if atual:
        filas.setdefault(gid, []).insert(0, atual)

    proxima_forcada[gid] = anterior
    await ctx.send(f"⏮️ Voltando para: **{anterior['titulo']}**")

    if ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
        ignorar_historico.add(gid)
        ctx.voice_client.stop()  # o "after" chama tocar_proxima automaticamente
    else:
        await tocar_proxima(ctx)

INFO_COMANDOS = {
    "play": ("Toca uma música ou playlist.", "/play <nome ou link>"),
    "pause": ("Pausa a música atual.", "/pause"),
    "resume": ("Retoma a música pausada.", "/resume"),
    "skip": ("Pula para a próxima música.", "/skip"),
    "previous": ("Volta para a música anterior.", "/previous"),
    "remove": ("Remove uma música da fila pelo número.", "/remove <numero>"),
    "queue": ("Mostra a fila de músicas.", "/queue [pagina]"),
    "current-song": ("Mostra a música tocando agora.", "/current-song"),
    "autoplay": ("Liga/desliga músicas recomendadas ao fim da fila.", "/autoplay"),
    "autoshuffle": ("Liga/desliga a ordem aleatória da fila.", "/autoshuffle"),
    "stop": ("Para a música e limpa a fila.", "/stop"),
    "leave": ("Desconecta o bot do canal de voz.", "/leave"),
    "ask": ("Faz uma pergunta para a IA.", "/ask <pergunta>"),
    "reset": ("Apaga o histórico da IA no canal.", "/reset"),
    "commands": ("Lista todos os comandos.", "/commands"),
    "help": ("Explica como usar um comando.", "/help <comando>"),
}

@bot.hybrid_command(name="commands", aliases=["comandos"], description="Lista todos os comandos do bot")
async def lista_comandos(ctx):
    linhas = [f"`/{nome}` — {desc}" for nome, (desc, _) in INFO_COMANDOS.items()]
    embed = discord.Embed(
        title="📜 Comandos do Musik",
        description="\n".join(linhas),
        color=discord.Color.from_str("#1C5FF1"),
    )
    embed.set_footer(text="Use /help <comando> para ver como usar.")
    await ctx.send(embed=embed)

@bot.hybrid_command(name="help", aliases=["ajuda"], description="Mostra como usar um comando específico")
@app_commands.describe(comando="Nome do comando (ex: play)")
async def help_cmd(ctx, comando: str = None):
    if not comando:
        await ctx.send("Use `/help <comando>` (ex: `/help play`) ou `/commands` para ver a lista.")
        return

    chave = comando.lower().lstrip("/!")
    cmd_obj = bot.get_command(chave)
    nome_real = cmd_obj.name if cmd_obj else chave

    if nome_real not in INFO_COMANDOS:
        await ctx.send(f"Comando `{comando}` não encontrado. Use `/commands` para ver a lista.")
        return

    desc, uso = INFO_COMANDOS[nome_real]
    embed = discord.Embed(
        title=f"❓ /{nome_real}",
        description=f"{desc}\n\n**Uso:** `{uso}`",
        color=discord.Color.from_str("#1C5FF1"),
    )
    await ctx.send(embed=embed)

    # -------------------- Ferramentas da IA --------------------

def cmd_play(busca: str):
    """Toca uma música ou playlist no canal de voz pelo nome ou link."""

def cmd_pause():
    """Pausa a música que está tocando."""

def cmd_resume():
    """Retoma a música pausada."""

def cmd_skip():
    """Pula para a próxima música da fila."""

def cmd_previous():
    """Volta para a música anterior."""

def cmd_stop():
    """Para a reprodução e limpa a fila."""

def cmd_leave():
    """Desconecta o bot do canal de voz. Use sempre que o usuário pedir para sair da call, sair do canal, desconectar, vazar ou ir embora."""

def cmd_queue(pagina: int = 1):
    """Mostra a fila de músicas atual."""

def cmd_current_song():
    """Mostra qual música está tocando agora."""

def cmd_remove(numero: int):
    """Remove uma música da fila pelo número dela."""

def cmd_autoplay():
    """Ativa ou desativa o autoplay (recomendações automáticas)."""

def cmd_autoshuffle():
    """Ativa ou desativa o modo aleatório da fila."""


FERRAMENTAS_BOT = [
    cmd_play, cmd_pause, cmd_resume, cmd_skip, cmd_previous,
    cmd_stop, cmd_leave, cmd_queue, cmd_current_song,
    cmd_remove, cmd_autoplay, cmd_autoshuffle,
]

async def executar_ferramenta(ctx, nome: str, args: dict) -> str:
    if not ctx.guild:
        return "Comandos de música só funcionam dentro de um servidor."

    mapa = {
        "cmd_play": lambda: ctx.invoke(play, busca=args["busca"]),
        "cmd_pause": lambda: ctx.invoke(pause),
        "cmd_resume": lambda: ctx.invoke(resume),
        "cmd_skip": lambda: ctx.invoke(skip),
        "cmd_previous": lambda: ctx.invoke(previous),
        "cmd_stop": lambda: ctx.invoke(stop),
        "cmd_leave": lambda: ctx.invoke(leave),
        "cmd_queue": lambda: ctx.invoke(queue, pagina=int(args.get("pagina", 1))),
        "cmd_current_song": lambda: ctx.invoke(current_song),
        "cmd_remove": lambda: ctx.invoke(remove, numero=int(args["numero"])),
        "cmd_autoplay": lambda: ctx.invoke(autoplay),
        "cmd_autoshuffle": lambda: ctx.invoke(autoshuffle),
    }

    acao = mapa.get(nome)
    if not acao:
        return f"Comando {nome} não encontrado."

    try:
        await acao()
        return f"Comando {nome} executado com sucesso."
    except Exception as e:
        print(f"Erro ao executar {nome} via IA: {e}")
        return f"Erro ao executar {nome}: {e}"


bot.run(os.getenv("DISCORD_TOKEN"))