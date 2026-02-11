from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from config import ADMINS, BOT_TOKEN, MOD_LOG_CHAT_ID, MAX_REPORTS
from states import SELECT_GENDER, SELECT_AGE
from storage import load_data, save_data
from moderation import (
    admin_actions_keyboard,
    report_keyboard,
    add_report,
    report_text,
    is_admin,
    set_sanction,
    clear_sanction,
    is_active,
)

from telegram.constants import ChatAction

# ===== STATES =====
STATE_IDLE = "idle"
STATE_SEARCH = "search"
STATE_DIALOG = "dialog"

USER_STATE: dict[str, str] = {}  # user_id -> state

# ===== DATA =====
DATA = load_data()
PROFILES = DATA.setdefault("profiles", {})
DIALOGS = DATA.setdefault("dialogs", {})         # user_id -> partner_id
SEARCH_QUEUE = DATA.setdefault("queue", [])      # list[user_id]
BANS = DATA.setdefault("bans", {})               # sanctions storage
REPORTS = DATA.setdefault("reports", {})
# ===== BLACKLIST & LAST_PARTNER (храним внутри BANS, чтобы persist() работал без правок storage.py) =====
BLACKLIST = BANS.setdefault("__blacklist__", {})       # user_id -> list[str]
LAST_PARTNER = BANS.setdefault("__last_partner__", {}) # user_id -> last_partner_id
# ===== RATINGS (храним внутри BANS для persist) =====
RATINGS = BANS.setdefault("__ratings__", {})           # user_id -> {"total": int, "count": int}
PENDING_RATINGS = BANS.setdefault("__pending_ratings__", {})  # user_id -> partner_id (кого нужно оценить)


def persist():
    # НЕ трогаю сигнатуру save_data — как у тебя было
    save_data(PROFILES, DIALOGS, SEARCH_QUEUE, BANS, REPORTS)


# ===== KEYBOARD =====
MAIN_KB = ReplyKeyboardMarkup(
    [
        ["🔍 Искать", "🔄 Новый поиск"],
        ["🚫 Завершить"],
        ["👤 Профиль", "🚨 Пожаловаться"]
    ],
    resize_keyboard=True
)

MENU_TEXT = (
    "📋 *Головне меню*\n\n"
    "Используй команды ниже:\n\n"
    "⌨️ /show_keyboard — показать кнопки\n"
    "⛔ /blacklist — чёрный список\n"
    "🔒 /privacy — политика приватности\n"
    "📖 /info — правила пользования"
)

PRIVACY_TEXT = (
    "🔒 *Политика приватности*\n\n"
    "• Мы не храним сообщения\n"
    "• Собеседник анонимен\n"
    "• Жалобы модерируются вручную"
)

INFO_TEXT = (
    "📖 *Правила пользования*\n\n"
    "• Запрещены оскорбления\n"
    "• Запрещён спам\n"
    "• За нарушения — мут / бан"
)

def menu_panel():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⌨️ Показать кнопки", callback_data="menu_show_keyboard")],
            [InlineKeyboardButton("🚫 Чёрный список", callback_data="menu_blacklist")],
            [InlineKeyboardButton("🔒 Приватность", callback_data="menu_privacy")],
            [InlineKeyboardButton("📖 Информация", callback_data="menu_info")],
        ]
    )


# ===== ДОБАВЛЕНО: inline-панель после диалога (как на скринах) =====
def post_dialog_panel():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⭐ Оценить собеседника", callback_data="post_rate")],
            [InlineKeyboardButton("🚨 Пожаловаться", callback_data="post_report")],
            [InlineKeyboardButton("🚫 В чёрный список", callback_data="post_blacklist")],
            [InlineKeyboardButton("👤 Профиль собеседника", callback_data="post_partner_profile")],
            [InlineKeyboardButton("🔄 Новый поиск", callback_data="post_newsearch")],
        ]
    )

# ===== ДОБАВЛЕНО: клавиатура для оценки (1-5 звезд) =====
def rating_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⭐", callback_data="rate_1"),
            InlineKeyboardButton("⭐⭐", callback_data="rate_2"),
            InlineKeyboardButton("⭐⭐⭐", callback_data="rate_3"),
        ],
        [
            InlineKeyboardButton("⭐⭐⭐⭐", callback_data="rate_4"),
            InlineKeyboardButton("⭐⭐⭐⭐⭐", callback_data="rate_5"),
        ],
        [InlineKeyboardButton("❌ Пропустить", callback_data="rate_skip")],
    ])


# ===== ДОБАВЛЕНО: inline-панель на /start когда профиль уже есть =====
def start_panel():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔍 Искать", callback_data="menu_search")],
            [InlineKeyboardButton("📝 Создать профиль заново", callback_data="menu_reset_profile")],
        ]
    )


# =========================
# HELPERS (P3 stability)
# =========================
def _remove_from_queue(user_id: str):
    # remove all duplicates
    SEARCH_QUEUE[:] = [u for u in SEARCH_QUEUE if u != user_id]


def _set_state(user_id: str, state: str):
    USER_STATE[user_id] = state


def _sync_state_for(user_id: str):
    """Best-effort: keep USER_STATE consistent with real data."""
    if user_id in DIALOGS:
        _set_state(user_id, STATE_DIALOG)
        return
    if user_id in SEARCH_QUEUE:
        _set_state(user_id, STATE_SEARCH)
        return
    _set_state(user_id, STATE_IDLE)


def _ensure_sync_all():
    # cleanup queue from users who are in dialogs
    SEARCH_QUEUE[:] = [u for u in SEARCH_QUEUE if u not in DIALOGS]
    # remove duplicates while keeping order
    seen = set()
    newq = []
    for u in SEARCH_QUEUE:
        if u not in seen:
            seen.add(u)
            newq.append(u)
    SEARCH_QUEUE[:] = newq


# =========================
# BLACKLIST helpers
# =========================
def _bl_list(user_id: str) -> list[str]:
    """Получить список заблокированных пользователей с очисткой дубликатов."""
    user_id = str(user_id)
    lst = BLACKLIST.get(user_id)
    if not isinstance(lst, list):
        lst = []
        BLACKLIST[user_id] = lst
    # чистим мусор/дубликаты
    seen = set()
    clean = []
    for x in lst:
        sx = str(x)
        if sx not in seen:
            seen.add(sx)
            clean.append(sx)
    BLACKLIST[user_id] = clean
    return clean

def _bl_has(user_id: str, other_id: str) -> bool:
    """Проверить, заблокирован ли other_id пользователем user_id."""
    return str(other_id) in _bl_list(str(user_id))

def _bl_add(user_id: str, other_id: str) -> bool:
    """Добавить other_id в черный список user_id. Возвращает True если добавлен."""
    user_id = str(user_id)
    other_id = str(other_id)
    if user_id == other_id:
        return False
    lst = _bl_list(user_id)
    if other_id in lst:
        return False
    lst.append(other_id)
    BLACKLIST[user_id] = lst
    return True

def _bl_remove(user_id: str, other_id: str) -> bool:
    """Убрать other_id из черного списка user_id. Возвращает True если убран."""
    user_id = str(user_id)
    other_id = str(other_id)
    lst = _bl_list(user_id)
    if other_id not in lst:
        return False
    lst.remove(other_id)
    BLACKLIST[user_id] = lst
    return True

def _blocked_between(a: str, b: str) -> bool:
    """True если a заблокировал b ИЛИ b заблокировал a (для матчмейкинга)."""
    a = str(a)
    b = str(b)
    return _bl_has(a, b) or _bl_has(b, a)


# =========================
# RATING helpers
# =========================
def _get_rating(user_id: str) -> dict:
    """Получить рейтинг пользователя {total: int, count: int}."""
    user_id = str(user_id)
    rating = RATINGS.get(user_id)
    if not isinstance(rating, dict):
        rating = {"total": 0, "count": 0}
        RATINGS[user_id] = rating
    return rating

def _add_rating(user_id: str, stars: int):
    """Добавить оценку пользователю (1-5 звезд)."""
    user_id = str(user_id)
    rating = _get_rating(user_id)
    rating["total"] += stars
    rating["count"] += 1
    RATINGS[user_id] = rating

def _average_rating(user_id: str) -> float:
    """Получить средний рейтинг пользователя."""
    rating = _get_rating(user_id)
    if rating["count"] == 0:
        return 0.0
    return round(rating["total"] / rating["count"], 1)

def _rating_stars(user_id: str) -> str:
    """Получить рейтинг в виде звёзд (⭐⭐⭐⭐⭐)."""
    avg = _average_rating(user_id)
    if avg == 0:
        return "Нет оценок"
    full_stars = int(avg)
    half_star = 1 if (avg - full_stars) >= 0.5 else 0
    empty_stars = 5 - full_stars - half_star
    return "⭐" * full_stars + ("✨" if half_star else "") + "☆" * empty_stars + f" ({avg})"


async def _break_dialog(user_id: str, context: ContextTypes.DEFAULT_TYPE, notify_partner: bool = True):
    """Break dialog for user; notify partner if existed."""
    partner = DIALOGS.pop(user_id, None)
    if not partner:
        _sync_state_for(user_id)
        _remove_from_queue(user_id)
        return None

    # remove reverse link
    DIALOGS.pop(partner, None)

    # states
    _set_state(user_id, STATE_IDLE)
    _set_state(partner, STATE_IDLE)

    # also ensure neither is in queue
    _remove_from_queue(user_id)
    _remove_from_queue(partner)

    # ДОБАВЛЕНО: запоминаем последнего собеседника для обеих сторон
    LAST_PARTNER[user_id] = partner
    LAST_PARTNER[partner] = user_id
    
    # ДОБАВЛЕНО: сохраняем pending rating для обеих сторон
    PENDING_RATINGS[user_id] = partner
    PENDING_RATINGS[partner] = user_id
    
    persist()

    if notify_partner:
        try:
            await context.bot.send_message(
                int(partner),
                "❌ Собеседник завершил диалог.\n\n"
                "Оцени собеседника 👇",
                reply_markup=rating_keyboard()
            )
        except Exception:
            pass
    return partner


async def _try_match(user_id: str, context: ContextTypes.DEFAULT_TYPE):
    """Try to match user with someone from queue. Returns partner_id or None."""
    _ensure_sync_all()

    # find first valid partner != user_id who is also searching and not in dialog
    partner = None
    for u in SEARCH_QUEUE:
        if u == user_id:
            continue
        # partner must not be in dialog
        if u in DIALOGS:
            continue
        # partner must be searching
        if USER_STATE.get(u) != STATE_SEARCH:
            continue
        # ДОБАВЛЕНО: не матчим людей из ЧС
        if _blocked_between(user_id, u):
            continue
        partner = u
        break

    if not partner:
        return None

    # remove both from queue
    _remove_from_queue(user_id)
    _remove_from_queue(partner)

    # connect dialog
    DIALOGS[user_id] = partner
    DIALOGS[partner] = user_id

    _set_state(user_id, STATE_DIALOG)
    _set_state(partner, STATE_DIALOG)

    # ДОБАВЛЕНО: запоминаем последнего собеседника
    LAST_PARTNER[user_id] = partner
    LAST_PARTNER[partner] = user_id

    persist()

    # notify both
    await context.bot.send_message(
        int(partner),
        "✨ Собеседник найден!\n\n"
        "Можешь писать сообщение 💬",
        reply_markup=MAIN_KB
    )
    return partner


# ===== MENU =====
# =========================
# HANDLERS
# =========================

# ===== /start =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    user_id = str(update.effective_user.id)

    # break anything stale
    await _break_dialog(user_id, context, notify_partner=True)
    _remove_from_queue(user_id)
    _set_state(user_id, STATE_IDLE)
    persist()

    # ДОБАВЛЕНО: если профиль уже есть — показываем его (и кнопка создать заново)
    p = PROFILES.get(user_id)
    if p:
        vip_status = "Обычный"  # задел под VIP/Premium
        bl_count = len(_bl_list(user_id))
        reports_count = int(REPORTS.get(user_id, 0))

        await update.message.reply_text(
            f"👤 *ТВОЙ ПРОФИЛЬ*\n\n"
            f"🆔 ID: `{user_id}`\n"
            f"🧑 Пол: {p.get('gender', '—')}\n"
            f"🎂 Возраст: {p.get('age', '—')}\n\n"
            f"⭐ Статус: *{vip_status}*\n"
            f"🚫 В чёрном списке: *{bl_count}*\n"
            f"🚨 Жалоб на тебя: *{reports_count}*\n\n"
            f"Выбирай действие 👇",
            parse_mode="Markdown",
            reply_markup=start_panel()
        )
        return

    kb = [[InlineKeyboardButton("♂️ Мужской", callback_data="gender_male")]]
    await update.message.reply_text(
        "👋 Добро пожаловать!\n\n"
        "Для начала выбери пол 👇",
        reply_markup=InlineKeyboardMarkup(kb)
    )
    context.user_data["step"] = SELECT_GENDER


# ===== REG: GENDER =====
async def select_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["step"] = SELECT_AGE

    kb = [[InlineKeyboardButton(str(i), callback_data=f"age_{i}") for i in range(16, 21)]]
    await q.edit_message_text(
        "🎂 Выбери возраст:",
        reply_markup=InlineKeyboardMarkup(kb)
    )


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(MENU_TEXT, parse_mode="Markdown")

async def cmd_show_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⌨️ Клавиатура показана", reply_markup=MAIN_KB)

async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(PRIVACY_TEXT, parse_mode="Markdown")

async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(INFO_TEXT, parse_mode="Markdown")


# ===== REG: AGE =====
async def select_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    user_id = str(q.from_user.id)
    age = q.data.replace("age_", "")

    PROFILES[user_id] = {"gender": "♂️", "age": age}
    _set_state(user_id, STATE_IDLE)
    persist()

    await q.edit_message_text("✅ Профиль создан!")
    await context.bot.send_message(
        int(user_id),
        "Готово 🎉\n\n"
        "Используй кнопки ниже 👇",
        reply_markup=MAIN_KB
    )


# ===== SEARCH =====
async def start_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    user_id = str(update.effective_user.id)

    # ban check (admins bypass)
    if is_active(BANS, user_id, "ban") and int(user_id) not in ADMINS:
        await update.message.reply_text(
            "⛔ Доступ ограничен.\n\n"
            "Ты временно заблокирован.",
            reply_markup=MAIN_KB
        )
        return

    # sync state
    _sync_state_for(user_id)

    # if already in dialog
    if USER_STATE.get(user_id) == STATE_DIALOG:
        await update.message.reply_text(
            "⚠️ Ты уже в диалоге.",
            reply_markup=MAIN_KB
        )
        return

    # if already searching
    if USER_STATE.get(user_id) == STATE_SEARCH:
        await update.message.reply_text(
            "🔎 Поиск уже запущен.\n\n"
            "Пожалуйста, подожди…",
            reply_markup=MAIN_KB
        )
        return

    # clean duplicates
    _remove_from_queue(user_id)

    # mark searching + enqueue
    _set_state(user_id, STATE_SEARCH)
    SEARCH_QUEUE.append(user_id)
    persist()

    # try immediate match
    partner = await _try_match(user_id, context)
    if partner:
        await update.message.reply_text(
            "✨ Собеседник найден!\n\n"
            "Можешь начинать общение 💬",
            reply_markup=MAIN_KB
        )
        return

    await update.message.reply_text(
        "🔍 Идёт поиск собеседника…\n\n"
        "Это может занять несколько секунд ⏳",
        reply_markup=MAIN_KB
    )


# ===== NEW SEARCH (end dialog if exists and immediately search) =====
async def new_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    user_id = str(update.effective_user.id)

    # ban check
    if is_active(BANS, user_id, "ban") and int(user_id) not in ADMINS:
        await update.message.reply_text(
            "⛔ Доступ ограничен.\n\n"
            "Ты временно заблокирован.",
            reply_markup=MAIN_KB
        )
        return

    # break dialog if exists
    await _break_dialog(user_id, context, notify_partner=True)

    # also remove from queue
    _remove_from_queue(user_id)

    # start fresh search
    _set_state(user_id, STATE_IDLE)
    persist()

    await update.message.reply_text(
        "🔄 Начинаю новый поиск…",
        reply_markup=MAIN_KB
    )
    await start_search(update, context)


# ===== END (just stop everything, no search) =====
async def end_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    user_id = str(update.effective_user.id)

    # If in dialog -> notify partner
    partner = await _break_dialog(user_id, context, notify_partner=True)

    # If in queue -> remove
    _remove_from_queue(user_id)

    # state idle
    _set_state(user_id, STATE_IDLE)
    persist()

    # ДОБАВЛЕНО: панель после диалога (жалоба / ЧС / новый поиск / профиль)
    if partner:
        await update.message.reply_text(
            "⛔ Диалог завершён.\n\n"
            "Что будем делать дальше?",
            reply_markup=post_dialog_panel()
        )
    else:
        await update.message.reply_text(
            "⛔ Диалог завершён.\n\n"
            "Ты вышел из чата.",
            reply_markup=MAIN_KB
        )


# ===== PROFILE =====
async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    user_id = str(update.effective_user.id)
    p = PROFILES.get(user_id)

    if not p:
        kb = [[InlineKeyboardButton("♂️ Мужской", callback_data="gender_male")]]
        await update.message.reply_text(
            "👤 Профиль не найден.\n\n"
            "Давай создадим его 👇",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    vip_status = "Обычный"  # задел под VIP/Premium
    bl_count = len(_bl_list(user_id))
    reports_count = int(REPORTS.get(user_id, 0))
    rating_display = _rating_stars(user_id)
    rating_count = _get_rating(user_id)["count"]

    await update.message.reply_text(
        f"👤 *ТВОЙ ПРОФИЛЬ*\n\n"
        f"🆔 ID: `{user_id}`\n"
        f"🧑 Пол: {p.get('gender', '—')}\n"
        f"🎂 Возраст: {p.get('age', '—')}\n\n"
        f"⭐ Рейтинг: {rating_display} ({rating_count} оценок)\n"
        f"💎 Статус: *{vip_status}*\n"
        f"🚫 В чёрном списке: *{bl_count}*\n"
        f"🚨 Жалоб на тебя: *{reports_count}*\n\n"
        f"💎 *Premium / VIP* — скоро (подключим позже)",
        parse_mode="Markdown",
        reply_markup=MAIN_KB
    )

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    await update.message.reply_text(
        "📋 *Меню управления*\n\n"
        "Выбери нужный пункт 👇",
        reply_markup=menu_panel(),
        parse_mode="Markdown"
    )


# ===== REPORT =====
async def report_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    user_id = str(update.effective_user.id)
    if user_id not in DIALOGS:
        await update.message.reply_text(
            "⚠️ Жалобу можно отправить только во время диалога.\n\n"
            "Либо заверши диалог и нажми «🚨 Пожаловаться» в панели.",
            reply_markup=MAIN_KB
        )
        return

    await update.message.reply_text(
        "🚨 Выбери причину жалобы:",
        reply_markup=report_keyboard()
    )


async def report_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    reporter = q.from_user

    # ✅ ВАЖНО: теперь жалоба работает и в диалоге, и после диалога
    target_id = DIALOGS.get(str(reporter.id))
    if not target_id:
        target_id = LAST_PARTNER.get(str(reporter.id))

    if not target_id:
        await q.edit_message_text("ℹ️ Нет данных о собеседнике для жалобы.")
        return

    target_user = await context.bot.get_chat(int(target_id))
    reason = q.data.replace("report_", "")

    add_report(
        str(reporter.id),
        target_id,
        reason,
        {"reports": REPORTS, "bans": BANS, "max_reports": MAX_REPORTS}
    )
    persist()

    await context.bot.send_message(
        MOD_LOG_CHAT_ID,
        report_text(reporter, target_user, reason, REPORTS.get(target_id, 0)),
        reply_markup=admin_actions_keyboard(target_id),
        parse_mode="Markdown"
    )

    await q.edit_message_text("✅ Жалоба отправлена. Спасибо!")


# =========================
# BLACKLIST UI (/blacklist)
# =========================
def _blacklist_kb(user_id: str, partner_id: str | None = None):
    rows = []

    if partner_id:
        in_bl = _bl_has(user_id, partner_id)
        if in_bl:
            rows.append([InlineKeyboardButton("✅ Убрать собеседника из ЧС", callback_data=f"bl_rm_{partner_id}")])
        else:
            rows.append([InlineKeyboardButton("⛔ Добавить собеседника в ЧС", callback_data=f"bl_add_{partner_id}")])

    rows.append([InlineKeyboardButton("📋 Показать мой ЧС", callback_data="bl_list")])
    rows.append([InlineKeyboardButton("❌ Закрыть", callback_data="bl_close")])
    return InlineKeyboardMarkup(rows)


async def cmd_blacklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    user_id = str(update.effective_user.id)
    partner_id = DIALOGS.get(user_id)  # если сейчас в диалоге

    count = len(_bl_list(user_id))
    text = (
        "⛔ *Чёрный список*\n\n"
        f"В списке: *{count}* пользователей.\n\n"
        "• Если ты добавишь человека в ЧС — вы больше не будете попадаться друг другу.\n"
        "• Управление — кнопками ниже."
    )

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=_blacklist_kb(user_id, partner_id)
    )


async def blacklist_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    user_id = str(q.from_user.id)
    data = q.data

    if data == "bl_close":
        await q.edit_message_text("✅ Закрыто.")
        return

    # показать список
    if data == "bl_list":
        lst = _bl_list(user_id)
        if not lst:
            await q.edit_message_text(
                "📋 *Чёрный список пуст.*",
                parse_mode="Markdown",
                reply_markup=_blacklist_kb(user_id, DIALOGS.get(user_id))
            )
            return

        # кнопки удаления (первые 10, чтобы не раздувать)
        rows = []
        show = lst[:10]
        for uid in show:
            rows.append([InlineKeyboardButton(f"❌ Убрать {uid}", callback_data=f"bl_rm_{uid}")])
        rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="bl_back")])

        text = "📋 *Твой ЧС:*\n\n" + "\n".join([f"• `{x}`" for x in show])
        if len(lst) > 10:
            text += f"\n\n…и ещё *{len(lst) - 10}* (пока не показываю, чтобы не спамить кнопками)."

        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data == "bl_back":
        await q.edit_message_text(
            "⛔ *Чёрный список*",
            parse_mode="Markdown",
            reply_markup=_blacklist_kb(user_id, DIALOGS.get(user_id))
        )
        return

    # добавить
    if data.startswith("bl_add_"):
        target_id = data.replace("bl_add_", "")
        ok = _bl_add(user_id, target_id)
        persist()

        # если сейчас был в диалоге с ним — разрываем сразу
        if DIALOGS.get(user_id) == target_id:
            await _break_dialog(user_id, context, notify_partner=True)

        await q.edit_message_text(
            "✅ Добавлен в ЧС." if ok else "ℹ️ Он уже был в ЧС.",
            reply_markup=_blacklist_kb(user_id, DIALOGS.get(user_id))
        )
        return

    # убрать
    if data.startswith("bl_rm_"):
        target_id = data.replace("bl_rm_", "")
        ok = _bl_remove(user_id, target_id)
        persist()
        await q.edit_message_text(
            "✅ Убран из ЧС." if ok else "ℹ️ Его нет в ЧС.",
            reply_markup=_blacklist_kb(user_id, DIALOGS.get(user_id))
        )
        return


# ===== ДОБАВЛЕНО: POST actions (после диалога) =====
async def post_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    user_id = str(q.from_user.id)
    partner = LAST_PARTNER.get(user_id)

    if q.data == "post_newsearch":
        # запускаем новый поиск
        try:
            await q.edit_message_text("🔄 Запускаю новый поиск…")
        except Exception:
            pass
        # “фальш” update.message нет, но start_search проверяет update.message
        # поэтому шлём сообщение и далее запускаем через send_message → пользователь нажмёт кнопку
        await context.bot.send_message(
            int(user_id),
            "Нажми «🔍 Искать», чтобы начать новый поиск 👇",
            reply_markup=MAIN_KB
        )
        return

    if not partner:
        await q.edit_message_text("ℹ️ Нет данных о последнем собеседнике.")
        return

    if q.data == "post_blacklist":
        ok = _bl_add(user_id, partner)
        persist()
        await q.edit_message_text(
            "🚫 Добавлен в чёрный список." if ok else "ℹ️ Он уже в твоём чёрном списке."
        )
        return

    if q.data == "post_partner_profile":
        p = PROFILES.get(partner)
        if not p:
            await q.edit_message_text("ℹ️ Профиль собеседника не найден.")
            return

        await q.edit_message_text(
            f"👤 *ПРОФИЛЬ СОБЕСЕДНИКА*\n\n"
            f"🆔 ID: `{partner}`\n"
            f"🧑 Пол: {p.get('gender', '—')}\n"
            f"🎂 Возраст: {p.get('age', '—')}\n\n"
            f"🚨 Жалоб на него: *{int(REPORTS.get(partner, 0))}*\n"
            f"🚫 В твоём ЧС: *{'Да' if _bl_has(user_id, partner) else 'Нет'}*",
            parse_mode="Markdown",
        )
        return

    if q.data == "post_report":
        # показываем причины (а обработает report_reason по report_*)
        await q.edit_message_text("🚨 Выбери причину жалобы:", reply_markup=report_keyboard())
        return

    if q.data == "post_rate":
        # показываем клавиатуру оценки
        await q.edit_message_text("⭐ Оцени собеседника:", reply_markup=rating_keyboard())
        return


# ===== ДОБАВЛЕНО: обработчик оценок =====
async def rating_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    user_id = str(q.from_user.id)
    
    if q.data == "rate_skip":
        # пропустить оценку
        PENDING_RATINGS.pop(user_id, None)
        persist()
        await q.edit_message_text("✅ Оценка пропущена.")
        return

    # получить оценку (1-5)
    if q.data.startswith("rate_"):
        stars = int(q.data.replace("rate_", ""))
        partner = PENDING_RATINGS.pop(user_id, None)
        
        if not partner:
            await q.edit_message_text("ℹ️ Нет данных о собеседнике для оценки.")
            return
        
        # добавить оценку
        _add_rating(partner, stars)
        persist()
        
        await q.edit_message_text(
            f"✅ Спасибо за оценку!\n\n"
            f"Ты поставил {'⭐' * stars}"
        )
        return


# ===== ДОБАВЛЕНО: menu actions на /start =====
async def menu_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    user_id = str(q.from_user.id)

    if q.data == "menu_reset_profile":
        # удаляем только профиль пользователя (как ты и хотел — создать заново)
        PROFILES.pop(user_id, None)
        persist()

        kb = [[InlineKeyboardButton("♂️ Мужской", callback_data="gender_male")]]
        await q.edit_message_text(
            "📝 Ок, создаём профиль заново.\n\nВыбери пол:",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    if q.data == "menu_search":
        # удобно — просто подсказка и клавиатура
        await q.edit_message_text("Нажми «🔍 Искать» снизу 👇")
        await context.bot.send_message(int(user_id), "Жми «🔍 Искать» 👇", reply_markup=MAIN_KB)
        return


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    if update.effective_user.id not in ADMINS:
        return

    text = update.message.text.replace("/broadcast", "", 1).strip()
    if not text:
        await update.message.reply_text("❌ Напиши текст после /broadcast")
        return

    sent = 0
    for uid in PROFILES.keys():
        try:
            await context.bot.send_message(int(uid), text)
            sent += 1
        except:
            pass

    await update.message.reply_text(f"✅ Рассылка отправлена ({sent} пользователей)")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMINS:
        return
    await update.message.reply_text(
        f"👥 Пользователей: {len(PROFILES)}\n"
        f"💬 В диалогах: {len(DIALOGS) // 2}\n"
        f"🔍 В поиске: {len(SEARCH_QUEUE)}"
    )


# ===== ADMIN =====
async def admin_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not is_admin(q.from_user.id):
        await q.edit_message_text("⛔ Нет доступа.")
        return

    data = q.data
    data_pack = {"bans": BANS}

    if data.startswith("admin_profile_"):
        target_id = data.replace("admin_profile_", "")
        p = PROFILES.get(target_id)
        if not p:
            await q.edit_message_text("Профиль не найден.")
            return

        await q.edit_message_text(
            f"👤 Профиль\n\n"
            f"ID: `{target_id}`\n"
            f"Пол: {p['gender']}\n"
            f"Возраст: {p['age']}\n"
            f"Жалоб: {REPORTS.get(target_id, 0)}\n"
            f"Бан: {'Да' if is_active(BANS, target_id, 'ban') else 'Нет'}\n"
            f"Мут: {'Да' if is_active(BANS, target_id, 'mute') else 'Нет'}",
            parse_mode="Markdown",
            reply_markup=admin_actions_keyboard(target_id)
        )
        return

    if data.startswith("admin_ban24_"):
        target_id = data.replace("admin_ban24_", "")
        if target_id == str(q.from_user.id) or int(target_id) in ADMINS:
            await q.edit_message_text("⚠️ Нельзя банить администратора или себя.")
            return

        set_sanction("ban", target_id, data_pack, q.from_user.id, 24 * 60, "бан 24ч")
        persist()
        await q.edit_message_text(
            "🚫 Бан на 24 часа установлен.",
            reply_markup=admin_actions_keyboard(target_id)
        )
        return

    if data.startswith("admin_unban_"):
        target_id = data.replace("admin_unban_", "")
        clear_sanction("ban", target_id, data_pack, q.from_user.id)
        persist()
        await q.edit_message_text(
            "🔓 Бан снят.",
            reply_markup=admin_actions_keyboard(target_id)
        )
        return

    if data.startswith("admin_mute30_"):
        target_id = data.replace("admin_mute30_", "")
        if target_id == str(q.from_user.id) or int(target_id) in ADMINS:
            await q.edit_message_text("⚠️ Нельзя мутить администратора или себя.")
            return

        set_sanction("mute", target_id, data_pack, q.from_user.id, 30, "мут 30м")
        persist()
        await q.edit_message_text(
            "🔇 Мут на 30 минут установлен.",
            reply_markup=admin_actions_keyboard(target_id)
        )
        return

    if data.startswith("admin_unmute_"):
        target_id = data.replace("admin_unmute_", "")
        clear_sanction("mute", target_id, data_pack, q.from_user.id)
        persist()
        await q.edit_message_text(
            "🔊 Мут снят.",
            reply_markup=admin_actions_keyboard(target_id)
        )
        return


# ===== RELAY =====
async def relay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    user_id = str(update.effective_user.id)

    # mute check (admins bypass)
    if is_active(BANS, user_id, "mute") and int(user_id) not in ADMINS:
        await update.message.reply_text("🔇 Ты в муте.")
        return

    partner = DIALOGS.get(user_id)
    if not partner:
        return

    # If partner link is broken, clean user
    if DIALOGS.get(partner) != user_id:
        # stale, cleanup
        DIALOGS.pop(user_id, None)
        _set_state(user_id, STATE_IDLE)
        _remove_from_queue(user_id)
        persist()
        return

    await update.message.copy(chat_id=int(partner))


# ===== ERROR =====
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print("❌ ERROR:", context.error)

# ===== MENU CALLBACKS =====
async def menu_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    user_id = str(q.from_user.id)
    data = q.data

    # ===== ПОКАЗАТЬ КЛАВИАТУРУ =====
    if q.data == "menu_show_keyboard":
        await q.edit_message_text(
            "⌨️ Клавиатура показана.\n\n"
            "Используй кнопки внизу экрана 👇"
        )
        await context.bot.send_message(
            chat_id=q.from_user.id,
            text="⬇️ Главное меню",
            reply_markup=MAIN_KB
        )
        return

    # ===== ПОИСК ИЗ МЕНЮ / СТАРТА =====
    if data == "menu_search":
        await q.edit_message_text("🔍 Нажми «🔍 Искать» снизу 👇")
        await context.bot.send_message(int(user_id), "Жми «🔍 Искать» 👇", reply_markup=MAIN_KB)
        return

    # ===== СБРОС ПРОФИЛЯ =====
    if data == "menu_reset_profile":
        PROFILES.pop(user_id, None)
        persist()

        await q.edit_message_text(
            "📝 Профиль удалён.\n\n"
            "Давай создадим новый 👇",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("♂️ Мужской", callback_data="gender_male")]]
            )
        )
        return

    # ===== ЧЁРНЫЙ СПИСОК =====
    if data == "menu_blacklist":
        await q.edit_message_text(
            "🚫 *Чёрный список*\n\n"
            "Здесь отображаются пользователи,\n"
            "с которыми ты не хочешь общаться.",
            parse_mode="Markdown"
        )
        return

    # ===== ПРИВАТНОСТЬ =====
    if data == "menu_privacy":
        await q.edit_message_text(
            "🔒 *Политика приватности*\n\n"
            "• Бот не сохраняет переписки\n"
            "• Все диалоги анонимны\n"
            "• Жалобы видят только модераторы",
            parse_mode="Markdown"
        )
        return

    # ===== ИНФОРМАЦИЯ =====
    if data == "menu_info":
        await q.edit_message_text(
            "📖 *Правила пользования*\n\n"
            "• Запрещены оскорбления\n"
            "• Запрещён спам\n"
            "• За нарушения — блокировка",
            parse_mode="Markdown"
        )
        return


# ===== MAIN =====
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # ===== COMMANDS =====
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("show_keyboard", cmd_show_keyboard))
    app.add_handler(CommandHandler("privacy", cmd_privacy))
    app.add_handler(CommandHandler("info", cmd_info))
    app.add_handler(CommandHandler("blacklist", cmd_blacklist))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("broadcast", broadcast))

    # ===== INLINE CALLBACKS =====
    app.add_handler(CallbackQueryHandler(menu_callbacks, pattern="^menu_"))
    app.add_handler(CallbackQueryHandler(select_gender, pattern="^gender_"))
    app.add_handler(CallbackQueryHandler(select_age, pattern="^age_"))
    app.add_handler(CallbackQueryHandler(blacklist_actions, pattern="^bl_"))
    app.add_handler(CallbackQueryHandler(report_reason, pattern="^report_"))
    app.add_handler(CallbackQueryHandler(admin_actions, pattern="^admin_"))
    app.add_handler(CallbackQueryHandler(post_actions, pattern="^post_"))
    app.add_handler(CallbackQueryHandler(rating_handler, pattern="^rate_"))

    # ===== REPLY KEYBOARD BUTTONS =====
    app.add_handler(MessageHandler(filters.Regex("^🔍 Искать$"), start_search))
    app.add_handler(MessageHandler(filters.Regex("^🔄 Новый поиск$"), new_search))
    app.add_handler(MessageHandler(filters.Regex("^🚫 Завершить$"), end_dialog))
    app.add_handler(MessageHandler(filters.Regex("^👤 Профиль$"), profile))
    app.add_handler(MessageHandler(filters.Regex("^🚨 Пожаловаться$"), report_start))

    # ===== CHAT RELAY =====
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, relay))

    # ===== ERRORS =====
    app.add_error_handler(error_handler)

    print("✅ Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()

