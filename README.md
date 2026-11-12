# Great Energy 15є: бот графіку відключень ДТЕК (Київ, наб. Дніпровська, 15є)

Щогодини відкриває https://www.dtek-kem.com.ua/ua/shutdowns у прихованому браузері
(Playwright, щоб пройти антибот-захист і CSRF), бере чергу та графік для адреси
і, якщо графік змінився, надсилає повідомлення в Telegram.

## 1. Telegram
1. У @BotFather: `/newbot`, назва **Great Energy 15є**, username напр. `great_energy_15e_bot` → отримаєте токен.
2. Напишіть своєму боту будь-що, відкрийте `https://api.telegram.org/bot<ТОКЕН>/getUpdates`
   і візьміть `chat.id` (або напишіть @userinfobot).

## 2. Запуск на Windows
```
pip install -r requirements.txt
python -m playwright install chromium
copy .env.example .env      (і вписати токен та chat id)
python dtek_bot.py --test   (має прийти повідомлення)
```
Щогодини: Планувальник завдань → Створити завдання → Тригер «Щодня, повторювати кожну 1 год»
→ Дія: `run.bat`. Або просто `python dtek_bot.py --loop` у відкритому вікні.

## 3. Запуск у GitHub Actions (основний спосіб)
1. Settings → Secrets and variables → Actions → New repository secret:
   `TELEGRAM_TOKEN` (`TELEGRAM_CHAT_ID` необов'язковий: бот сам візьме чат того, хто написав йому /start).
2. Actions → «Great Energy 15є» → Run workflow (можна поставити галочку force, щоб одразу отримати графік).
3. Далі працює сам: кожні 15 хв перевіряє канал, раз на годину графік ДТЕК.
   Стан зберігається у `state.json` у репозиторії.

## Фактичний стан світла
Бот також читає публічний канал https://t.me/s/SvitloGreat (змінна `TG_CHANNEL`) і пересилає
кожен новий пост з позначкою 🟢/🔴. У режимі `--loop` канал перевіряється кожні 5 хв, графік щогодини.
