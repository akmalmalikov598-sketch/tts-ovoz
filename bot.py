import os
import json
import time
import wave
import asyncio
import logging
import subprocess
import threading
import numpy as np
from scipy.signal import butter, lfilter
import edge_tts
import imageio_ffmpeg
from flask import Flask
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# --- LOGGING ---
logging.basicConfig(level=logging.INFO)

# --- ENVIRONMENT VARIABLES ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip() or 0)

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN topilmadi! Render Environment Variables ga qo'shing.")

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

# --- MA'LUMOTLAR BAZASI (JSON) ---
DB_FILE = "bot_data.json"

def load_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "users": {},
        "channels": [],
        "total_generated": 0
    }

def save_db(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

db = load_db()

# --- STANDART TABIIY OVOZLAR ---
VOICES = {
    "uz_female": {"id": "uz-UZ-MadinaNeural", "name": "🇺🇿 Madina (Ayol)", "gender": "female"},
    "uz_male": {"id": "uz-UZ-SardorNeural", "name": "🇺🇿 Sardor (Erkak)", "gender": "male"},
    "ru_female": {"id": "ru-RU-SvetlanaNeural", "name": "🇷🇺 Svetlana (Ayol)", "gender": "female"},
    "ru_male": {"id": "ru-RU-DmitryNeural", "name": "🇷🇺 Dmitry (Erkak)", "gender": "male"},
    "en_female": {"id": "en-US-JennyNeural", "name": "🇺🇸 Jenny (Ayol)", "gender": "female"},
    "en_male": {"id": "en-US-GuyNeural", "name": "🇺🇸 Guy (Erkak)", "gender": "male"},
    "tr_female": {"id": "tr-TR-EmelNeural", "name": "🇹🇷 Emel (Ayol)", "gender": "female"},
    "tr_male": {"id": "tr-TR-AhmetNeural", "name": "🇹🇷 Ahmet (Erkak)", "gender": "male"},
}

# --- BOT VA DISPATCHER ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class BotStates(StatesGroup):
    waiting_voice_sample = State()
    waiting_voice_name = State()
    admin_add_channel = State()
    admin_broadcast = State()

# --- OVOZNI TAHLIL QILISH VA KLONLASH DVIGATELI (100% BEPUL, API-SIZ) ---
def analyze_voice_profile(wav_path: str) -> dict:
    """Foydalanuvchi ovozidan asosiy chastota (F0), jins, tembr va energiyani aniqlaydi."""
    with wave.open(wav_path, "rb") as wf:
        sr = wf.getframerate()
        n_frames = wf.getnframes()
        raw_data = wf.readframes(n_frames)
        audio = np.frombuffer(raw_data, dtype=np.int16).astype(np.float32) / 32768.0

    # Jimjitlikni kesib tashlash
    energy = np.abs(audio)
    threshold = np.percentile(energy, 40)
    voiced = audio[energy > max(threshold, 0.01)]
    if len(voiced) < sr:
        voiced = audio

    # Bandpass filtr (70 Hz - 350 Hz inson ovozi asosiy chastotasi)
    nyq = 0.5 * sr
    b, a = butter(3, [70.0 / nyq, 350.0 / nyq], btype="band")
    filtered = lfilter(b, a, voiced)

    # Autocorrelation orqali asosiy chastota (Pitch F0) ni hisoblash
    frame_len = int(sr * 0.04)  # 40 ms
    pitches = []
    min_lag = int(sr / 320)
    max_lag = int(sr / 75)

    for i in range(0, len(filtered) - frame_len, frame_len):
        frame = filtered[i:i + frame_len]
        if np.mean(frame ** 2) < 0.0005:
            continue
        corr = np.correlate(frame, frame, mode="full")[len(frame) - 1:]
        if max_lag < len(corr):
            peak = np.argmax(corr[min_lag:max_lag]) + min_lag
            if corr[peak] > 0.35 * corr[0]:
                f0 = sr / peak
                pitches.append(f0)

    median_f0 = float(np.median(pitches)) if pitches else 145.0

    # Spektral yorqinlik (tembr ingichka yoki yo'g'onligi)
    fft_vals = np.abs(np.fft.rfft(voiced[:min(len(voiced), sr * 5)]))
    freqs = np.fft.rfftfreq(min(len(voiced), sr * 5), 1.0 / sr)
    centroid = float(np.sum(freqs * fft_vals) / (np.sum(fft_vals) + 1e-8))

    # Jins va bazaviy modelni aniqlash
    if median_f0 >= 165.0:
        gender = "female"
        base_f0 = 210.0  # MadinaNeural o'rtacha F0
    else:
        gender = "male"
        base_f0 = 115.0  # SardorNeural o'rtacha F0

    # Pitch farqini Hz ga o'tkazish (-35Hz dan +35Hz gacha)
    pitch_diff = int(np.clip((median_f0 - base_f0) * 0.65, -35, 35))
    pitch_str = f"{pitch_diff:+d}Hz"

    # Tembr koeffitsiyenti (formant siljishi uchun 0.92 - 1.08)
    timbre_ratio = float(np.clip(1.0 + (centroid - 1400.0) / 14000.0, 0.93, 1.07))

    return {
        "gender": gender,
        "f0": round(median_f0, 1),
        "pitch": pitch_str,
        "timbre_ratio": round(timbre_ratio, 3),
        "rate": "+0%"
    }

def process_cloned_audio(input_mp3: str, output_ogg: str, profile: dict):
    """Yaratilgan ovozga foydalanuvchi tembri va akustik eq filtrlarini qo'llaydi."""
    ratio = profile.get("timbre_ratio", 1.0)
    inv_ratio = round(1.0 / ratio, 4)
    # Formant va tabiiy iliqlik filtri (asetrate + atempo + compressor)
    af_filter = (
        f"asetrate=24000*{ratio},aresample=24000,atempo={inv_ratio},"
        f"equalizer=f=250:width_type=o:width=1.5:g=2,"
        f"acompressor=threshold=-18dB:ratio=2.5:attack=15:release=150"
    )
    cmd = [
        FFMPEG_PATH, "-y", "-i", input_mp3,
        "-af", af_filter,
        "-c:a", "libopus", "-b:a", "48k", "-vbr", "on",
        output_ogg
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

def convert_to_ogg(input_path: str, output_path: str):
    cmd = [
        FFMPEG_PATH, "-y", "-i", input_path,
        "-c:a", "libopus", "-b:a", "48k", "-vbr", "on", output_path
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

# --- YORDAMCHI FUNKSIYALAR ---
def get_user(user_id: int, full_name: str = "Foydalanuvchi"):
    uid = str(user_id)
    if uid not in db["users"]:
        db["users"][uid] = {
            "name": full_name,
            "voice": "uz_female",
            "rate": "+0%",
            "pitch": "+0Hz",
            "cloned_voices": {}
        }
        save_db(db)
    return db["users"][uid]

async def check_subs(user_id: int) -> list:
    if user_id == ADMIN_ID or not db["channels"]:
        return []
    not_subbed = []
    for ch in db["channels"]:
        try:
            member = await bot.get_chat_member(chat_id=ch, user_id=user_id)
            if member.status in ["left", "kicked"]:
                not_subbed.append(ch)
        except Exception:
            continue
    return not_subbed

# --- KLAVIATURALAR ---
def main_menu(user_id: int):
    kb = [
        [KeyboardButton(text="🎙 Ovozni tanlash"), KeyboardButton(text="🧬 O'z ovozimni klonlash")],
        [KeyboardButton(text="⚙️ Tezlik va Ton"), KeyboardButton(text="ℹ️ Mening sozlamalarim")]
    ]
    if user_id == ADMIN_ID:
        kb.append([KeyboardButton(text="👑 Admin Panel")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def voices_inline(user_data: dict):
    buttons = []
    row = []
    for key, val in VOICES.items():
        mark = "✅ " if user_data["voice"] == key else ""
        row.append(InlineKeyboardButton(text=f"{mark}{val['name']}", callback_data=f"setvoice:{key}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    clones = user_data.get("cloned_voices", {})
    if clones:
        buttons.append([InlineKeyboardButton(text="--- 🧬 Mening klonlangan ovozlarim ---", callback_data="ignore")])
        for cname in clones.keys():
            mark = "✅ " if user_data["voice"] == f"clone:{cname}" else ""
            buttons.append([
                InlineKeyboardButton(text=f"{mark}🎤 {cname}", callback_data=f"setvoice:clone:{cname}"),
                InlineKeyboardButton(text="🗑", callback_data=f"delclone:{cname}")
            ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def settings_inline(user_data: dict):
    r = user_data.get("rate", "+0%")
    p = user_data.get("pitch", "+0Hz")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⏩ Tezlik: {r}", callback_data="ignore")],
        [
            InlineKeyboardButton(text="🐢 Sekin (-20%)", callback_data="rate:-20%"),
            InlineKeyboardButton(text="🚶 Normal (0%)", callback_data="rate:+0%"),
            InlineKeyboardButton(text="🐇 Tez (+25%)", callback_data="rate:+25%")
        ],
        [InlineKeyboardButton(text=f"🎵 Ovoz toni (Pitch): {p}", callback_data="ignore")],
        [
            InlineKeyboardButton(text="🔉 Yo'g'on (-15Hz)", callback_data="pitch:-15Hz"),
            InlineKeyboardButton(text="🎙 Tabiiy (0Hz)", callback_data="pitch:+0Hz"),
            InlineKeyboardButton(text="🔊 Ingichka (+15Hz)", callback_data="pitch:+15Hz")
        ]
    ])

def admin_inline():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Statistika", callback_data="adm:stats")],
        [InlineKeyboardButton(text="📢 Xabar tarqatish (Rassilka)", callback_data="adm:broadcast")],
        [
            InlineKeyboardButton(text="➕ Kanal qo'shish", callback_data="adm:add_ch"),
            InlineKeyboardButton(text="➖ Kanallarni ko'rish/o'chirish", callback_data="adm:list_ch")
        ]
    ])

# --- HANDLERLAR ---
@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    get_user(message.from_user.id, message.from_user.full_name)
    not_sub = await check_subs(message.from_user.id)
    if not_sub:
        btns = [[InlineKeyboardButton(text=f"📢 Obuna bo'lish ({ch})", url=f"https://t.me/{ch.replace('@', '')}")] for ch in not_sub]
        btns.append([InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_sub")])
        return await message.answer(
            "Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=btns)
        )

    await message.answer(
        f"👋 Assalomu alaykum, <b>{message.from_user.first_name}</b>!\n\n"
        "Menga istalgan matnni yuboring — men uni xuddi tirik odamning ovozidek qilib "
        "<b>ovozli xabar</b> ko'rinishida qaytaraman!\n\n"
        "🎙 <b>Imkoniyatlar (100% Bepul):</b>\n"
        "• O'zbek, Rus, Ingliz, Turk tillarida tabiiy erkak va ayol ovozlari\n"
        "• 🧬 <b>O'z ovozingizni klonlash</b> (ovoz yuboring — bot ovozingiz tembri va ohangini nusxalab, o'z ovozingizga o'xshatib gapiradi)\n"
        "• ⚙️ Ovoz tezligi va tonini moslash",
        parse_mode="HTML",
        reply_markup=main_menu(message.from_user.id)
    )

@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(call: types.CallbackQuery):
    not_sub = await check_subs(call.from_user.id)
    if not_sub:
        return await call.answer("❌ Hali barcha kanallarga obuna bo'lmadingiz!", show_alert=True)
    await call.message.delete()
    await call.message.answer("✅ Obuna tasdiqlandi! Endi matn yuborishingiz mumkin.", reply_markup=main_menu(call.from_user.id))

@dp.message(F.text == "🎙 Ovozni tanlash")
async def menu_voices(message: types.Message):
    user = get_user(message.from_user.id, message.from_user.full_name)
    await message.answer("🗣 Kerakli ovozni tanlang:", reply_markup=voices_inline(user))

@dp.callback_query(F.data.startswith("setvoice:"))
async def cb_set_voice(call: types.CallbackQuery):
    user = get_user(call.from_user.id)
    chosen = call.data.split("setvoice:", 1)[1]
    user["voice"] = chosen
    save_db(db)
    await call.message.edit_reply_markup(reply_markup=voices_inline(user))
    await call.answer("✅ Ovoz o'zgartirildi!")

@dp.callback_query(F.data.startswith("delclone:"))
async def cb_del_clone(call: types.CallbackQuery):
    user = get_user(call.from_user.id)
    cname = call.data.split("delclone:", 1)[1]
    clones = user.get("cloned_voices", {})
    if cname in clones:
        del clones[cname]
    if user["voice"] == f"clone:{cname}":
        user["voice"] = "uz_female"
    save_db(db)
    await call.message.edit_reply_markup(reply_markup=voices_inline(user))
    await call.answer("🗑 Klonlangan ovoz o'chirildi")

@dp.message(F.text == "⚙️ Tezlik va Ton")
async def menu_settings(message: types.Message):
    user = get_user(message.from_user.id)
    await message.answer(
        "⚙️ <b>Ovoz parametrlarini sozlash:</b>",
        parse_mode="HTML",
        reply_markup=settings_inline(user)
    )

@dp.callback_query(F.data.startswith("rate:"))
async def cb_rate(call: types.CallbackQuery):
    user = get_user(call.from_user.id)
    user["rate"] = call.data.split(":", 1)[1]
    save_db(db)
    await call.message.edit_reply_markup(reply_markup=settings_inline(user))
    await call.answer("✅ Tezlik saqlandi")

@dp.callback_query(F.data.startswith("pitch:"))
async def cb_pitch(call: types.CallbackQuery):
    user = get_user(call.from_user.id)
    user["pitch"] = call.data.split(":", 1)[1]
    save_db(db)
    await call.message.edit_reply_markup(reply_markup=settings_inline(user))
    await call.answer("✅ Ovoz toni saqlandi")

@dp.message(F.text == "ℹ️ Mening sozlamalarim")
async def menu_info(message: types.Message):
    user = get_user(message.from_user.id)
    v = user["voice"]
    if v.startswith("clone:"):
        cname = v.split(":", 1)[1]
        v_name = f"🧬 {cname} (Shaxsiy klon)"
    else:
        v_name = VOICES.get(v, VOICES["uz_female"])["name"]

    await message.answer(
        f"👤 <b>Sizning sozlamalaringiz:</b>\n\n"
        f"🎙 Tanlangan ovoz: <b>{v_name}</b>\n"
        f"⏩ Tezlik: <b>{user.get('rate', '+0%')}</b>\n"
        f"🎵 Ovoz toni: <b>{user.get('pitch', '+0Hz')}</b>\n"
        f"🧬 Klonlangan ovozlar: <b>{len(user.get('cloned_voices', {}))} ta</b>",
        parse_mode="HTML"
    )

# --- API-SIZ OVOZ KLONLASH ---
@dp.message(F.text == "🧬 O'z ovozimni klonlash")
async def start_clone(message: types.Message, state: FSMContext):
    await state.set_state(BotStates.waiting_voice_sample)
    await message.answer(
        "🎤 <b>O'z ovozingizni klonlash (100% Bepul):</b>\n\n"
        "Shovqinsiz joyda <b>10–30 soniya</b> davomida tabiiy gapirib, menga <b>ovozli xabar (voice)</b> yoki <b>audio fayl</b> yuboring.\n"
        "Bot ovozingiz chastotasi (F0), tembri va jinsini tahlil qilib, shaxsiy ovoz profilingizni yaratadi!\n\n"
        "❌ Bekor qilish uchun /start bosing.",
        parse_mode="HTML"
    )

@dp.message(BotStates.waiting_voice_sample, F.voice | F.audio)
async def receive_voice_sample(message: types.Message, state: FSMContext):
    file_id = message.voice.file_id if message.voice else message.audio.file_id
    duration = message.voice.duration if message.voice else (message.audio.duration or 15)
    if duration < 4:
        return await message.answer("⚠️ Ovoz juda qisqa! Kamida 5-10 soniyalik ovozli xabar yuboring.")

    await state.update_data(sample_file_id=file_id)
    await state.set_state(BotStates.waiting_voice_name)
    await message.answer("✍️ Ajoyib! Endi bu ovozga nom bering (masalan: <i>Akmal</i> yoki <i>Mening ovozim</i>):", parse_mode="HTML")

@dp.message(BotStates.waiting_voice_name, F.text)
async def receive_voice_name(message: types.Message, state: FSMContext):
    voice_name = message.text.strip()[:25]
    data = await state.get_data()
    file_id = data.get("sample_file_id")
    await state.clear()

    wait_msg = await message.answer("⏳ Ovozingiz tembri va chastotasi tahlil qilinmoqda...")
    uid = message.from_user.id
    raw_path = f"sample_{uid}_{int(time.time())}.ogg"
    wav_path = f"sample_{uid}_{int(time.time())}.wav"

    try:
        tg_file = await bot.get_file(file_id)
        await bot.download_file(tg_file.file_path, raw_path)

        # WAV 16kHz mono formatga o'tkazish
        subprocess.run(
            [FFMPEG_PATH, "-y", "-i", raw_path, "-ar", "16000", "-ac", "1", wav_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
        )

        profile = analyze_voice_profile(wav_path)
        user = get_user(uid)
        user.setdefault("cloned_voices", {})[voice_name] = profile
        user["voice"] = f"clone:{voice_name}"
        save_db(db)

        gender_uz = "Ayol" if profile["gender"] == "female" else "Erkak"
        await wait_msg.edit_text(
            f"✅ <b>«{voice_name}» ovozi muvaffaqiyatli klonlandi va tanlandi!</b>\n\n"
            f"📊 <b>Ovoz tahlili natijasi:</b>\n"
            f"• Aniqlangan tur: <b>{gender_uz} ovozi</b>\n"
            f"• Asosiy chastota (F0): <b>{profile['f0']} Hz</b>\n"
            f"• Ovoz ohangi (Pitch): <b>{profile['pitch']}</b>\n\n"
            "Endi menga istalgan matn yuboring — o'zingizning ovoz tembringizda o'qib beraman!",
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Clone error: {e}")
        await wait_msg.edit_text(f"❌ Ovozni tahlil qilishda xatolik: {e}")
    finally:
        for p in (raw_path, wav_path):
            if os.path.exists(p):
                os.remove(p)

# --- ADMIN PANEL ---
@dp.message(F.text == "👑 Admin Panel")
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("👑 <b>Admin Boshqaruv Paneli:</b>", parse_mode="HTML", reply_markup=admin_inline())

@dp.callback_query(F.data.startswith("adm:"))
async def cb_admin(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    action = call.data.split(":", 1)[1]

    if action == "stats":
        total_clones = sum(len(u.get("cloned_voices", {})) for u in db["users"].values())
        await call.message.answer(
            f"📊 <b>Bot Statistikasi:</b>\n\n"
            f"👥 Foydalanuvchilar: <b>{len(db['users'])} ta</b>\n"
            f"🎙 Yaratilgan ovozlar: <b>{db.get('total_generated', 0)} ta</b>\n"
            f"🧬 Klonlangan ovozlar: <b>{total_clones} ta</b>\n"
            f"📢 Majburiy kanallar: <b>{len(db['channels'])} ta</b>",
            parse_mode="HTML"
        )
    elif action == "broadcast":
        await state.set_state(BotStates.admin_broadcast)
        await call.message.answer("📢 Barcha foydalanuvchilarga yuboriladigan xabarni yuboring:")
    elif action == "add_ch":
        await state.set_state(BotStates.admin_add_channel)
        await call.message.answer("➕ Kanal usernameni yuboring (masalan: <code>@kanal_nomi</code>). Bot kanalda admin bo'lishi shart!", parse_mode="HTML")
    elif action == "list_ch":
        if not db["channels"]:
            return await call.answer("Majburiy kanallar yo'q", show_alert=True)
        btns = [[InlineKeyboardButton(text=f"❌ O'chirish: {ch}", callback_data=f"delch:{ch}")] for ch in db["channels"]]
        await call.message.answer("📢 Majburiy obuna kanallari:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    await call.answer()

@dp.callback_query(F.data.startswith("delch:"))
async def cb_del_channel(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    ch = call.data.split(":", 1)[1]
    if ch in db["channels"]:
        db["channels"].remove(ch)
        save_db(db)
    await call.message.edit_text(f"✅ {ch} ro'yxatdan o'chirildi.")

@dp.message(BotStates.admin_add_channel, F.text)
async def adm_save_channel(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    ch = message.text.strip()
    if not ch.startswith("@"):
        ch = "@" + ch
    if ch not in db["channels"]:
        db["channels"].append(ch)
        save_db(db)
    await state.clear()
    await message.answer(f"✅ {ch} majburiy obunaga qo'shildi!", reply_markup=main_menu(ADMIN_ID))

@dp.message(BotStates.admin_broadcast)
async def adm_do_broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    users = list(db["users"].keys())
    sent = 0
    status_msg = await message.answer(f"⏳ Xabar {len(users)} ta foydalanuvchiga yuborilmoqda...")
    for uid in users:
        try:
            await message.copy_to(chat_id=int(uid))
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await status_msg.edit_text(f"✅ Xabar <b>{sent}</b> ta foydalanuvchiga muvaffaqiyatli yetkazildi!", parse_mode="HTML")

# --- ASOSIY MATNNI OVOZGA AYLANTIRISH ---
@dp.message(F.text)
async def text_to_voice_handler(message: types.Message):
    if message.text.startswith("/"):
        return

    not_sub = await check_subs(message.from_user.id)
    if not_sub:
        btns = [[InlineKeyboardButton(text=f"📢 Obuna bo'lish ({ch})", url=f"https://t.me/{ch.replace('@', '')}")] for ch in not_sub]
        btns.append([InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_sub")])
        return await message.answer("Avval kanallarga obuna bo'ling:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

    text = message.text.strip()
    if len(text) > 2500:
        return await message.answer("⚠️ Matn juda uzun! Iltimos, 2500 ta belgidan kamroq matn yuboring.")

    # O'zbekcha apostroflarni to'g'rilash
    clean_text = text.replace("'", "ʼ").replace("‘", "ʼ").replace("’", "ʼ")

    user = get_user(message.from_user.id, message.from_user.full_name)
    voice_choice = user.get("voice", "uz_female")
    uid = message.from_user.id
    ts = int(time.time() * 1000)
    mp3_file = f"tts_{uid}_{ts}.mp3"
    ogg_file = f"tts_{uid}_{ts}.ogg"

    await bot.send_chat_action(chat_id=message.chat.id, action="record_voice")

    try:
        if voice_choice.startswith("clone:"):
            cname = voice_choice.split(":", 1)[1]
            profile = user.get("cloned_voices", {}).get(cname)
            if not profile:
                return await message.answer("⚠️ Bu klonlangan ovoz topilmadi. Boshqa ovoz tanlang.")

            base_voice = "uz-UZ-MadinaNeural" if profile["gender"] == "female" else "uz-UZ-SardorNeural"
            user_rate = user.get("rate", "+0%")
            communicate = edge_tts.Communicate(
                text=clean_text,
                voice=base_voice,
                rate=user_rate,
                pitch=profile["pitch"]
            )
            await communicate.save(mp3_file)
            process_cloned_audio(mp3_file, ogg_file, profile)
            caption_voice = f"🧬 {cname} (Klonlangan ovoz)"
        else:
            v_info = VOICES.get(voice_choice, VOICES["uz_female"])
            communicate = edge_tts.Communicate(
                text=clean_text,
                voice=v_info["id"],
                rate=user.get("rate", "+0%"),
                pitch=user.get("pitch", "+0Hz")
            )
            await communicate.save(mp3_file)
            convert_to_ogg(mp3_file, ogg_file)
            caption_voice = v_info["name"]

        db["total_generated"] = db.get("total_generated", 0) + 1
        save_db(db)

        await message.answer_voice(
            voice=FSInputFile(ogg_file),
            caption=f"🎙 <b>{caption_voice}</b>",
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"TTS Error: {e}")
        await message.answer("❌ Ovoz yaratishda texnik xatolik yuz berdi. Qaytadan urinib ko'ring.")
    finally:
        for fpath in (mp3_file, ogg_file):
            if os.path.exists(fpath):
                os.remove(fpath)

@dp.callback_query(F.data == "ignore")
async def cb_ignore(call: types.CallbackQuery):
    await call.answer()

# --- RENDER UCHUN FLASK SERVER VA BOT ISHGA TUSHIRISH ---
app = Flask(__name__)

@app.route("/")
def index():
    return "Voice TTS & Free Cloning Bot is running!", 200

def run_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(bot.delete_webhook(drop_pending_updates=True))
    loop.run_until_complete(dp.start_polling(bot, handle_signals=False))

threading.Thread(target=run_bot, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
