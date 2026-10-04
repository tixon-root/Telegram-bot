import os
import re
import time
import json
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote
import telebot
from telebot import types
from flask import Flask, request, jsonify
from threading import Thread
from pymongo import MongoClient
from flask_cors import CORS  
import datetime

# Глобальные переменные для состояний
transfer_states = {}
last_transfer = {}  # uid -> datetime последнего перевода
user_states = {}     # uid -> {target_id, action, amount}
jewel_user_states = {}  # uid -> {target_id, stage}
admin_states = {}    # uid -> {action, target_id}
HEADERS = {"User-Agent": "Mozilla/5.0"}

# ---------------- Конфигурация ----------------
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 6395348885  # ← ЗАМЕНИ НА СВОЙ TELEGRAM ID (узнать можно через @userinfobot)
YETI_BOT_ID = os.getenv("YETI_BOT_ID", "YOUR_YETI_BOT_ID")
SCAM_FILE = "scammers.json"

if not TOKEN:
    raise ValueError("BOT_TOKEN не найден! Проверь переменные окружения Render.")

bot = telebot.TeleBot(TOKEN, parse_mode=None)

# --- Настройка подключения к БД ---
try:
    mongo_uri = os.getenv("MONGO_URI")
    if not mongo_uri:
        print("⚠️ Переменная MONGO_URI не задана!")
        bank_db = None
    else:
        mongo = MongoClient(mongo_uri, serverSelectionTimeoutMS=15000)
        mongo.admin.command('ping')
        db = mongo["rucoy"]
        bank_db = db["bank"]
        print("✅ ПОДКЛЮЧЕНО: База данных rucoy готова к работе!")
except Exception as e:
    print(f"❌ ОШИБКА ПОДКЛЮЧЕНИЯ: {e}")
    bank_db = None

# Функции для работы с балансом
def get_balance(uid):
    if bank_db is None: 
        return "db_error"
    try:
        user_data = bank_db.find_one({"uid": str(uid)})
        return user_data.get("balance", 0) if user_data else 0
    except Exception as e:
        print(f"Ошибка БД: {e}")
        return "query_error"

def add_balance(uid, amount):
    if bank_db is None: 
        return False
    try:
        bank_db.update_one(
            {"uid": str(uid)}, 
            {"$inc": {"balance": int(amount)}}, 
            upsert=True
        )
        return True
    except Exception as e:
        print(f"Ошибка в add_balance: {e}")
        return False

def set_balance(uid, amount):
    if bank_db is None: 
        return False
    try:
        bank_db.update_one(
            {"uid": str(uid)},
            {"$set": {"balance": int(amount)}},
            upsert=True
        )
        return True
    except Exception as e:
        print(f"Ошибка в set_balance: {e}")
        return False

def get_player_name(uid):
    """Получить имя игрока из БД"""
    if bank_db is None:
        return str(uid)
    try:
        user_data = bank_db.find_one({"uid": str(uid)})
        return user_data.get("name", str(uid)) if user_data else str(uid)
    except:
        return str(uid)

def ban_user(uid, ban=True):
    """Забанить/разбанить пользователя"""
    if bank_db is None:
        return False
    try:
        bank_db.update_one(
            {"uid": str(uid)},
            {"$set": {"banned": ban}},
            upsert=True
        )
        return True
    except:
        return False

def is_banned(uid):
    """Проверка на бан"""
    if bank_db is None:
        return False
    try:
        user_data = bank_db.find_one({"uid": str(uid)})
        return user_data.get("banned", False) if user_data else False
    except:
        return False

# Ссылка на баннер для меню /start
START_BANNER = "https://i.ibb.co/5X2W2c8q/e9a3f45d2f734f9126820cdca7b55266.jpg"

# ---------------- SCAM STORAGE ----------------
def load_scammers():
    try:
        if os.path.exists(SCAM_FILE):
            with open(SCAM_FILE, "r", encoding="utf-8") as f:
                data = f.read().strip()
                if data:
                    return json.loads(data)
    except:
        pass
    return {}

def save_scammers(data):
    with open(SCAM_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

# ---------------- RENDER & API SETTINGS ----------------
app = Flask('')
CORS(app)

@app.route('/health')
def health_check():
    return "ok", 200


def keep_alive():
    render_url = os.getenv("RENDER_EXTERNAL_URL")
    if not render_url:
        return
    while True:
        try:
            requests.get(f"{render_url}/health", timeout=10)
        except Exception as e:
            print(f"⚠️ Keep-alive ping error: {e}")
        time.sleep(300)


@app.route('/')
def home():
    return "Bot is alive!"

@app.route('/api/search', methods=['GET'])
def api_search():
    try:
        name = request.args.get('name', '').strip()
        stype = request.args.get('type', 'player')

        if not name:
            return jsonify({"error": "Введите имя"}), 400

        safe_name = quote(name)
        url = (
            f"https://www.rucoyonline.com/guild/{safe_name}"
            if stype == "guild"
            else f"https://www.rucoyonline.com/characters/{safe_name}"
        )

        r = requests.get(url, headers=HEADERS, timeout=5)

        if r.status_code != 200:
            return jsonify({"error": "Не найдено"}), 404

        soup = BeautifulSoup(r.text, "html.parser")

        # ── GUILD ──────────────────────────────────────────
        if stype == "guild":
            rows = soup.find_all("tr")
            members = sum(1 for row in rows if row.find_all("td"))

            # Дата создания
            created_at = "Неизвестно"
            text = soup.get_text("\n", strip=True)
            m = re.search(r"Founded on[:\s]+(.+)", text, re.I)
            if m:
                created_at = m.group(1).strip()

            # Описание гильдии
            description = extract_description(soup, name)

            return jsonify({
                "type": "guild",
                "name": name,
                "members": members,
                "created_at": created_at,
                "description": description,
                "url": url
            })

        # ── PLAYER ─────────────────────────────────────────
        table = soup.find("table")
        if not table:
            return jsonify({"error": "Персонаж скрыт или не существует"}), 404

        data = {}
        for tr in table.find_all("tr"):
            td = tr.find_all("td")
            if len(td) == 2:
                data[td[0].text.strip()] = td[1].text.strip()

        return jsonify({
            "type": "player",
            "name": data.get("Name", name),
            "level": data.get("Level", "?"),
            "guild": data.get("Guild", "None"),
            "online": data.get("Last online", "?"),
            "born": data.get("Born", ""),          # ← ДОБАВЛЕНО
            "url": url
        })

    except Exception as e:
        print(f"Ошибка API: {e}")
        return jsonify({"error": f"Internal error: {str(e)}"}), 500


# ---------------- PARSING ----------------
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

MENU_WORDS = {
    "le navigation", "rucoy online", "welcome", "news", "highscores",
    "characters", "guilds", "sign in", "sign in with google", "sign in with apple"
}

def remove_adjacent_duplicates(lines):
    out = []
    for l in lines:
        if not out or l != out[-1]:
            out.append(l)
    return out

def remove_repeated_block(lines):
    n = len(lines)
    for k in range(1, n // 2 + 1):
        if lines[:k] == lines[k:2 * k]:
            return lines[k:]
    return lines

def extract_description(soup, name):
    text = soup.get_text("\n", strip=True)
    idx = text.lower().find(name.lower())
    chunk = ""
    if idx != -1:
        start = idx + len(name)
        m = re.search(r"(Founded on|Members)", text[start:], re.I)
        end = start + m.start() if m else len(text)
        chunk = text[start:end]
    if not chunk:
        return "Нет описания"
    lines = [l.strip() for l in chunk.split("\n") if l.strip()]
    lines = [l for l in lines if not any(m in l.lower() for m in MENU_WORDS)]
    lines = remove_adjacent_duplicates(lines)
    lines = remove_repeated_block(lines)
    return "\n".join(lines) if lines else "Нет описания"

# ---------------- YETI JEWEL TRANSFER ----------------
def send_jewel_via_yeti(target_id, amount, source_id=None, admin_mode=False):
    """
    Отправка Jewels через другого бота YETI.
    Формат команды пример: /give_jewel 123456 500
    """
    if str(YETI_BOT_ID).strip() in ("", "YOUR_YETI_BOT_ID"):
        return False, "❌ YETI_BOT_ID не настроен. Укажи ID бота YETI в переменной окружения."

    try:
        payload = f"/give_jewel {target_id} {amount}"
        if source_id:
            payload = f"{payload} from={source_id}"

        bot.send_message(YETI_BOT_ID, payload)

        if admin_mode:
            return True, f"✅ Команда на выдачу {amount} Jewel пользователю {target_id} отправлена в YETI."
        return True, f"✅ Запрос на перевод {amount} Jewel пользователю {target_id} отправлен в YETI."
    except Exception as e:
        print(f"Ошибка при отправке Jewel в YETI: {e}")
        return False, f"❌ Ошибка отправки Jewel через YETI: {str(e)}"

# ---------------- COMMANDS ----------------

@bot.message_handler(commands=['start'])
def send_start(message):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("🏦 Мой Банк", callback_data="open_bank"))
    
    kb.add(types.InlineKeyboardButton("➕ Добавить в группу", url="https://t.me/rucoy_online_robot?startgroup=interface"))
    kb.add(
        types.InlineKeyboardButton("💬 Rucoy Chat", url="https://t.me/Bancus_Rucoy/13"),
        types.InlineKeyboardButton("🛒 Rucoy Market", url="https://t.me/Bancus_Rucoy/4")
    )
    kb.add(types.InlineKeyboardButton("🧮 Calculator", callback_data="calc"))
    kb.add(
        types.InlineKeyboardButton("💰 Купить Gold", callback_data="buy_gold"),
        types.InlineKeyboardButton("📤 Продать Gold", url="https://t.me/Bancus_Rucoy/159")
    )
    kb.add(types.InlineKeyboardButton("ℹ️ Информация", callback_data="info"))

    bot.send_photo(
        message.chat.id,
        START_BANNER,
        caption="⚔️ *Rucoy Hub*\n\nВыберите раздел:",
        parse_mode="Markdown",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data == "open_bank")
def open_bank_callback(call):
    bot.answer_callback_query(call.id)
    # Отправляем в личку
    try:
        bot.send_message(call.from_user.id, "Открываю ваш банк...")
        bank_profile_direct(call.from_user.id)
    except:
        bot.answer_callback_query(call.id, "❌ Напишите боту в личку: /bank", show_alert=True)

@bot.message_handler(commands=['gold'])
def gold_command(message):
    try:
        text = (
            "💰 *Курс Gold на данный момент*\n\n"
            "🇷🇺 *Россия (RUB)*\n"
            "`20₽ = 1kk Gold`\n\n"
            "🇺🇦 *Украина (UAH)*\n"
            "`11₴ = 1kk Gold`\n\n"
            "🇰🇿 *Казахстан (KZT)*\n"
            "`130₸ = 1kk Gold`\n\n"
            "🇧🇾 *Беларусь (BYN)*\n"
            "`0.8 BYN = 1kk Gold`\n\n"
            "🇺🇸 *USD*\n"
            "`$0.3 = 1kk Gold`\n"
        )
        bot.reply_to(message, text, parse_mode="Markdown")
    except Exception as e:
        print("Ошибка в команде /gold:", e)
        bot.reply_to(message, "❌ Не удалось отправить информацию о ценах на Gold")

@bot.callback_query_handler(func=lambda c: c.data == "calc")
def send_calculator(call):
    try:
        bot.forward_message(call.message.chat.id, "@rucoy_calculyator", 2)
    except Exception as e:
        bot.send_message(call.message.chat.id, f"❌ Не удалось переслать файл: {e}")

@bot.callback_query_handler(func=lambda c: c.data == "info")
def info_callback(call):
    bot.send_message(
        call.message.chat.id,
        "ℹ️ *Информация*\n\n📌 Команды:\n`/guild` — информация о гильдии\n`/user` — информация об игроке\n`/skam` — список",
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda c: c.data == "buy_gold")
def buy_gold_menu(call):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📊 Курсы и топ-трейдеры", callback_data="gold_rates"))
    kb.add(types.InlineKeyboardButton("➕ Ещё", url="https://t.me/Bancus_Rucoy/159"))

    try:
        bot.send_message(
            call.message.chat.id,
            "💰 *Курс Gold на данный момент*\n\n🇷🇺 Россия (RUB)\n`20₽ = 1kk Gold`\n\n🇺🇦 Украина (UAH)\n`11₴ = 1kk Gold`\n\n🇰🇿 Казахстан",
            parse_mode="Markdown",
            reply_markup=kb
        )
        bot.answer_callback_query(call.id)
    except Exception as e:
        print(f"Error in buy_gold: {e}")

@bot.callback_query_handler(func=lambda c: c.data == "gold_rates")
def gold_rates(call):
    bot.send_message(
        call.message.chat.id,
        "📊 *Курсы Gold*\n\nЛучшие трейдеры и цены скоро здесь.",
        parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id)

# -------- BANK SYSTEM (ИСПРАВЛЕНО) --------

def bank_profile_direct(user_id):
    """Отправка профиля банка напрямую по user_id"""
    uid = str(user_id)
    
    # Получаем имя пользователя
    try:
        user = bot.get_chat(user_id)
        name = user.first_name if user.first_name else "Player"
    except:
        name = "Player"
    
    # Проверка на бан
    if is_banned(uid):
        return bot.send_message(user_id, "🚫 Вы заблокированы в банковской системе.")

    if bank_db is not None:
        bank_db.update_one({"uid": uid}, {"$set": {"name": name}}, upsert=True)
    
    balance = get_balance(uid)
    if balance in ["db_error", "query_error"]:
        return bot.send_message(user_id, "❌ Ошибка базы данных.")

    kb = types.InlineKeyboardMarkup(row_width=2)
    url_deposit = f"https://t.me/herozvz?text=Я%20хочу%20пополнить%20счёт%20(ID:%20{uid})"
    url_withdraw = f"https://t.me/herozvz?text=Я%20хочу%20вывести%20gold%20(ID:%20{uid})"
    
    kb.add(
        types.InlineKeyboardButton("➕ Пополнить", url=url_deposit),
        types.InlineKeyboardButton("📤 Вывод", url=url_withdraw)
    )
    kb.add(types.InlineKeyboardButton("💸 Отправить Gold", callback_data="start_gift_btn"))
    kb.add(types.InlineKeyboardButton("💎 Отправить Jewel", callback_data="start_jewel_btn"))
    
    text = (
        "🏦 *Rucoy Bank*\n\n"
        f"👤 Игрок: *{name}*\n"
        f"🆔 ID: `{uid}`\n"
        f"💰 Баланс: *{balance:,}* gold"
    )
    bot.send_message(user_id, text, parse_mode="Markdown", reply_markup=kb)

@bot.message_handler(commands=['bank'])
def bank_profile(msg):
    # Работает ТОЛЬКО в личке
    if msg.chat.type != 'private':
        return bot.reply_to(msg, "⚠️ Эта команда работает только в личных сообщениях с ботом.")
    
    bank_profile_direct(msg.from_user.id)

@bot.callback_query_handler(func=lambda c: c.data == "start_gift_btn")
def start_gift_from_button(call):
    bot.answer_callback_query(call.id)
    uid = str(call.from_user.id)
    
    # Проверка на бан
    if is_banned(uid):
        return bot.send_message(call.from_user.id, "🚫 Вы заблокированы в банковской системе.")
    
    # Антиспам (2 минуты)
    now = datetime.datetime.now()
    if uid in last_transfer:
        diff = (now - last_transfer[uid]).total_seconds()
        if diff < 120:
            return bot.send_message(call.from_user.id, f"⏳ Подождите {int(120 - diff)} сек.")
    
    user_states[uid] = {'action': 'waiting_target_id'}
    bot.send_message(call.from_user.id, "🆔 *Введите ID получателя:*", parse_mode="Markdown")

@bot.callback_query_handler(func=lambda c: c.data == "start_jewel_btn")
def start_jewel_from_button(call):
    bot.answer_callback_query(call.id)
    uid = str(call.from_user.id)
    jewel_user_states[uid] = {'stage': 'target'}
    bot.send_message(call.from_user.id, "💎 *Введите ID получателя Jewel:*", parse_mode="Markdown")

@bot.message_handler(commands=['gift'])
def gift_init(msg):
    # Работает ТОЛЬКО в личке
    if msg.chat.type != 'private':
        return bot.reply_to(msg, "⚠️ Эта команда работает только в личных сообщениях с ботом.")
    
    uid = str(msg.from_user.id)
    
    # Проверка на бан
    if is_banned(uid):
        return bot.reply_to(msg, "🚫 Вы заблокированы в банковской системе.")
    
    # Антиспам (2 минуты)
    now = datetime.datetime.now()
    if uid in last_transfer:
        diff = (now - last_transfer[uid]).total_seconds()
        if diff < 120:
            return bot.reply_to(msg, f"⏳ Подождите {int(120 - diff)} сек.")

    parts = msg.text.split()
    target_id = None

    if msg.reply_to_message:
        target_id = str(msg.reply_to_message.from_user.id)
    elif len(parts) > 1:
        target_id = parts[1]
    else:
        # Переходим в режим ввода ID
        user_states[uid] = {'action': 'waiting_target_id'}
        return bot.reply_to(msg, "🆔 *Введите ID получателя:*", parse_mode="Markdown")

    if target_id == uid:
        return bot.reply_to(msg, "❌ Нельзя переводить самому себе.")

    target_name = get_player_name(target_id)
    user_states[uid] = {
        'target_id': target_id, 
        'target_name': target_name, 
        'action': 'waiting_gift_amount'
    }
    
    bot.reply_to(msg, f"💰 Перевод для: *{target_name}*\n🆔 ID: `{target_id}`\n\n*Введите сумму (минимум 25,000):*", parse_mode="Markdown")

@bot.message_handler(commands=['send_jewel'])
def send_jewel_init(msg):
    if msg.chat.type != 'private':
        return bot.reply_to(msg, "⚠️ Эта команда работает только в личных сообщениях с ботом.")

    uid = str(msg.from_user.id)
    jewel_user_states[uid] = {'stage': 'target'}
    bot.reply_to(msg, "💎 *Введите ID получателя Jewel:*", parse_mode="Markdown")

# Обработчик текстовых состояний (ИСПРАВЛЕНО)
@bot.message_handler(func=lambda msg: msg.chat.type == 'private' and str(msg.from_user.id) in user_states and not msg.text.startswith('/'))
def handle_text_states(msg):
    uid = str(msg.from_user.id)
    state = user_states.get(uid)
    
    if not state:
        return

    # Ввод ID получателя
    if state.get('action') == 'waiting_target_id':
        target_id = msg.text.strip()
        
        if target_id == uid:
            return bot.reply_to(msg, "❌ Нельзя переводить самому себе.")
        
        target_name = get_player_name(target_id)
        state['target_id'] = target_id
        state['target_name'] = target_name
        state['action'] = 'waiting_gift_amount'
        
        bot.reply_to(msg, f"💰 Перевод для: *{target_name}*\n🆔 ID: `{target_id}`\n\n*Введите сумму (минимум 25,000):*", 
                     parse_mode="Markdown")
    
    # Ввод суммы
    elif state.get('action') == 'waiting_gift_amount':
        try:
            amount_str = msg.text.replace(" ", "").replace(",", "")
            amount = int(amount_str)
            
            if amount < 25000:
                return bot.reply_to(msg, "❌ Минимальная сумма: 25,000.")
            
            balance = get_balance(uid)
            if balance in ["db_error", "query_error"]:
                return bot.reply_to(msg, "❌ Ошибка базы данных.")
            
            if balance < amount:
                return bot.reply_to(msg, f"❌ Недостаточно средств. Ваш баланс: {balance:,}")

            state['amount'] = amount
            state['action'] = 'confirm'

            kb = types.InlineKeyboardMarkup()
            kb.add(
                types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"gconfirm_yes_{uid}"),
                types.InlineKeyboardButton("❌ Отмена", callback_data=f"gconfirm_no_{uid}")
            )
            bot.send_message(msg.chat.id, 
                            f"❓ Отправить *{amount:,}* gold игроку *{state['target_name']}* (ID: `{state['target_id']}`)?\n\nВаш баланс после перевода: *{balance - amount:,}*",
                            parse_mode="Markdown", reply_markup=kb)
        except ValueError:
            bot.reply_to(msg, "❌ Введите сумму цифрами.")

@bot.message_handler(func=lambda msg: msg.chat.type == 'private' and str(msg.from_user.id) in jewel_user_states and not msg.text.startswith('/'))
def handle_jewel_user_states(msg):
    uid = str(msg.from_user.id)
    state = jewel_user_states.get(uid)
    if not state:
        return

    if state.get('stage') == 'target':
        target_id = msg.text.strip()
        if target_id == uid:
            return bot.reply_to(msg, "❌ Нельзя отправить Jewel самому себе.")
        state['target_id'] = target_id
        state['stage'] = 'amount'
        bot.reply_to(msg, f"💎 Для ID `{target_id}` введите количество Jewel:", parse_mode="Markdown")
        return

    if state.get('stage') == 'amount':
        try:
            amount = int(msg.text.replace(' ', '').replace(',', ''))
            if amount <= 0:
                return bot.reply_to(msg, "❌ Количество должно быть больше нуля.")

            ok, text = send_jewel_via_yeti(state['target_id'], amount, source_id=uid, admin_mode=False)
            bot.send_message(msg.chat.id, text)
            jewel_user_states.pop(uid, None)
        except ValueError:
            bot.reply_to(msg, "❌ Введите число.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("gconfirm_"))
def gift_final_stage(call):
    _, action, owner_id = call.data.split("_")
    
    if str(call.from_user.id) != owner_id:
        return bot.answer_callback_query(call.id, "❌ Это не ваш перевод!", show_alert=True)

    if owner_id not in user_states:
        return bot.edit_message_text("❌ Сессия истекла.", 
                                     call.message.chat.id, call.message.message_id)

    data = user_states.pop(owner_id)
    
    if action == "no":
        return bot.edit_message_text("❌ Перевод отменен.", 
                                     call.message.chat.id, call.message.message_id)

    amount = data['amount']
    target = data['target_id']
    
    # Проверяем баланс еще раз перед переводом
    balance = get_balance(owner_id)
    if balance in ["db_error", "query_error"] or balance < amount:
        return bot.edit_message_text(f"❌ Ошибка: недостаточно средств.", 
                                     call.message.chat.id, call.message.message_id)
    
    if add_balance(owner_id, -amount) and add_balance(target, amount):
        last_transfer[owner_id] = datetime.datetime.now()
        
        bot.edit_message_text(f"✅ Перевод {amount:,} gold успешно выполнен!\n\nПолучатель: {data['target_name']} (ID: `{target}`)", 
                              call.message.chat.id, call.message.message_id, parse_mode="Markdown")
        try:
            bot.send_message(target, 
                            f"💰 Вам пришел перевод: *{amount:,}* gold от игрока ID: `{owner_id}`\n\nПроверьте `/bank`", 
                            parse_mode="Markdown")
        except: 
            pass
    else:
        bot.send_message(call.message.chat.id, "❌ Ошибка транзакции. Обратитесь к администратору.")

# -------- GUILD --------
@bot.message_handler(commands=['guild'])
def guild(msg):
    try:
        parts = msg.text.split(" ", 1)
        if len(parts) < 2:
            bot.reply_to(
                msg,
                "🔴 `УКАЖИ НАЗВАНИЕ ГИЛЬДИИ`\n\nПример:\n`/guild Imperia Of Titans`",
                parse_mode="Markdown"
            )
            return

        name = parts[1].strip()
        url = f"https://www.rucoyonline.com/guild/{quote(name)}"

        r = requests.get(url, headers=HEADERS, timeout=5)
        
        if r.status_code != 200:
            bot.reply_to(msg, "Гильдия не найдена 📛")
            return

        soup = BeautifulSoup(r.text, "html.parser")
        text_all = soup.get_text("\n", strip=True)

        created = re.search(r"Founded on ([A-Za-z0-9 ,]+)", text_all)
        created = created.group(1) if created else "Не указано"

        members_rows = soup.find_all("tr")
        members_count = sum(1 for r in members_rows if r.find_all("td"))

        desc = extract_description(soup, name)
        desc_clean = desc.replace("```", "`\u200b``")

        reply = (
            f"⚔️ *{name}*\n"
            f"👥 Members: *{members_count}*\n"
            f"📅 Created: *{created}*\n\n"
            f"```\n{desc_clean}\n```\n"
            f"🔗 {url}"
        )

        bot.reply_to(msg, reply, parse_mode="Markdown", disable_web_page_preview=True)

    except requests.exceptions.Timeout:
        bot.reply_to(msg, "⚠️ Ошибка: Сайт Rucoy не отвечает (Таймаут).")
    except Exception as e:
        print(f"Ошибка в команде guild: {e}")
        bot.reply_to(msg, "❌ Произошла ошибка при поиске гильдии.")

# -------- ПРОДОЛЖЕНИЕ ФУНКЦИИ USER (ИСПРАВЛЕНО) --------
@bot.message_handler(commands=['user'])
def user(msg):
    parts = msg.text.split(" ", 1)
    if len(parts) < 2:
        return bot.reply_to(msg, "🔴 `УКАЖИ НИК ИГРОКА`", parse_mode="Markdown")

    name = parts[1].strip()
    url = f"https://www.rucoyonline.com/characters/{quote(name)}"

    try:
        r = requests.get(url, headers=HEADERS, timeout=5)
        if r.status_code != 200:
            return bot.reply_to(msg, "Игрок не найден 📛")

        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.find("table")
        if not table:
            return bot.reply_to(msg, "Нет данных 📛")

        data = {}
        for tr in table.find_all("tr"):
            td = tr.find_all("td")
            if len(td) == 2:
                data[td[0].text.strip()] = td[1].text.strip()

        reply = (
            f"👤 {data.get('Name', name)}\n"
            f"📊 Level: {data.get('Level', '?')}\n"
            f"⚔️ Guild: {data.get('Guild', 'None')}\n"
            f"🟢 Last online: {data.get('Last online', '?')}\n"
            f"📅 Born: {data.get('Born', '?')}\n"
            f"🔗 {url}"
        )
        bot.reply_to(msg, reply, disable_web_page_preview=True)
    except Exception as e:
        bot.reply_to(msg, "❌ Ошибка при поиске игрока")

# -------- ADMIN PANEL (ПО ID) --------

@bot.message_handler(commands=['admin'])
def admin_panel(msg):
    # Проверка по твоему ID
    if msg.from_user.id != ADMIN_ID:
        return

    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("➕ Добавить Gold", callback_data="adm_add"),
        types.InlineKeyboardButton("➖ Снять Gold", callback_data="adm_sub")
    )
    kb.add(
        types.InlineKeyboardButton("⚙️ Установить баланс", callback_data="adm_set"),
        types.InlineKeyboardButton("🚫 Бан/Разбан", callback_data="adm_ban")
    )
    kb.add(types.InlineKeyboardButton("💎 Отправить Jewel (YETI)", callback_data="adm_jewel"))
    
    bot.send_message(msg.chat.id, "🛠 **Панель администратора**", parse_mode="Markdown", reply_markup=kb)

@bot.message_handler(commands=['give_jewel'])
def give_jewel_command(msg):
    if msg.from_user.id != ADMIN_ID:
        return

    parts = msg.text.split()
    if len(parts) >= 3:
        try:
            target_id = parts[1].strip()
            amount = int(parts[2])
            ok, response = send_jewel_via_yeti(target_id, amount, source_id=str(msg.from_user.id), admin_mode=True)
            bot.send_message(msg.chat.id, response)
            return
        except ValueError:
            bot.send_message(msg.chat.id, "❌ Формат: `/give_jewel <user_id> <amount>`", parse_mode="Markdown")
            return

    admin_states[msg.from_user.id] = {"action": "give_jewel", "stage": "target"}
    bot.send_message(msg.chat.id, "💎 Введите **ID пользователя**:", parse_mode="Markdown")

@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_"))
def admin_callback(call):
    if call.from_user.id != ADMIN_ID:
        return bot.answer_callback_query(call.id, "Доступ запрещен!")

    action = call.data.split("_")[1]
    admin_states[call.from_user.id] = {"action": action, "stage": "target"}
    
    if action == "jewel":
        bot.edit_message_text(
            "💎 *Отправка Jewel через YETI*\n\nВведите **ID получателя**:", 
            call.message.chat.id, call.message.message_id, parse_mode="Markdown"
        )
    else:
        bot.edit_message_text(
            f"📝 Действие: {action}. Введите **ID игрока**:", 
            call.message.chat.id, call.message.message_id, parse_mode="Markdown"
        )

# Обработка ввода для админа (ID и Сумма)
@bot.message_handler(func=lambda msg: msg.from_user.id == ADMIN_ID and msg.from_user.id in admin_states)
def handle_admin_text(msg):
    aid = msg.from_user.id
    state = admin_states[aid]
    action = state.get("action")

    # Процесс выдачи Jewel
    if action == "give_jewel":
        if state.get("stage") == "target":
            state["target_id"] = msg.text.strip()
            state["stage"] = "amount"
            bot.send_message(msg.chat.id, "💎 Введите количество Jewel:")
            return

        if state.get("stage") == "amount":
            try:
                amount = int(msg.text.replace(" ", ""))
                if amount <= 0:
                    return bot.send_message(msg.chat.id, "❌ Количество должно быть больше нуля.")
                ok, response = send_jewel_via_yeti(state["target_id"], amount, source_id=str(aid), admin_mode=True)
                bot.send_message(msg.chat.id, response)
                admin_states.pop(aid, None)
            except ValueError:
                bot.send_message(msg.chat.id, "❌ Ошибка! Введите число.")
        return

    # Старые админ-операции
    if "target_id" not in state:
        state["target_id"] = msg.text.strip()
        if state["action"] == "ban":
            res = ban_user(state["target_id"], not is_banned(state["target_id"]))
            bot.send_message(msg.chat.id, f"✅ Статус бана изменен для `{state['target_id']}`")
            admin_states.pop(aid)
        elif state["action"] == "jewel":
            bot.send_message(msg.chat.id, "💎 Введите количество Jewel:")
        else:
            bot.send_message(msg.chat.id, "💰 Введите сумму:")
    else:
        try:
            amount = int(msg.text.replace(" ", ""))
            target = state["target_id"]
            if state["action"] == "jewel":
                ok, response = send_jewel_via_yeti(target, amount, source_id=str(aid), admin_mode=True)
                bot.send_message(msg.chat.id, response)
            else:
                if state["action"] == "add": 
                    add_balance(target, amount)
                elif state["action"] == "sub": 
                    add_balance(target, -amount)
                elif state["action"] == "set": 
                    set_balance(target, amount)
                bot.send_message(msg.chat.id, f"✅ Успешно выполнено для {target}")
            admin_states.pop(aid)
        except:
            bot.send_message(msg.chat.id, "❌ Ошибка! Введите число.")

# -------- ЗАПУСК БОТА И API --------

if __name__ == "__main__":
    # Запускаем Flask в отдельном потоке (Thread)
    # Мы используем лямбда-функцию, чтобы запустить app.run без создания лишних функций
    keep_alive_thread = Thread(target=keep_alive)
    keep_alive_thread.daemon = False
    keep_alive_thread.start()

    flask_thread = Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000))))
    flask_thread.daemon = False
    flask_thread.start()
    
    print("✅ Flask API запущен на порту", os.environ.get("PORT", 10000))
    print("✅ Бот начинает опрос (polling)...")
    
    # Пытаемся запустить бота с защитой от вылетов
    while True:
        try:
            # Убираем вебхуки, чтобы избежать конфликтов
            bot.remove_webhook()
            # Запускаем бесконечный опрос
            bot.polling(none_stop=True, interval=1, timeout=20)
        except Exception as e:
            print(f"❌ Ошибка подключения бота: {e}")
            # Ждем 10 секунд, если Telegram нас временно ограничил
            time.sleep(10)
