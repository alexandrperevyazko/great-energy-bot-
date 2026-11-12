"""
Бот, що перевіряє графік відключень ДТЕК (Київ) для однієї адреси
і надсилає повідомлення в Telegram, коли графік змінився.

Запуск: python dtek_bot.py            (одна перевірка, для планувальника)
        python dtek_bot.py --loop     (перевіряти кожну годину без зупинки)
        python dtek_bot.py --test     (надіслати поточний графік, навіть якщо він не змінився)

Налаштування через змінні середовища або файл .env поруч зі скриптом:
  TELEGRAM_TOKEN=123456:ABC...
  TELEGRAM_CHAT_ID=123456789
  DTEK_STREET=Дніпровська        (частина назви вулиці, як на сайті)
  DTEK_HOUSE=15є
"""
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state.json"
URL = "https://www.dtek-kem.com.ua/ua/shutdowns"


def load_env():
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


load_env()
TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
STREET_HINT = os.environ.get("DTEK_STREET", "Дніпровська")
HOUSE = os.environ.get("DTEK_HOUSE", "15є")

# Час у Києві (без залежності від tzdata на Windows)
def kyiv_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Kyiv"))
    except Exception:
        return datetime.now(timezone(timedelta(hours=2)))


# JS, що виконується всередині сторінки: так ми проходимо антибот-захист
# і автоматично отримуємо cookie та CSRF-токен, як звичайний браузер.
FETCH_JS = r"""
async ({streetHint, house, updateFact}) => {
  const ds = (typeof DisconSchedule !== 'undefined' ? DisconSchedule : window.DisconSchedule) || {};
  const streets = ds.streets || [];
  const norm = s => (s || '').toLowerCase().replace(/[’'`]/g, '');
  const hint = norm(streetHint);
  let candidates = streets.filter(s => norm(s).includes(hint));
  // перевага: набережна
  candidates.sort((a, b) => (norm(b).includes('наб') ? 1 : 0) - (norm(a).includes('наб') ? 1 : 0));
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
  const tried = [];
  for (const street of (candidates.length ? candidates : [streetHint])) {
    const body = new URLSearchParams();
    body.append('method', 'getHomeNum');
    body.append('data[0][name]', 'street');
    body.append('data[0][value]', street);
    body.append('data[1][name]', 'updateFact');
    body.append('data[1][value]', updateFact);
    const r = await fetch('/ua/ajax', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'X-Requested-With': 'XMLHttpRequest',
        'X-CSRF-Token': csrf,
      },
      body: body.toString(),
      credentials: 'include',
    });
    const text = await r.text();
    let json = null;
    try { json = JSON.parse(text); } catch (e) {}
    tried.push(street);
    if (!json || !json.data) continue;
    const keys = Object.keys(json.data);
    const key = keys.find(k => norm(k) === norm(house));
    if (key) {
      return {street, house: key, home: json.data[key], fact: json.fact || ds.fact || null,
              preset: ds.preset || null, updateTimestamp: json.updateTimestamp || null};
    }
  }
  return {error: 'address_not_found', tried, streetsCount: streets.length, similar: streets.filter(s => norm(s).includes('дніпр')).slice(0, 40)};
}
"""


def fetch_schedule():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            locale="uk-UA",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
        )
        page = ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=90_000)
        # чекаємо, поки антибот пропустить і сторінка підвантажить дані
        page.wait_for_function("() => typeof DisconSchedule !== 'undefined' && DisconSchedule.streets",
                               timeout=90_000)
        update_fact = kyiv_now().strftime("%d.%m.%Y %H:%M")
        result = page.evaluate(FETCH_JS, {"streetHint": STREET_HINT, "house": HOUSE,
                                          "updateFact": update_fact})
        browser.close()
    if result.get("error"):
        raise RuntimeError(f"Адресу не знайдено на сайті: {result}")
    print("Знайдено:", result.get("street"), result.get("house"), json.dumps(result.get("home"), ensure_ascii=False))
    return result


# ---------- форматування ----------
# Значення погодинних слотів на сайті ДТЕК:
#  yes – світло є; no – світла немає всю годину;
#  first – немає перші 30 хв; second – немає другі 30 хв;
#  maybe / mfirst / msecond – можливе відключення.
def hour_to_halves(v):
    v = (v or "yes").lower()
    return {
        "yes": ("on", "on"), "no": ("off", "off"),
        "first": ("off", "on"), "second": ("on", "off"),
        "maybe": ("maybe", "maybe"), "mfirst": ("maybe", "on"), "msecond": ("on", "maybe"),
    }.get(v, ("on", "on"))


def intervals(hours: dict):
    slots = []
    for h in range(1, 25):
        slots.extend(hour_to_halves(hours.get(str(h))))
    out, start = [], 0
    for i in range(1, 49):
        if i == 48 or slots[i] != slots[start]:
            out.append((slots[start], start, i))
            start = i
    return out


def hhmm(half):
    return f"{half // 2:02d}:{'30' if half % 2 else '00'}"


def format_day(hours):
    lines = []
    for state, a, b in intervals(hours):
        if state == "off":
            lines.append(f"🔴 {hhmm(a)}–{hhmm(b)} немає світла")
        elif state == "maybe":
            lines.append(f"🟡 {hhmm(a)}–{hhmm(b)} можливе відключення")
    return "\n".join(lines) if lines else "🟢 Відключень не заплановано"


def build_message(res):
    home = res["home"] or {}
    groups = home.get("sub_type_reason") or []
    group = groups[0] if groups else None
    head = f"📍 Київ, {res['street']}, {res['house']}"
    if group:
        head += f"\nЧерга: {group.replace('GPV', '')}"
    parts = [head]

    # Поточне (аварійне/планове) відключення саме зараз
    if home.get("sub_type") or home.get("start_date"):
        parts.append(f"⚠️ Зараз: {home.get('sub_type', 'відключення')}"
                     f"\nз {home.get('start_date', '?')} до {home.get('end_date', '?')}")

    fact = res.get("fact") or {}
    days = (fact.get("data") or {}) if isinstance(fact, dict) else {}
    if group and days:
        for ts in sorted(days, key=lambda x: int(x)):
            day_hours = (days[ts] or {}).get(group)
            if not day_hours:
                continue
            d = datetime.fromtimestamp(int(ts), timezone(timedelta(hours=2)))
            # timestamp ДТЕК – північ за Києвом; беремо дату з невеликим зсувом
            d = (d + timedelta(hours=3)).date()
            parts.append(f"📅 {d.strftime('%d.%m.%Y')}\n{format_day(day_hours)}")
    elif not group:
        parts.append("Для адреси не вказано черги на сайті.")
    else:
        parts.append("Графіка на найближчі дні на сайті поки немає.")

    if fact.get("update"):
        parts.append(f"🕒 Оновлено ДТЕК: {fact['update']}")
    return "\n\n".join(parts)


# ---------- стан і Telegram ----------
def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve_chat_id(state):
    """Якщо TELEGRAM_CHAT_ID не задано, беремо чат того, хто написав боту /start."""
    global CHAT_ID
    if CHAT_ID or not TOKEN:
        return
    if state.get("chat_id"):
        CHAT_ID = str(state["chat_id"])
        return
    try:
        r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates", timeout=30).json()
        for upd in r.get("result", []):
            chat = (upd.get("message") or {}).get("chat") or {}
            if chat.get("type") == "private":
                CHAT_ID = str(chat["id"])
                state["chat_id"] = chat["id"]
                send_telegram("👋 Вітаю! Це Great Energy 15є. Надсилатиму графік відключень "
                              "для наб. Дніпровська, 15є і повідомлення, коли світло зникає чи з'являється.")
                return
        print("Напишіть боту /start, щоб він дізнався, куди надсилати повідомлення.")
    except Exception as e:
        print(f"getUpdates: {e}", file=sys.stderr)


def send_telegram(text):
    if not TOKEN or not CHAT_ID:
        print("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID не задані. Повідомлення:\n" + text)
        return False
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": text}, timeout=30)
    r.raise_for_status()
    return True


# ---------- фактичний стан світла з Telegram-каналу ----------
TG_CHANNEL = os.environ.get("TG_CHANNEL", "SvitloGreat")
ON_WORDS = ("✅", "відновлено", "світло є", "з'явил", "з’явил", "появил", "увімкн", "включ", "дали світло", "🟢", "💡")
OFF_WORDS = ("❌", "відключено", "немає світла", "нема світла", "зникл", "вимкн", "відключ", "виключ", "🔴", "🕯")


def guess_status(text):
    t = text.lower()
    if t.lstrip().startswith("✅") or "відновлено" in t:
        return "🟢 Світло є"
    if t.lstrip().startswith("❌") or "відключено" in t:
        return "🔴 Світла немає"
    off = any(w in t for w in OFF_WORDS)
    on = any(w in t for w in ON_WORDS)
    if on and not off:
        return "🟢 Світло є"
    if off and not on:
        return "🔴 Світла немає"
    return "ℹ️ Оновлення"


def fetch_channel_posts():
    """Читає публічний веб-перегляд каналу t.me/s/<канал>. Повертає [(id, текст, час)]."""
    import html as _html
    import re
    r = requests.get(f"https://t.me/s/{TG_CHANNEL}", timeout=30,
                     headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    posts = []
    for block in re.split(r'(?=<div class="tgme_widget_message_wrap)', r.text)[1:]:
        m_id = re.search(r'data-post="[^"/]+/(\d+)"', block)
        m_txt = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
        m_time = re.search(r'<time datetime="([^"]+)"', block)
        if not (m_id and m_txt):
            continue
        txt = re.sub(r"<br\s*/?>", "\n", m_txt.group(1))
        txt = _html.unescape(re.sub(r"<[^>]+>", "", txt)).strip()
        posts.append((int(m_id.group(1)), txt, m_time.group(1) if m_time else ""))
    return posts


def check_channel(state):
    try:
        posts = fetch_channel_posts()
    except Exception as e:
        print(f"Канал {TG_CHANNEL} недоступний: {e}", file=sys.stderr)
        return
    if not posts:
        return
    last_id = state.get("channel_last_id")
    newest = max(p[0] for p in posts)
    if last_id is None:  # перший запуск: запам'ятати, але не спамити історією
        state["channel_last_id"] = newest
        return
    for pid, txt, ts in sorted(p for p in posts if p[0] > last_id):
        when = ""
        try:
            when = datetime.fromisoformat(ts).astimezone(kyiv_now().tzinfo).strftime("%d.%m %H:%M")
        except Exception:
            pass
        send_telegram(f"{guess_status(txt)} (за фактом){' · ' + when if when else ''}\n\n{txt}")
    state["channel_last_id"] = newest


def check_once(force=False):
    state = load_state()
    resolve_chat_id(state)
    if not CHAT_ID:
        save_state(state)
        return
    check_channel(state)
    save_state(state)
    # графік ДТЕК перевіряємо не частіше ніж раз на годину
    if not force and time.time() - state.get("schedule_checked_at", 0) < 55 * 60:
        print("Графік перевірявся менше години тому.")
        return
    res = fetch_schedule()
    state["schedule_checked_at"] = int(time.time())
    msg = build_message(res)
    # порівнюємо без рядка «Оновлено», щоб не слати те саме
    core = "\n".join(l for l in msg.splitlines() if not l.startswith("🕒"))
    digest = hashlib.sha256(core.encode("utf-8")).hexdigest()
    if force or state.get("digest") != digest:
        if not send_telegram(msg):
            save_state(state)
            return
        state.update({"digest": digest, "message": msg, "sent_at": kyiv_now().isoformat()})
        save_state(state)
        print("Графік змінився, повідомлення надіслано.")
    else:
        print("Без змін.")


def main():
    force = "--test" in sys.argv
    if "--loop" in sys.argv:
        # графік – щогодини, канал з фактом – кожні 5 хвилин
        last_schedule = 0.0
        while True:
            try:
                if time.time() - last_schedule >= 3600:
                    check_once(force)
                    force = False
                    last_schedule = time.time()
                else:
                    state = load_state()
                    check_channel(state)
                    save_state(state)
            except Exception as e:
                print(f"Помилка: {e}", file=sys.stderr)
            time.sleep(300)
    else:
        check_once(force or os.environ.get("FORCE_SEND") == "1")


if __name__ == "__main__":
    main()
