import json
import os

DATA_FILE = "data/data.json"  # у тебя так и должно быть

def _default():
    return {
        "profiles": {},
        "dialogs": {},
        "queue": [],
        "bans": {},
        "reports": {}
    }

def load_data():
    # если файла нет — создаём дефолт
    if not os.path.exists(DATA_FILE):
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        data = _default()
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return data

    # читаем файл
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            text = f.read().strip()
            if not text:
                return _default()
            data = json.loads(text)
    except Exception:
        return _default()

    # гарантируем ключи
    base = _default()
    if isinstance(data, dict):
        base.update(data)
    for k in base:
        if base[k] is None:
            base[k] = _default()[k]
    return base

def save_data(profiles, dialogs, queue, bans, reports):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    data = {
        "profiles": profiles,
        "dialogs": dialogs,
        "queue": queue,
        "bans": bans,
        "reports": reports
    }
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
