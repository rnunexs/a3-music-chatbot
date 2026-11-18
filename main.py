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
import math

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
    "noplaylist": False,
    "extract_flat": "in_playlist",
    "playlistend": 200,
    "quiet": True,
    "default_search": "ytsearch",
}


filas = {}
tocando_agora = {}


def extrair(consulta):
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(consulta, download=False)
        if "entries" in info:
            entradas = info["entries"]
        else:
            entradas = [info]
        musicas = []
        for item in entradas:
            if not item:
                continue
            titulo = item.get("title")
            if not titulo or titulo in ("[Private video]", "[Deleted video]"):
                continue
            pagina = item.get("webpage_url") or item.get("url")
            if pagina and not pagina.startswith("http"):
                pagina = f"https://www.youtube.com/watch?v={pagina}"
            if pagina:
                musicas.append({
                    "titulo": titulo,
                    "pagina": pagina,
                })
        return musicas

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

    tocando_agora[ctx.guild.id] = musica
    vc.play(fonte, after=depois)
    await ctx.send(f"▶️ Tocando: **{musica['titulo']}**")


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
            musicas = await loop.run_in_executor(None, extrair, busca)
        except Exception as e:
            print(f"Erro yt-dlp: {e}")
            await ctx.send("Não consegui achar essa música ou playlist.")
            return
    if not musicas:
        await ctx.send("Nenhuma música válida encontrada.")
        return
    filas.setdefault(ctx.guild.id, []).extend(musicas)
    if len(musicas) > 1:
        await ctx.send(f"Adicionadas **{len(musicas)} músicas** da playlist à fila!")
        if not ctx.voice_client.is_playing() or ctx.voice_client.is_paused():
            await tocar_proxima(ctx)
    else:
        if ctx.voice_client.is_playing() or ctx.voice_client.is_paused():
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

@bot.command(aliases=["fila", "q"])
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
    tocando_agora.pop(ctx.guild.id, None)
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.voice_client.disconnect()
        await ctx.send("Parei de tocar e saí do canal.")
    else:
        await ctx.voice_client.disconnect()
        await  ctx.send("Saí do canal.")

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