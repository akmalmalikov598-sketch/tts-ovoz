"""
Voice Studio (API kalitsiz) - bitta fayl.

  Matn -> Ovoz : edge-tts   (kalit shart emas, ayol/erkak ovozlar, o'zbekcha ham)
  Ovoz -> Matn : faster-whisper (o'z serveringizda ishlaydi)
  Ovoz klonlash: Coqui XTTS-v2 (ixtiyoriy, ENABLE_CLONE=1, katta server kerak)

Render sozlamalari:
  Build Command (klonlashsiz, yengil):
    pip install fastapi "uvicorn[standard]" httpx python-multipart edge-tts faster-whisper
  Build Command (klonlash bilan, og'ir, 8GB+ RAM tavsiya):
    pip install fastapi "uvicorn[standard]" python-multipart edge-tts faster-whisper coqui-tts
  Start Command:
    uvicorn app:app --host 0.0.0.0 --port $PORT
  Environment (hammasi ixtiyoriy):
    APP_PASSWORD      - ilovani parol bilan yopish
    WHISPER_MODEL     - tiny | base | small (default: base)
    ENABLE_CLONE      - 1 bo'lsa klonlash yoqiladi
    COQUI_TOS_AGREED  - 1 (XTTS litsenziyasi CPML, faqat notijorat; o'qib chiqib o'zingiz qo'ying)
    VOICES_DIR        - klonlangan ovozlar papkasi (default: voices)
    TELEGRAM_BOT_TOKEN - @BotFather bergan token (Telegram bot yoqiladi)
    TELEGRAM_ALLOWED_IDS - ixtiyoriy: ruxsat berilgan chat id'lar, vergul bilan (bo'sh = hamma)
    PUBLIC_URL        - ixtiyoriy: saytingiz manzili (Render'da RENDER_EXTERNAL_URL avtomatik)
"""
import asyncio
import glob
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import uuid
import wave
from contextlib import asynccontextmanager
from typing import List, Optional

import aiohttp
import edge_tts
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, Response

APP_PASSWORD = os.getenv("APP_PASSWORD", "")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
ENABLE_CLONE = os.getenv("ENABLE_CLONE", "0") == "1"
VOICES_DIR = os.getenv("VOICES_DIR", "voices")
os.makedirs(VOICES_DIR, exist_ok=True)

@asynccontextmanager
async def lifespan(_app):
    try:
        await tg_setup()
    except Exception as e:
        print("Telegram sozlashda xato:", e)
    yield


app = FastAPI(title="Voice Studio", lifespan=lifespan)


def auth(x_app_password: Optional[str] = Header(default=None)):
    if APP_PASSWORD and x_app_password != APP_PASSWORD:
        raise HTTPException(401, "Parol noto'g'ri")


@app.get("/api/config")
def config():
    return {"password_required": bool(APP_PASSWORD), "clone_enabled": ENABLE_CLONE}


# ------------------------------------------------------------------ ovozlar
_edge_cache: list = []


async def edge_voices():
    global _edge_cache
    if not _edge_cache:
        _edge_cache = await edge_tts.list_voices()
    return _edge_cache


def clone_dirs():
    out = []
    for d in sorted(glob.glob(os.path.join(VOICES_DIR, "*", "meta.json"))):
        try:
            meta = json.load(open(d, encoding="utf-8"))
            out.append({"id": "clone:" + os.path.basename(os.path.dirname(d)), "name": meta["name"],
                        "gender": "", "locale": "klon", "category": "cloned"})
        except Exception:
            pass
    return out


@app.get("/api/voices", dependencies=[Depends(auth)])
async def voices(gender: Optional[str] = None, lang: Optional[str] = None):
    out = clone_dirs()
    for v in await edge_voices():
        g = v["Gender"].lower()
        loc = v["Locale"]
        if gender and g != gender:
            continue
        if lang and not loc.lower().startswith(lang.lower()):
            continue
        out.append({"id": v["ShortName"], "name": v["ShortName"].replace("Neural", ""),
                    "gender": g, "locale": loc, "category": "edge"})
    return out


# ------------------------------------------------------------------ matn -> ovoz
_xtts = None


def get_xtts():
    global _xtts
    if _xtts is None:
        from TTS.api import TTS  # coqui-tts
        _xtts = TTS("tts_models/multilingual/multi-dataset/xtts_v2")
    return _xtts


def valid_vid(vid: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", vid):
        raise HTTPException(400, "Noto'g'ri ovoz id")
    return vid


def clone_speak(text: str, vid: str, lang: str) -> bytes:
    d = os.path.join(VOICES_DIR, valid_vid(vid))
    samples = sorted(glob.glob(os.path.join(d, "sample*.wav")))
    if not samples:
        raise HTTPException(404, "Ovoz namunalari topilmadi")
    fd, tmp = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        get_xtts().tts_to_file(text=text, speaker_wav=samples, language=lang, file_path=tmp)
        return open(tmp, "rb").read()
    finally:
        os.remove(tmp)


@app.post("/api/tts", dependencies=[Depends(auth)])
async def tts(payload: dict):
    text = (payload.get("text") or "").strip()
    voice = payload.get("voice_id") or ""
    if not text or not voice:
        raise HTTPException(400, "text va voice_id kerak")
    if voice.startswith("clone:"):
        if not ENABLE_CLONE:
            raise HTTPException(501, "Klonlash serverda yoqilmagan (ENABLE_CLONE=1)")
        lang = payload.get("clone_lang") or "tr"
        wav = await run_in_threadpool(clone_speak, text, voice.split(":", 1)[1], lang)
        return Response(wav, media_type="audio/wav")
    rate = int(payload.get("rate", 0))
    pitch = int(payload.get("pitch", 0))
    try:
        com = edge_tts.Communicate(text, voice, rate=f"{rate:+d}%", pitch=f"{pitch:+d}Hz")
        buf = bytearray()
        async for ch in com.stream():
            if ch["type"] == "audio":
                buf += ch["data"]
    except Exception as e:
        raise HTTPException(502, f"edge-tts xatosi: {e}")
    if not buf:
        raise HTTPException(502, "Ovoz yaratilmadi (matn yoki ovozni tekshiring)")
    return Response(bytes(buf), media_type="audio/mpeg")


# ------------------------------------------------------------------ ovoz -> matn
_whisper = None


def get_whisper():
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    return _whisper


def transcribe(path: str, lang: Optional[str]):
    segs, info = get_whisper().transcribe(path, language=lang or None, vad_filter=True, beam_size=1)
    return "".join(s.text for s in segs).strip(), info.language


@app.post("/api/stt", dependencies=[Depends(auth)])
async def stt(file: UploadFile = File(...), language: Optional[str] = Form(None)):
    data = await file.read()
    suffix = os.path.splitext(file.filename or "")[1] or ".webm"
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        open(tmp, "wb").write(data)
        text, lang = await run_in_threadpool(transcribe, tmp, language)
    except Exception as e:
        raise HTTPException(500, f"Whisper xatosi: {e}")
    finally:
        os.remove(tmp)
    return {"text": text, "language": lang}


# ------------------------------------------------------------------ klonlash
def to_wav(data: bytes, out_path: str):
    import av  # faster-whisper bilan birga o'rnatiladi
    container = av.open(io.BytesIO(data))
    rs = av.AudioResampler(format="s16", layout="mono", rate=22050)
    pcm = bytearray()
    for frame in container.decode(audio=0):
        for f in rs.resample(frame):
            pcm += f.to_ndarray().tobytes()
    for f in rs.resample(None) or []:
        pcm += f.to_ndarray().tobytes()
    with wave.open(out_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(bytes(pcm))


@app.post("/api/clone", dependencies=[Depends(auth)])
async def clone(name: str = Form(...), consent: bool = Form(...), files: List[UploadFile] = File(...)):
    if not ENABLE_CLONE:
        raise HTTPException(501, "Klonlash serverda yoqilmagan. ENABLE_CLONE=1 va coqui-tts o'rnatilishi kerak.")
    if not consent:
        raise HTTPException(400, "Ovoz egasining roziligi shart")
    vid = uuid.uuid4().hex
    d = os.path.join(VOICES_DIR, vid)
    os.makedirs(d)
    try:
        for i, f in enumerate(files):
            await run_in_threadpool(to_wav, await f.read(), os.path.join(d, f"sample{i}.wav"))
        json.dump({"name": name.strip()[:60]}, open(os.path.join(d, "meta.json"), "w", encoding="utf-8"))
    except Exception as e:
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(400, f"Namunani o'qib bo'lmadi: {e}")
    return {"voice_id": "clone:" + vid, "name": name}


@app.delete("/api/voices/{voice_id}", dependencies=[Depends(auth)])
async def delete_voice(voice_id: str):
    vid = valid_vid(voice_id.replace("clone:", ""))
    shutil.rmtree(os.path.join(VOICES_DIR, vid), ignore_errors=True)
    return {"ok": True}


# ------------------------------------------------------------------ Telegram bot (webhook)
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_SECRET = hashlib.sha256(("tg" + TG_TOKEN).encode()).hexdigest()[:32]
TG_ALLOWED = {int(x) for x in os.getenv("TELEGRAM_ALLOWED_IDS", "").replace(" ", "").split(",") if x}
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"
MAX_TTS_CHARS = 3000

PRESETS = [
    ("🇺🇿 Madina (ayol)", "uz-UZ-MadinaNeural"), ("🇺🇿 Sardor (erkak)", "uz-UZ-SardorNeural"),
    ("🇷🇺 Svetlana (ayol)", "ru-RU-SvetlanaNeural"), ("🇷🇺 Dmitry (erkak)", "ru-RU-DmitryNeural"),
    ("🇬🇧 Jenny (ayol)", "en-US-JennyNeural"), ("🇬🇧 Guy (erkak)", "en-US-GuyNeural"),
    ("🇹🇷 Emel (ayol)", "tr-TR-EmelNeural"), ("🇹🇷 Ahmet (erkak)", "tr-TR-AhmetNeural"),
]
USER_VOICE: dict = {}   # chat_id -> ovoz
USER_LANG: dict = {}    # chat_id -> whisper tili (None = avtomatik)
_tasks: set = set()

WELCOME = (
    "Salom! 🎙️ Men ovoz botman.\n\n"
    "• Menga <b>matn</b> yozing — ovozga aylantirib beraman.\n"
    "• Menga <b>ovozli xabar</b> yoki audio yuboring — matnga aylantirib beraman.\n\n"
    "Buyruqlar:\n"
    "/voice — ovozni tanlash (ayol/erkak, til)\n"
    "/lang uz|ru|en|tr|auto — ovozni matnga aylantirishda tilni belgilash\n"
)


async def tg(method: str, **data):
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{TG_API}/{method}", json=data) as r:
            return await r.json()


async def tg_send_audio(chat: int, data: bytes, title: str):
    fd = aiohttp.FormData()
    fd.add_field("chat_id", str(chat))
    fd.add_field("title", title)
    fd.add_field("audio", data, filename="ovoz.mp3", content_type="audio/mpeg")
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{TG_API}/sendAudio", data=fd) as r:
            return await r.json()


async def tg_download(file_id: str):
    info = await tg("getFile", file_id=file_id)
    path = info["result"]["file_path"]
    async with aiohttp.ClientSession() as s:
        async with s.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{path}") as r:
            return await r.read(), path


async def tg_say(chat: int, text: str):
    for i in range(0, len(text), 4000):
        await tg("sendMessage", chat_id=chat, text=text[i:i + 4000])


async def tg_handle(u: dict):
    chat = None
    try:
        if "callback_query" in u:
            cq = u["callback_query"]
            chat = cq["message"]["chat"]["id"]
            data = cq.get("data", "")
            if data.startswith("v:"):
                USER_VOICE[chat] = data[2:]
                await tg("answerCallbackQuery", callback_query_id=cq["id"], text="Ovoz tanlandi ✓")
                await tg_say(chat, f"Ovoz tanlandi: {data[2:].replace('Neural', '')}\nEndi menga matn yuboring.")
            return
        m = u.get("message")
        if not m:
            return
        chat = m["chat"]["id"]
        if TG_ALLOWED and chat not in TG_ALLOWED:
            await tg_say(chat, "Kechirasiz, bu bot yopiq.")
            return
        text = (m.get("text") or "").strip()
        if text:
            cmd = text.split()[0].split("@")[0].lower()
            if cmd in ("/start", "/help"):
                await tg("sendMessage", chat_id=chat, text=WELCOME, parse_mode="HTML")
            elif cmd == "/voice":
                rows = [[{"text": PRESETS[i][0], "callback_data": "v:" + PRESETS[i][1]},
                         {"text": PRESETS[i + 1][0], "callback_data": "v:" + PRESETS[i + 1][1]}]
                        for i in range(0, len(PRESETS), 2)]
                await tg("sendMessage", chat_id=chat, text="Ovozni tanlang:", reply_markup={"inline_keyboard": rows})
            elif cmd == "/lang":
                arg = (text.split()[1:] or ["auto"])[0].lower()
                USER_LANG[chat] = None if arg == "auto" else arg
                await tg_say(chat, f"Til: {arg}")
            elif cmd.startswith("/"):
                await tg_say(chat, "Noma'lum buyruq. /start ni bosing.")
            else:
                if len(text) > MAX_TTS_CHARS:
                    return await tg_say(chat, f"Matn juda uzun (maksimum {MAX_TTS_CHARS} belgi).")
                await tg("sendChatAction", chat_id=chat, action="record_voice")
                voice = USER_VOICE.get(chat, "uz-UZ-MadinaNeural")
                buf = bytearray()
                async for ch in edge_tts.Communicate(text, voice).stream():
                    if ch["type"] == "audio":
                        buf += ch["data"]
                if not buf:
                    return await tg_say(chat, "Ovoz yaratilmadi. Matnni tekshirib qayta urinib ko'ring.")
                await tg_send_audio(chat, bytes(buf), text[:40])
            return
        media = m.get("voice") or m.get("audio") or m.get("video_note") or m.get("document")
        if media:
            await tg("sendChatAction", chat_id=chat, action="typing")
            data, path = await tg_download(media["file_id"])
            fd, tmp = tempfile.mkstemp(suffix=os.path.splitext(path)[1] or ".oga")
            os.close(fd)
            try:
                open(tmp, "wb").write(data)
                out, lang = await run_in_threadpool(transcribe, tmp, USER_LANG.get(chat))
            finally:
                os.remove(tmp)
            await tg_say(chat, out or "Gap topilmadi.")
            return
        await tg_say(chat, "Matn yoki ovozli xabar yuboring. /start")
    except Exception as e:
        print("Telegram xato:", e)
        if chat:
            await tg_say(chat, f"Xatolik: {e}")


@app.post("/tg/{secret}")
async def tg_webhook(secret: str, request: Request):
    if not TG_TOKEN or secret != TG_SECRET:
        raise HTTPException(403)
    t = asyncio.create_task(tg_handle(await request.json()))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    return {"ok": True}


async def tg_setup():
    base = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
    if not TG_TOKEN:
        print("TELEGRAM_BOT_TOKEN yo'q: Telegram bot o'chiq")
        return
    if not base:
        print("PUBLIC_URL topilmadi: webhook o'rnatilmadi")
        return
    url = f"{base}/tg/{TG_SECRET}"
    info = await tg("getWebhookInfo")
    if info.get("result", {}).get("url") != url:
        r = await tg("setWebhook", url=url, allowed_updates=["message", "callback_query"],
                     drop_pending_updates=True)
        print("setWebhook:", r)
    await tg("setMyCommands", commands=[
        {"command": "start", "description": "Boshlash"},
        {"command": "voice", "description": "Ovozni tanlash"},
        {"command": "lang", "description": "Ovoz->matn tili"},
    ])


@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def index():
    return HTML


HTML = r"""<!doctype html>
<html lang="uz">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Voice Studio</title>
<style>
  :root { --bg:#0f1115; --card:#181b22; --line:#2a2f3a; --txt:#e8eaf0; --mut:#8b92a3; --acc:#6c8cff; --ok:#3ecf8e; --err:#ff6b6b; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--txt); font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
  .wrap { max-width:720px; margin:0 auto; padding:16px; }
  h1 { font-size:22px; margin:8px 0 16px; }
  .tabs { display:flex; gap:6px; margin-bottom:14px; }
  .tab { flex:1; padding:10px 6px; background:var(--card); border:1px solid var(--line); color:var(--mut); border-radius:10px; cursor:pointer; font-size:14px; }
  .tab.on { color:#fff; border-color:var(--acc); background:#1d2335; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:16px; display:none; }
  .card.on { display:block; }
  label { display:block; font-size:13px; color:var(--mut); margin:12px 0 6px; }
  textarea, input[type=text], input[type=password], select { width:100%; padding:10px 12px; background:#0f1218; border:1px solid var(--line); color:var(--txt); border-radius:10px; font:inherit; }
  textarea { min-height:140px; resize:vertical; }
  button { padding:11px 16px; background:var(--acc); color:#fff; border:0; border-radius:10px; font:inherit; font-weight:600; cursor:pointer; }
  button.sec { background:#262b38; font-weight:500; }
  button.rec { background:var(--err); }
  button:disabled { opacity:.5; cursor:default; }
  .row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-top:12px; }
  .row > * { flex:0 0 auto; }
  .grow { flex:1 1 auto !important; }
  audio { width:100%; margin-top:12px; }
  .msg { font-size:14px; margin-top:10px; min-height:20px; color:var(--mut); }
  .msg.err { color:var(--err); } .msg.ok { color:var(--ok); }
  .out { background:#0f1218; border:1px solid var(--line); border-radius:10px; padding:12px; margin-top:12px; white-space:pre-wrap; min-height:60px; }
  .chk { display:flex; gap:8px; align-items:flex-start; font-size:14px; color:var(--txt); margin-top:14px; }
  .chk input { margin-top:4px; }
  .samples div { font-size:13px; color:var(--mut); margin-top:6px; }
  input[type=range] { width:100%; }
  .half { display:flex; gap:8px; } .half > div { flex:1; }
</style>
</head>
<body>
<div class="wrap">
  <h1>🎙️ Voice Studio</h1>

  <div id="lock" class="card on" style="display:none">
    <label>Parol</label>
    <input type="password" id="pw" placeholder="Ilova paroli">
    <div class="row"><button onclick="savePw()">Kirish</button></div>
  </div>

  <div id="app" style="display:none">
    <div class="tabs">
      <button class="tab on" data-t="tts">Matn → Ovoz</button>
      <button class="tab" data-t="stt">Ovoz → Matn</button>
      <button class="tab" data-t="clone">Klonlash</button>
    </div>

    <!-- TTS -->
    <div class="card on" id="tts">
      <div class="half">
        <div><label>Til</label>
          <select id="vlang" onchange="loadVoices()">
            <option value="uz">O'zbekcha</option>
            <option value="ru">Ruscha</option>
            <option value="en">Inglizcha</option>
            <option value="tr">Turkcha</option>
            <option value="kk">Qozoqcha</option>
            <option value="">Hammasi</option>
          </select></div>
        <div><label>Jins</label>
          <select id="gender" onchange="loadVoices()">
            <option value="">Hammasi</option>
            <option value="female">Ayol ovozi</option>
            <option value="male">Erkak ovozi</option>
          </select></div>
      </div>
      <label>Ovoz</label>
      <select id="voice" onchange="onVoice()"></select>
      <div id="cloneLangBox" style="display:none">
        <label>Klon ovoz tili (XTTS o'zbekchani qo'llamaydi; lotin o'zbekcha uchun "Turkcha" eng yaqin)</label>
        <select id="cloneLang">
          <option value="tr">Turkcha</option><option value="ru">Ruscha</option><option value="en">Inglizcha</option>
          <option value="ar">Arabcha</option><option value="es">Ispancha</option><option value="de">Nemischa</option>
          <option value="fr">Fransuzcha</option>
        </select>
      </div>
      <label>Matn</label>
      <textarea id="ttsText" placeholder="Gapirtirmoqchi bo'lgan matnni yozing..."></textarea>
      <div id="edgeOpts">
        <label>Tezlik: <span id="rv">0</span>%</label>
        <input type="range" id="rate" min="-50" max="50" step="5" value="0" oninput="rv.textContent=this.value">
        <label>Ohang: <span id="pv">0</span> Hz</label>
        <input type="range" id="pitch" min="-50" max="50" step="5" value="0" oninput="pv.textContent=this.value">
      </div>
      <div class="row"><button id="ttsBtn" onclick="doTTS()">Ovozga aylantirish</button></div>
      <audio id="ttsAudio" controls style="display:none"></audio>
      <div class="msg" id="ttsMsg"></div>
    </div>

    <!-- STT -->
    <div class="card" id="stt">
      <label>Til</label>
      <select id="lang">
        <option value="uz">O'zbekcha</option>
        <option value="">Avtomatik aniqlash</option>
        <option value="ru">Ruscha</option>
        <option value="en">Inglizcha</option>
        <option value="tr">Turkcha</option>
      </select>
      <div class="row">
        <button id="sttRec" class="rec" onclick="toggleRec('stt')">● Yozib olish</button>
        <input type="file" id="sttFile" accept="audio/*,video/*" class="grow">
      </div>
      <div class="row"><button id="sttBtn" onclick="doSTT()">Matnga aylantirish</button></div>
      <div class="out" id="sttOut"></div>
      <div class="row"><button class="sec" onclick="navigator.clipboard.writeText(sttOut.textContent)">Nusxalash</button></div>
      <div class="msg" id="sttMsg"></div>
    </div>

    <!-- CLONE -->
    <div class="card" id="clone">
      <div class="msg" id="cOff" style="display:none;color:var(--err)">Klonlash serverda yoqilmagan (ENABLE_CLONE=1 va coqui-tts kerak).</div>
      <label>Yangi ovoz nomi</label>
      <input type="text" id="cName" placeholder="Masalan: Mening ovozim">
      <label>Namuna ovoz (1–3 daqiqa toza, shovqinsiz, bir kishi). Bir nechta fayl qo'shish mumkin.</label>
      <div class="row" style="margin-top:0">
        <button id="cRec" class="rec" onclick="toggleRec('clone')">● Yozib olish</button>
        <input type="file" id="cFiles" accept="audio/*" multiple class="grow" onchange="addFiles(this.files)">
      </div>
      <div class="samples" id="samples"></div>
      <label class="chk"><input type="checkbox" id="consent">
        <span>Bu ovoz egasi menman yoki ovoz egasi klonlashga aniq rozilik bergan. Boshqa odamning ovozini ruxsatsiz klonlash — qonunga zid.</span>
      </label>
      <div class="row"><button id="cBtn" onclick="doClone()">Ovozni klonlash</button></div>
      <div class="msg" id="cMsg"></div>
      <label style="margin-top:20px">Klonlangan ovozlarim</label>
      <div id="myVoices"></div>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
let PW = localStorage.getItem('pw') || '';
let CFG = {};
let cloneSamples = [];
let mediaRec = null, chunks = [], recTarget = null, sttBlob = null;

function H() { return PW ? {'X-App-Password': PW} : {}; }
function setMsg(id, text, cls='') { const e=$(id); e.textContent=text; e.className='msg '+cls; }

async function init() {
  CFG = await (await fetch('/api/config')).json();
  if (CFG.password_required && !PW) { $('lock').style.display='block'; return; }
  $('lock').style.display='none'; $('app').style.display='block';
  $('cOff').style.display = CFG.clone_enabled ? 'none' : 'block';
  loadVoices();
}
function savePw() { PW = $('pw').value; localStorage.setItem('pw', PW); init(); }

document.querySelectorAll('.tab').forEach(t => t.onclick = () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('on'));
  document.querySelectorAll('#app .card').forEach(x => x.classList.remove('on'));
  t.classList.add('on'); $(t.dataset.t).classList.add('on');
  if (t.dataset.t === 'clone') loadMyVoices();
});

async function api(url, opts={}) {
  opts.headers = Object.assign({}, H(), opts.headers||{});
  const r = await fetch(url, opts);
  if (r.status === 401) { localStorage.removeItem('pw'); PW=''; location.reload(); }
  if (!r.ok) { let t = await r.text(); try { t = JSON.parse(t).detail || t; } catch(e){} throw new Error(typeof t==='string'?t:JSON.stringify(t)); }
  return r;
}

async function loadVoices() {
  try {
    const q = new URLSearchParams();
    if ($('gender').value) q.set('gender', $('gender').value);
    if ($('vlang').value) q.set('lang', $('vlang').value);
    const list = await (await api('/api/voices?' + q)).json();
    $('voice').innerHTML = list.map(v =>
      `<option value="${v.id}">${v.category==='cloned'?'⭐ ':''}${v.name}${v.gender?' ('+(v.gender==='female'?'ayol':'erkak')+')':''}</option>`).join('');
    onVoice();
  } catch(e) { setMsg('ttsMsg', e.message, 'err'); }
}
function onVoice() {
  const isClone = ($('voice').value || '').startsWith('clone:');
  $('cloneLangBox').style.display = isClone ? 'block' : 'none';
  $('edgeOpts').style.display = isClone ? 'none' : 'block';
}

async function doTTS() {
  const text = $('ttsText').value.trim();
  if (!text) return setMsg('ttsMsg', 'Matn yozing', 'err');
  $('ttsBtn').disabled = true; setMsg('ttsMsg', 'Ovoz yaratilmoqda...');
  try {
    const r = await api('/api/tts', { method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ text, voice_id: $('voice').value, rate: +$('rate').value, pitch: +$('pitch').value, clone_lang: $('cloneLang').value }) });
    const blob = await r.blob();
    const a = $('ttsAudio'); a.src = URL.createObjectURL(blob); a.style.display='block'; a.play();
    setMsg('ttsMsg', 'Tayyor ✓', 'ok');
  } catch(e) { setMsg('ttsMsg', e.message, 'err'); }
  $('ttsBtn').disabled = false;
}

// ---- yozib olish ----
async function toggleRec(target) {
  const btn = target==='stt' ? $('sttRec') : $('cRec');
  if (mediaRec && mediaRec.state === 'recording') { mediaRec.stop(); return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    chunks = []; recTarget = target;
    mediaRec = new MediaRecorder(stream);
    mediaRec.ondataavailable = e => chunks.push(e.data);
    mediaRec.onstop = () => {
      stream.getTracks().forEach(t => t.stop());
      const blob = new Blob(chunks, { type: mediaRec.mimeType || 'audio/webm' });
      const ext = (blob.type.includes('mp4') ? 'm4a' : 'webm');
      if (recTarget === 'stt') { sttBlob = new File([blob], 'rec.'+ext, {type: blob.type}); setMsg('sttMsg', 'Yozuv tayyor. "Matnga aylantirish" ni bosing.', 'ok'); }
      else { addFiles([new File([blob], 'sample-'+Date.now()+'.'+ext, {type: blob.type})]); }
      btn.textContent = '● Yozib olish';
    };
    mediaRec.start(); btn.textContent = '■ To\'xtatish';
  } catch(e) { alert('Mikrofonga ruxsat berilmadi: ' + e.message); }
}

// ---- STT ----
async function doSTT() {
  const f = sttBlob || $('sttFile').files[0];
  if (!f) return setMsg('sttMsg', 'Yozib oling yoki fayl tanlang', 'err');
  $('sttBtn').disabled = true; setMsg('sttMsg', 'Matnga aylantirilmoqda (birinchi marta model yuklanadi, biroz kuting)...');
  const fd = new FormData(); fd.append('file', f); if ($('lang').value) fd.append('language', $('lang').value);
  try {
    const j = await (await api('/api/stt', { method:'POST', body: fd })).json();
    $('sttOut').textContent = j.text || '(matn topilmadi)';
    setMsg('sttMsg', j.language ? 'Til: '+j.language : '', 'ok');
  } catch(e) { setMsg('sttMsg', e.message, 'err'); }
  $('sttBtn').disabled = false;
}
$('sttFile').onchange = () => { sttBlob = null; };

// ---- Clone ----
function addFiles(files) {
  for (const f of files) cloneSamples.push(f);
  $('samples').innerHTML = cloneSamples.map((f,i)=>`<div>• ${f.name} (${(f.size/1024).toFixed(0)} KB) <a href="#" onclick="cloneSamples.splice(${i},1);addFiles([]);return false">o'chirish</a></div>`).join('');
}
async function doClone() {
  if (!$('cName').value.trim()) return setMsg('cMsg', 'Ovoz nomini yozing', 'err');
  if (!cloneSamples.length) return setMsg('cMsg', 'Kamida bitta namuna kerak', 'err');
  if (!$('consent').checked) return setMsg('cMsg', 'Rozilik belgisini qo\'ying', 'err');
  $('cBtn').disabled = true; setMsg('cMsg', 'Namunalar saqlanmoqda...');
  const fd = new FormData(); fd.append('name', $('cName').value.trim()); fd.append('consent', 'true');
  cloneSamples.forEach(f => fd.append('files', f));
  try {
    const j = await (await api('/api/clone', { method:'POST', body: fd })).json();
    setMsg('cMsg', `"${j.name}" tayyor ✓ — "Matn → Ovoz" bo'limida ⭐ bilan chiqadi (Til: Hammasi)`, 'ok');
    cloneSamples = []; addFiles([]); loadMyVoices(); loadVoices();
  } catch(e) { setMsg('cMsg', e.message, 'err'); }
  $('cBtn').disabled = false;
}
async function loadMyVoices() {
  try {
    const list = (await (await api('/api/voices')).json()).filter(v => v.category === 'cloned');
    $('myVoices').innerHTML = list.length ? list.map(v =>
      `<div class="row"><span class="grow">⭐ ${v.name}</span><button class="sec" onclick="delVoice('${v.id}')">O'chirish</button></div>`).join('')
      : '<div class="msg">Hali klonlangan ovoz yo\'q</div>';
  } catch(e) {}
}
async function delVoice(id) {
  if (!confirm('Bu ovozni o\'chirasizmi?')) return;
  await api('/api/voices/'+encodeURIComponent(id), { method:'DELETE' }); loadMyVoices(); loadVoices();
}

init();
</script>
</body>
</html>
"""
