import os
import json
import time
import asyncio
import logging
import subprocess
import threading
import requests
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
DEFAULT_ELEVEN_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN topilmadi! Render Environment Variables ga qo'shing.")

# --- FFMPEG YO'LI ---
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
        "elevenlabs_key": DEFAULT_ELEVEN_KEY,
        "total_generated": 0
    }

def save_db(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

db = load_db()

# --- STANDART OVOZLAR RO'YXATI ---
VOICES = {
    "uz_female": {"id": "uz-UZ-MadinaNeural", "name": "🇺🇿 Madina (Ayol)", "lang": "O'zbek"},
    "uz_male": {"id": "uz-UZ-SardorNeural", "name": "🇺🇿 Sardor (Erkak)", "lang": "O'zbek"},
    "ru_female": {"id": "ru-RU-SvetlanaNeural", "name": "🇷🇺 Svetlana (Ayol)", "lang": "Rus"},
    "ru_male": {"id": "ru-RU-DmitryNeural", "name": "🇷🇺 Dmitry (Erkak)", "lang": "Rus"},
    "en_female": {"id": "en-US-JennyNeural", "name": "🇺🇸 Jenny (Ayol)", "lang": "Ingliz"},
    "en_male": {"id": "en-US-GuyNeural", "name": "🇺🇸 Guy (Erkak)", "lang": "Ingliz"},
    "tr_female": {"id": "tr-TR-EmelNeural", "name": "🇹🇷 Emel (Ayol)", "lang": "Turk"},
    "tr_male": {"id": "tr-TR-AhmetNeural", "name": "🇹🇷 Ahmet (Erkak)", "lang": "Turk"},
}

# --- BOT VA DISPATCHER ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- HOLATLAR (FSM) ---
class BotStates(StatesGroup):
    waiting_voice_sample = State()
    waiting_voice_name = State()
    admin_add_channel = State()
    admin_broadcast = State()
    admin_set_eleven_key = State()

# --- YORDAMCHI FUNKSIYALAR ---
def get_user(user_id: int, full_name: str = "Foydalanuvchi"):
    uid = str(user_id)
    if uid not in db["users"]:
        db["users"][uid] = {
            "name": full_name,
            "voice": "uz_female",
            "rate": "+0%",
            "pitch": "+0Hz",
            "custom_voices": {}
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

def convert_to_ogg(input_path: str, output_path: str):
    cmd = [
        FFMPEG_PATH, "-y", "-i", input_path,
        "-c:a", "libopus", "-b:a", "48k", "-vbr", "on", output_path
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

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

    customs = user_data.get("custom_voices", {})
    if customs:
        buttons.append([InlineKeyboardButton(text="--- 🧬 Mening klonlangan ovozlarim ---", callback_data="ignore")])
        for cname, c_id in customs.items():
            mark = "✅ " if user_data["voice"] == f"custom:{c_id}" else ""
            buttons.append([
                InlineKeyboardButton(text=f"{mark}🎤 {cname}", callback_data=f"setvoice:custom:{c_id}"),
                InlineKeyboardButton(text="🗑", callback_data=f"delcustom:{c_id}")
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
        ],
        [InlineKeyboardButton(text="🔑 ElevenLabs API kalitini kiritish", callback_data="adm:set_key")]
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
        "🎙 <b>Imkoniyatlar:</b>\n"
        "• O'zbek, Rus, Ingliz, Turk tillarida tabiiy erkak va ayol ovozlari\n"
        "• 🧬 <b>O'z ovozingizni klonlash</b> (ovoz yuboring va o'z ovozingizda gapirtiring)\n"
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

@dp.callback_query(F.data.startswith("delcustom:"))
async def cb_del_custom(call: types.CallbackQuery):
    user = get_user(call.from_user.id)
    cid = call.data.split("delcustom:", 1)[1]
    customs = user.get("custom_voices", {})
    to_del = [k for k, v in customs.items() if v == cid]
    for k in to_del:
        del customs[k]
    if user["voice"] == f"custom:{cid}":
        user["voice"] = "uz_female"
    save_db(db)
    if db.get("elevenlabs_key"):
        try:
            requests.delete(f"https://api.elevenlabs.io/v1/voices/{cid}", headers={"xi-api-key": db["elevenlabs_key"]}, timeout=10)
        except Exception:
            pass
    await call.message.edit_reply_markup(reply_markup=voices_inline(user))
    await call.answer("🗑 Ovoz o'chirildi")

@dp.message(F.text == "⚙️ Tezlik va Ton")
async def menu_settings(message: types.Message):
    user = get_user(message.from_user.id)
    await message.answer(
        "⚙️ <b>Ovoz parametrlarini sozlash:</b>\n<i>(Standart ovozlar uchun ishlaydi)</i>",
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
    if v.startswith("custom:"):
        cid = v.split(":", 1)[1]
        cname = next((k for k, val in user.get("custom_voices", {}).items() if val == cid), "Klonlangan ovoz")
        v_name = f"🧬 {cname} (Shaxsiy klon)"
    else:
        v_name = VOICES.get(v, VOICES["uz_female"])["name"]

    await message.answer(
        f"👤 <b>Sizning sozlamalaringiz:</b>\n\n"
        f"🎙 Tanlangan ovoz: <b>{v_name}</b>\n"
        f"⏩ Tezlik: <b>{user.get('rate', '+0%')}</b>\n"
        f"🎵 Ovoz toni: <b>{user.get('pitch', '+0Hz')}</b>\n"
        f"🧬 Klonlangan ovozlar soni: <b>{len(user.get('custom_voices', {}))} ta</b>",
        parse_mode="HTML"
    )

# --- OVOZ KLONLASH (VOICE CLONING) ---
@dp.message(F.text == "🧬 O'z ovozimni klonlash")
async def start_clone(message: types.Message, state: FSMContext):
    if not db.get("elevenlabs_key"):
        return await message.answer(
            "⚠️ Ovoz klonlash funksiyasi hozircha admin tomonidan faollashtirilmagan (API kalit kiritilmagan).\n"
            "Hozircha <b>🎙 Ovozni tanlash</b> bo'limidan bepul tabiiy erkak va ayol ovozlaridan foydalanishingiz mumkin!",
            parse_mode="HTML"
        )
    await state.set_state(BotStates.waiting_voice_sample)
    await message.answer(
        "🎤 <b>O'z ovozingizni klonlash uchun:</b>\n\n"
        "Shovqinsiz joyda <b>15–40 soniya</b> davomida aniq va ravon gapirib, menga <b>ovozli xabar (voice)</b> yoki <b>audio fayl</b> yuboring.\n\n"
        "❌ Bekor qilish uchun /start bosing.",
        parse_mode="HTML"
    )

@dp.message(BotStates.waiting_voice_sample, F.voice | F.audio)
async def receive_voice_sample(message: types.Message, state: FSMContext):
    file_id = message.voice.file_id if message.voice else message.audio.file_id
    duration = message.voice.duration if message.voice else (message.audio.duration or 15)
    if duration < 5:
        return await message.answer("⚠️ Ovoz juda qisqa! Kamida 10-15 soniyalik ovozli xabar yuboring.")

    await state.update_data(sample_file_id=file_id)
    await state.set_state(BotStates.waiting_voice_name)
    await message.answer("✍️ Ajoyib! Endi bu ovozga biror nom bering (masalan: <i>Mening ovozim</i> yoki <i>Akmal</i>):", parse_mode="HTML")

@dp.message(BotStates.waiting_voice_name, F.text)
async def receive_voice_name(message: types.Message, state: FSMContext):
    voice_name = message.text.strip()[:25]
    data = await state.get_data()
    file_id = data.get("sample_file_id")
    await state.clear()

    wait_msg = await message.answer("⏳ Ovozingiz sun'iy intellektga o'rgatilmoqda, biroz kuting...")
    uid = message.from_user.id
    raw_path = f"sample_{uid}_{int(time.time())}.ogg"
    mp3_path = f"sample_{uid}_{int(time.time())}.mp3"

    try:
        tg_file = await bot.get_file(file_id)
        await bot.download_file(tg_file.file_path, raw_path)
        subprocess.run([FFMPEG_PATH, "-y", "-i", raw_path, mp3_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        with open(mp3_path, "rb") as audio_f:
            resp = requests.post(
                "https://api.elevenlabs.io/v1/voices/add",
                headers={"xi-api-key": db["elevenlabs_key"]},
                data={"name": f"{uid}_{voice_name}", "description": "Telegram Bot User Voice"},
                files={"files": (mp3_path, audio_f, "audio/mpeg")},
                timeout=45
            )

        if resp.status_code == 200:
            new_voice_id = resp.json().get("voice_id")
            user = get_user(uid)
            user.setdefault("custom_voices", {})[voice_name] = new_voice_id
            user["voice"] = f"custom:{new_voice_id}"
            save_db(db)
            await wait_msg.edit_text(
                f"✅ <b>Tabriklaymiz! «{voice_name}» ovozi muvaffaqiyatli yaratildi va tanlandi!</b>\n\n"
                "Endi menga istalgan matn yozib yuboring — o'zingizning ovozingizda o'qib beraman!",
                parse_mode="HTML"
            )
        else:
            await wait_msg.edit_text(f"❌ Xatolik yuz berdi (API): {resp.text[:200]}")
    except Exception as e:
        await wait_msg.edit_text(f"❌ Ovozni qayta ishlashda xatolik: {e}")
    finally:
        for p in (raw_path, mp3_path):
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
        key_status = "✅ Ulangan" if db.get("elevenlabs_key") else "❌ Kiritilmagan"
        await call.message.answer(
            f"📊 <b>Bot Statistikasi:</b>\n\n"
            f"👥 Foydalanuvchilar: <b>{len(db['users'])} ta</b>\n"
            f"🎙 Yaratilgan ovozlar: <b>{db.get('total_generated', 0)} ta</b>\n"
            f"📢 Majburiy kanallar: <b>{len(db['channels'])} ta</b>\n"
            f"🔑 ElevenLabs API (Klonlash): <b>{key_status}</b>",
            parse_mode="HTML"
        )
    elif action == "broadcast":
        await state.set_state(BotStates.admin_broadcast)
        await call.message.answer("📢 Barcha foydalanuvchilarga yuboriladigan xabarni (matn, rasm yoki video) yuboring:")
    elif action == "add_ch":
        await state.set_state(BotStates.admin_add_channel)
        await call.message.answer("➕ Kanal usernameni yuboring (masalan: <code>@kanal_nomi</code>). Bot kanalda admin bo'lishi shart!", parse_mode="HTML")
    elif action == "list_ch":
        if not db["channels"]:
            return await call.answer("Majburiy kanallar yo'q", show_alert=True)
        btns = [[InlineKeyboardButton(text=f"❌ O'chirish: {ch}", callback_data=f"delch:{ch}")] for ch in db["channels"]]
        await call.message.answer("📢 Majburiy obuna kanallari:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    elif action == "set_key":
        await state.set_state(BotStates.admin_set_eleven_key)
        await call.message.answer("🔑 ElevenLabs API kalitini yuboring (o'chirish uchun <code>none</code> deb yozing):", parse_mode="HTML")
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

@dp.message(BotStates.admin_set_eleven_key, F.text)
async def adm_save_key(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    val = message.text.strip()
    db["elevenlabs_key"] = "" if val.lower() == "none" else val
    save_db(db)
    await state.clear()
    await message.answer("✅ ElevenLabs API kaliti yangilandi!", reply_markup=main_menu(ADMIN_ID))

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

    user = get_user(message.from_user.id, message.from_user.full_name)
    voice_choice = user.get("voice", "uz_female")
    uid = message.from_user.id
    ts = int(time.time() * 1000)
    mp3_file = f"tts_{uid}_{ts}.mp3"
    ogg_file = f"tts_{uid}_{ts}.ogg"

    await bot.send_chat_action(chat_id=message.chat.id, action="record_voice")

    try:
        if voice_choice.startswith("custom:"):
            custom_id = voice_choice.split(":", 1)[1]
            api_key = db.get("elevenlabs_key", "")
            if not api_key:
                return await message.answer("⚠️ Klonlangan ovoz API kaliti o'chirilgan. Boshqa ovoz tanlang.")

            resp = requests.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{custom_id}",
                headers={"xi-api-key": api_key, "Content-Type": "application/json"},
                json={
                    "text": text,
                    "model_id": "eleven_multilingual_v2",
                    "voice_settings": {"stability": 0.5, "similarity_boost": 0.8}
                },
                timeout=45
            )
            if resp.status_code != 200:
                return await message.answer(f"❌ Ovoz yaratishda xatolik: {resp.text[:150]}")
            with open(mp3_file, "wb") as f:
                f.write(resp.content)
            caption_voice = "🧬 Shaxsiy klonlangan ovoz"
        else:
            v_info = VOICES.get(voice_choice, VOICES["uz_female"])
            communicate = edge_tts.Communicate(
                text=text,
                voice=v_info["id"],
                rate=user.get("rate", "+0%"),
                pitch=user.get("pitch", "+0Hz")
            )
            await communicate.save(mp3_file)
            caption_voice = v_info["name"]

        convert_to_ogg(mp3_file, ogg_file)

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
    return "Voice TTS & Cloning Bot is running!", 200

def run_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(bot.delete_webhook(drop_pending_updates=True))
    loop.run_until_complete(dp.start_polling(bot, handle_signals=False))

threading.Thread(target=run_bot, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
