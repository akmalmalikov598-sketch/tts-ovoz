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

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip() or 0)
DEFAULT_FISH_KEY = os.getenv("FISH_API_KEY", "").strip()

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN topilmadi!")

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
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
        "fish_key": DEFAULT_FISH_KEY,
        "total_generated": 0
    }

def save_db(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

db = load_db()

VOICES = {
    "uz_female": {"id": "uz-UZ-MadinaNeural", "name": "🇺🇿 Madina (Ayol)"},
    "uz_male": {"id": "uz-UZ-SardorNeural", "name": "🇺🇿 Sardor (Erkak)"},
    "ru_female": {"id": "ru-RU-SvetlanaNeural", "name": "🇷🇺 Svetlana (Ayol)"},
    "ru_male": {"id": "ru-RU-DmitryNeural", "name": "🇷🇺 Dmitry (Erkak)"},
    "en_female": {"id": "en-US-JennyNeural", "name": "🇺🇸 Jenny (Ayol)"},
    "en_male": {"id": "en-US-GuyNeural", "name": "🇺🇸 Guy (Erkak)"},
    "tr_female": {"id": "tr-TR-EmelNeural", "name": "🇹🇷 Emel (Ayol)"},
    "tr_male": {"id": "tr-TR-AhmetNeural", "name": "🇹🇷 Ahmet (Erkak)"},
}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class BotStates(StatesGroup):
    waiting_voice_sample = State()
    waiting_voice_name = State()
    admin_add_channel = State()
    admin_broadcast = State()
    admin_set_key = State()

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
    subprocess.run(
        [FFMPEG_PATH, "-y", "-i", input_path, "-c:a", "libopus", "-b:a", "64k", "-vbr", "on", output_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
    )

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
        for cname, cid in customs.items():
            mark = "✅ " if user_data["voice"] == f"fish:{cid}" else ""
            buttons.append([
                InlineKeyboardButton(text=f"{mark}🎤 {cname}", callback_data=f"setvoice:fish:{cid}"),
                InlineKeyboardButton(text="🗑", callback_data=f"delcustom:{cid}")
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
        [InlineKeyboardButton(text="📢 Xabar tarqatish", callback_data="adm:broadcast")],
        [
            InlineKeyboardButton(text="➕ Kanal qo'shish", callback_data="adm:add_ch"),
            InlineKeyboardButton(text="➖ Kanallarni o'chirish", callback_data="adm:list_ch")
        ],
        [InlineKeyboardButton(text="🔑 Fish Audio API kalit kiritish", callback_data="adm:set_key")]
    ])

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    get_user(message.from_user.id, message.from_user.full_name)
    not_sub = await check_subs(message.from_user.id)
    if not_sub:
        btns = [[InlineKeyboardButton(text=f"📢 Obuna bo'lish ({ch})", url=f"https://t.me/{ch.replace('@', '')}")] for ch in not_sub]
        btns.append([InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_sub")])
        return await message.answer("Botdan foydalanish uchun kanallarga obuna bo'ling:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

    await message.answer(
        f"👋 Assalomu alaykum, <b>{message.from_user.first_name}</b>!\n\n"
        "Matn yuboring — tabiiy erkak va ayol ovozlarida yoki o'zingizning klonlangan ovozingizda "
        "<b>ovozli xabar</b> qilib qaytaraman!",
        parse_mode="HTML",
        reply_markup=main_menu(message.from_user.id)
    )

@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(call: types.CallbackQuery):
    not_sub = await check_subs(call.from_user.id)
    if not_sub:
        return await call.answer("❌ Hali barcha kanallarga obuna bo'lmadingiz!", show_alert=True)
    await call.message.delete()
    await call.message.answer("✅ Obuna tasdiqlandi!", reply_markup=main_menu(call.from_user.id))

@dp.message(F.text == "🎙 Ovozni tanlash")
async def menu_voices(message: types.Message):
    user = get_user(message.from_user.id, message.from_user.full_name)
    await message.answer("🗣 Ovozni tanlang:", reply_markup=voices_inline(user))

@dp.callback_query(F.data.startswith("setvoice:"))
async def cb_set_voice(call: types.CallbackQuery):
    user = get_user(call.from_user.id)
    user["voice"] = call.data.split("setvoice:", 1)[1]
    save_db(db)
    await call.message.edit_reply_markup(reply_markup=voices_inline(user))
    await call.answer("✅ Ovoz tanlandi!")

@dp.callback_query(F.data.startswith("delcustom:"))
async def cb_del_custom(call: types.CallbackQuery):
    user = get_user(call.from_user.id)
    cid = call.data.split("delcustom:", 1)[1]
    customs = user.get("custom_voices", {})
    for k in [k for k, v in customs.items() if v == cid]:
        del customs[k]
    if user["voice"] == f"fish:{cid}":
        user["voice"] = "uz_female"
    save_db(db)
    if db.get("fish_key"):
        try:
            requests.delete(f"https://api.fish.audio/model/{cid}", headers={"Authorization": f"Bearer {db['fish_key']}"}, timeout=10)
        except Exception:
            pass
    await call.message.edit_reply_markup(reply_markup=voices_inline(user))
    await call.answer("🗑 O'chirildi")

@dp.message(F.text == "⚙️ Tezlik va Ton")
async def menu_settings(message: types.Message):
    user = get_user(message.from_user.id)
    await message.answer("⚙️ <b>Tezlik va ton sozlamalari:</b>", parse_mode="HTML", reply_markup=settings_inline(user))

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
    await call.answer("✅ Ton saqlandi")

@dp.message(F.text == "ℹ️ Mening sozlamalarim")
async def menu_info(message: types.Message):
    user = get_user(message.from_user.id)
    v = user["voice"]
    if v.startswith("fish:"):
        cid = v.split(":", 1)[1]
        cname = next((k for k, val in user.get("custom_voices", {}).items() if val == cid), "Klonlangan ovoz")
        v_name = f"🧬 {cname}"
    else:
        v_name = VOICES.get(v, VOICES["uz_female"])["name"]
    await message.answer(
        f"🎙 Tanlangan ovoz: <b>{v_name}</b>\n"
        f"⏩ Tezlik: <b>{user.get('rate', '+0%')}</b>\n"
        f"🎵 Ton: <b>{user.get('pitch', '+0Hz')}</b>",
        parse_mode="HTML"
    )

# --- FISH AUDIO RASMIY API ORQALI OVOZ KLONLASH ---
@dp.message(F.text == "🧬 O'z ovozimni klonlash")
async def start_clone(message: types.Message, state: FSMContext):
    if not db.get("fish_key"):
        return await message.answer(
            "⚠️ Ovoz klonlash uchun Admin Panelga <b>Fish Audio</b> bepul API kalitini kiriting (<code>fish.audio</code> saytidan olinadi).\n"
            "Ungacha <b>🎙 Ovozni tanlash</b> orqali bepul ovozlardan foydalanishingiz mumkin.",
            parse_mode="HTML"
        )
    await state.set_state(BotStates.waiting_voice_sample)
    await message.answer(
        "🎤 Shovqinsiz joyda <b>10–30 soniya</b> aniq gapirib <b>ovozli xabar (voice)</b> yoki <b>audio</b> yuboring.\n"
        "Bekor qilish: /start",
        parse_mode="HTML"
    )

@dp.message(BotStates.waiting_voice_sample, F.voice | F.audio)
async def receive_voice_sample(message: types.Message, state: FSMContext):
    file_id = message.voice.file_id if message.voice else message.audio.file_id
    duration = message.voice.duration if message.voice else (message.audio.duration or 15)
    if duration < 5:
        return await message.answer("⚠️ Kamida 8-10 soniyalik ovoz yuboring.")
    await state.update_data(sample_file_id=file_id)
    await state.set_state(BotStates.waiting_voice_name)
    await message.answer("✍️ Ovozga nom yozing (masalan: <i>Akmal</i>):", parse_mode="HTML")

@dp.message(BotStates.waiting_voice_name, F.text)
async def receive_voice_name(message: types.Message, state: FSMContext):
    voice_name = message.text.strip()[:25]
    data = await state.get_data()
    file_id = data.get("sample_file_id")
    await state.clear()

    wait_msg = await message.answer("⏳ Ovoz modeli yaratilmoqda...")
    uid = message.from_user.id
    raw_path = f"sample_{uid}_{int(time.time())}.ogg"
    mp3_path = f"sample_{uid}_{int(time.time())}.mp3"

    try:
        tg_file = await bot.get_file(file_id)
        await bot.download_file(tg_file.file_path, raw_path)
        subprocess.run([FFMPEG_PATH, "-y", "-i", raw_path, mp3_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        with open(mp3_path, "rb") as f_audio:
            resp = requests.post(
                "https://api.fish.audio/model",
                headers={"Authorization": f"Bearer {db['fish_key']}"},
                data={
                    "visibility": "private",
                    "type": "tts",
                    "title": f"{uid}_{voice_name}",
                    "train_mode": "fast"
                },
                files={"voices": (mp3_path, f_audio, "audio/mpeg")},
                timeout=30
            )

        if resp.status_code in (200, 201):
            model_id = resp.json().get("_id") or resp.json().get("id")
            user = get_user(uid)
            user.setdefault("custom_voices", {})[voice_name] = model_id
            user["voice"] = f"fish:{model_id}"
            save_db(db)
            await wait_msg.edit_text(
                f"✅ <b>«{voice_name}» ovozi tayyor va tanlandi!</b>\nEndi matn yuboring — o'z ovozingizda o'qiydi.",
                parse_mode="HTML"
            )
        else:
            await wait_msg.edit_text(f"❌ API xatosi ({resp.status_code}): {resp.text[:200]}")
    except Exception as e:
        await wait_msg.edit_text(f"❌ Xatolik: {e}")
    finally:
        for p in (raw_path, mp3_path):
            if os.path.exists(p):
                os.remove(p)

# --- ADMIN PANEL ---
@dp.message(F.text == "👑 Admin Panel")
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("👑 <b>Admin Panel:</b>", parse_mode="HTML", reply_markup=admin_inline())

@dp.callback_query(F.data.startswith("adm:"))
async def cb_admin(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    action = call.data.split(":", 1)[1]
    if action == "stats":
        st = "✅ Ulangan" if db.get("fish_key") else "❌ Kiritilmagan"
        await call.message.answer(
            f"👥 Foydalanuvchilar: <b>{len(db['users'])}</b>\n"
            f"🎙 Yaratilgan ovozlar: <b>{db.get('total_generated', 0)}</b>\n"
            f"📢 Kanallar: <b>{len(db['channels'])}</b>\n"
            f"🔑 Fish Audio API: <b>{st}</b>",
            parse_mode="HTML"
        )
    elif action == "broadcast":
        await state.set_state(BotStates.admin_broadcast)
        await call.message.answer("📢 Tarqatiladigan xabarni yuboring:")
    elif action == "add_ch":
        await state.set_state(BotStates.admin_add_channel)
        await call.message.answer("➕ Kanal usernameni yuboring (<code>@kanal</code>):", parse_mode="HTML")
    elif action == "list_ch":
        if not db["channels"]:
            return await call.answer("Kanallar yo'q", show_alert=True)
        btns = [[InlineKeyboardButton(text=f"❌ {ch}", callback_data=f"delch:{ch}")] for ch in db["channels"]]
        await call.message.answer("Kanallar:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    elif action == "set_key":
        await state.set_state(BotStates.admin_set_key)
        await call.message.answer("🔑 <b>fish.audio</b> saytidan olingan API kalitni yuboring (o'chirish: <code>none</code>):", parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data.startswith("delch:"))
async def cb_del_channel(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    ch = call.data.split(":", 1)[1]
    if ch in db["channels"]:
        db["channels"].remove(ch)
        save_db(db)
    await call.message.edit_text(f"✅ {ch} o'chirildi.")

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
    await message.answer(f"✅ {ch} qo'shildi!", reply_markup=main_menu(ADMIN_ID))

@dp.message(BotStates.admin_set_key, F.text)
async def adm_save_key(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    val = message.text.strip()
    db["fish_key"] = "" if val.lower() == "none" else val
    save_db(db)
    await state.clear()
    await message.answer("✅ Fish Audio API kaliti saqlandi!", reply_markup=main_menu(ADMIN_ID))

@dp.message(BotStates.admin_broadcast)
async def adm_do_broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    sent = 0
    for uid in list(db["users"].keys()):
        try:
            await message.copy_to(chat_id=int(uid))
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await message.answer(f"✅ <b>{sent}</b> ta foydalanuvchiga yuborildi!", parse_mode="HTML")

# --- MATNNI OVOZGA AYLANTIRISH ---
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
        return await message.answer("⚠️ Matn 2500 belgidan oshmasligi kerak.")

    clean_text = text.replace("'", "ʼ").replace("‘", "ʼ").replace("’", "ʼ")
    user = get_user(message.from_user.id, message.from_user.full_name)
    voice_choice = user.get("voice", "uz_female")
    uid = message.from_user.id
    ts = int(time.time() * 1000)
    mp3_file = f"tts_{uid}_{ts}.mp3"
    ogg_file = f"tts_{uid}_{ts}.ogg"

    await bot.send_chat_action(chat_id=message.chat.id, action="record_voice")

    try:
        if voice_choice.startswith("fish:"):
            model_id = voice_choice.split(":", 1)[1]
            api_key = db.get("fish_key", "")
            if not api_key:
                return await message.answer("⚠️ Fish Audio API kaliti kiritilmagan.")

            resp = requests.post(
                "https://api.fish.audio/v1/tts",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "text": clean_text,
                    "reference_id": model_id,
                    "format": "mp3"
                },
                timeout=30
            )
            if resp.status_code != 200:
                return await message.answer(f"❌ Xatolik ({resp.status_code}): {resp.text[:150]}")
            with open(mp3_file, "wb") as f:
                f.write(resp.content)
            caption_voice = "🧬 Klonlangan ovoz"
        else:
            v_info = VOICES.get(voice_choice, VOICES["uz_female"])
            communicate = edge_tts.Communicate(
                text=clean_text,
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
        await message.answer("❌ Xatolik yuz berdi. Qaytadan urinib ko'ring.")
    finally:
        for fpath in (mp3_file, ogg_file):
            if os.path.exists(fpath):
                os.remove(fpath)

@dp.callback_query(F.data == "ignore")
async def cb_ignore(call: types.CallbackQuery):
    await call.answer()

app = Flask(__name__)

@app.route("/")
def index():
    return "Bot is running!", 200

def run_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(bot.delete_webhook(drop_pending_updates=True))
    loop.run_until_complete(dp.start_polling(bot, handle_signals=False))

threading.Thread(target=run_bot, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
