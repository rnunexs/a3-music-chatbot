import os
import json
import asyncio
import discord
import yt_dlp
from discord.ext import commands
from google import genai
from google.genai import types
from dotenv import load_dotenv
from pathlib import Path
import uuid
import sys
import davey

load_dotenv()

ARQUIVO_AGENTE = Path(__file__).parent / "agente.txt"
HISTORICO = Path(__file__).parent / "historico.json"
LIMITE = 100  # mensagens guardadas por canal (use número par)

intents = discord.Intents.all()
bot = commands.Bot("!", intents=intents)

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
MODELO = "gemini-3.5-flash-lite"

carregando = set()


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
    print("Musik inicializado com sucesso")


# ---------- IA ----------

@bot.command()
async def ask(ctx, *, pergunta: str):
    agente = ARQUIVO_AGENTE.read_text(encoding="utf-8")
    cid = str(ctx.channel.id)
    msgs = memoria.get(cid, [])

    pergunta_com_nome = f"[{ctx.author.display_name}]: {pergunta}"

    chat = client.aio.chats.create(
        model=MODELO,
        config=types.GenerateContentConfig(system_instruction=agente),
        history=[
            types.Content(role=m["role"], parts=[types.Part(text=m["text"])])
            for m in msgs
        ],
    )

    async with ctx.typing():
        try:
            resposta = await chat.send_message(pergunta_com_nome)
            texto = (resposta.text or "").strip()
        except Exception as e:
            print(f"Erro Gemini: {e}")
            await ctx.send("Deu erro ao falar com o Gemini, tenta de novo daqui a pouco.")
            return

    if not texto:
        await ctx.send("O Gemini não retornou resposta.")
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

YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",
}


filas = {}


def extrair(consulta):
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(consulta, download=False)
        if "entries" in info:
            info = info["entries"][0]
        return {
            "titulo": info["title"],
            "pagina": info["webpage_url"],
            "url": info["url"],
            "headers": info.get("http_headers", {}),
        }

PASTA = Path(__file__).parent / "temp"
PASTA.mkdir(exist_ok=True)

def baixar(pagina):
    uid = uuid.uuid4().hex[:8]
    opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "outtmpl": str(PASTA / f"{uid}_%(id)s.%(ext)s"),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(pagina, download=True)
        return ydl.prepare_filename(info)

FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")

async def tocar_proxima(ctx):
    gid = ctx.guild.id
    fila = filas.get(gid)
    vc = ctx.voice_client
    if not fila or not vc or gid in carregando:
        return
    carregando.add(gid)
    musica = fila.pop(0)
    loop = asyncio.get_running_loop()
    try:
        arquivo = await loop.run_in_executor(None, baixar, musica["pagina"])
    except Exception as e:
        carregando.discard(gid)
        print(f"Erro yt-dlp: {e}")
        await ctx.send(f"Não consegui tocar **{musica['titulo']}**, pulando.")
        await tocar_proxima(ctx)
        return
    carregando.discard(gid)
    if not ctx.voice_client:  # In case !stop was called while downloading
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
        asyncio.run_coroutine_threadsafe(tocar_proxima(ctx), loop)
    vc.play(fonte, after=depois)
    await ctx.send(f"Tocando: **{musica['titulo']}**")


@bot.command()
@commands.guild_only()
async def play(ctx, *, busca: str):
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
            info = await loop.run_in_executor(None, extrair, busca)
        except Exception as e:
            print(f"Erro yt-dlp: {e}")
            await ctx.send("Não consegui achar essa música.")
            return

    filas.setdefault(ctx.guild.id, []).append(info)

    vc = ctx.voice_client
    if vc.is_playing() or vc.is_paused() or ctx.guild.id in carregando:
        await ctx.send(f"Adicionado à fila: **{info['titulo']}**")
    else:
        await tocar_proxima(ctx)


@bot.command()
@commands.guild_only()
async def skip(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()  # o "after" chama a próxima sozinho
        await ctx.send("Pulei.")

@bot.command()
@commands.guild_only()
async def pause(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        await ctx.voice_client.pause()
    elif ctx.voice_client and ctx.voice_client.is_paused():
        await ctx.send("A música já está pausada.")
    else:
        await ctx.send("Não há nada tocando no momento.")

@bot.command()
@commands.guild_only()
async def resume(ctx):
    vc = ctx.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await ctx.send("Música retomada.")
    else:
        await ctx.send("A música não está pausada.")

@bot.command()
@commands.guild_only()
async def stop(ctx):
    filas[ctx.guild.id] = []
    if ctx.voice_client:
        ctx.voice_client.stop()
    await ctx.send("Parei e Limpei a fila!")

@bot.command()
@commands.guild_only()
async def leave(ctx):
    filas[ctx.guild.id] = []
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.voice_client.disconnect()
    else:
        await ctx.voice_client.disconnect()
    await ctx.send("Parei e saí do canal.")

@bot.command()
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


bot.run(os.getenv("DISCORD_TOKEN"))