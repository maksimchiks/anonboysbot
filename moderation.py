import time
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from config import ADMINS, MAX_REPORTS

# ===== ВСПОМОГАТЕЛЬНОЕ =====

def _now() -> int:
    return int(time.time())


def is_admin(user_id: int) -> bool:
    return int(user_id) in list(map(int, ADMINS))


def _ensure_user_pack(bans: dict, user_id: str) -> dict:
    pack = bans.get(user_id)
    if not isinstance(pack, dict):
        pack = {}
        bans[user_id] = pack
    return pack


def get_sanction(bans: dict, user_id: str, kind: str):
    pack = bans.get(user_id)
    if not isinstance(pack, dict):
        return None
    return pack.get(kind)


def is_active(bans: dict, user_id: str, kind: str) -> bool:
    s = get_sanction(bans, user_id, kind)
    if not isinstance(s, dict):
        return False
    until = int(s.get("until", 0))
    return until == 0 or until > _now()

# ===== САНКЦИИ =====

def set_sanction(kind: str, target_id: str, data_pack: dict, by_id: int, minutes: int, note: str):
    bans = data_pack["bans"]
    pack = _ensure_user_pack(bans, target_id)
    until = 0 if minutes == 0 else _now() + minutes * 60
    pack[kind] = {
        "until": until,
        "by": int(by_id),
        "note": note
    }


def clear_sanction(kind: str, target_id: str, data_pack: dict, by_id: int) -> bool:
    bans = data_pack["bans"]
    pack = bans.get(target_id)
    if not isinstance(pack, dict):
        return False
    if kind not in pack:
        return False
    pack.pop(kind, None)
    if not pack:
        bans.pop(target_id, None)
    return True

# ===== ЖАЛОБЫ =====

def add_report(reporter_id: str, target_id: str, reason_key: str, data: dict):
    reports = data["reports"]
    bans = data["bans"]

    reports[target_id] = int(reports.get(target_id, 0)) + 1

    # ❗ НИКАКОГО АВТОБАНА
    # просто гарантируем, что пользователь есть в bans
    if reports[target_id] >= int(data.get("max_reports", MAX_REPORTS)):
        _ensure_user_pack(bans, target_id)

# ===== ТЕКСТ ЖАЛОБЫ =====

def report_text(reporter_user, target_user, reason_key: str, reports_count: int) -> str:
    return (
        "🚨 *Жалоба*\n\n"
        f"От: `{reporter_user.id}` @{reporter_user.username or '—'}\n"
        f"На: `{target_user.id}` @{target_user.username or '—'}\n"
        f"Причина: *{reason_key}*\n"
        f"Жалоб на пользователя: *{reports_count}*\n"
    )

# ===== КЛАВИАТУРЫ =====

def report_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧨 Спам", callback_data="report_spam")],
        [InlineKeyboardButton("🤬 Оскорбления", callback_data="report_abuse")],
        [InlineKeyboardButton("🔞 Контент", callback_data="report_18")],
        [InlineKeyboardButton("🚫 Другое", callback_data="report_other")],
    ])


def admin_actions_keyboard(target_id: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚫 Бан 24ч", callback_data=f"admin_ban24_{target_id}"),
            InlineKeyboardButton("🔓 Разбан", callback_data=f"admin_unban_{target_id}"),
        ],
        [
            InlineKeyboardButton("🔇 Мут 30м", callback_data=f"admin_mute30_{target_id}"),
            InlineKeyboardButton("🔊 Размут", callback_data=f"admin_unmute_{target_id}"),
        ],
        [
            InlineKeyboardButton("👤 Профиль", callback_data=f"admin_profile_{target_id}"),
        ]
    ])
