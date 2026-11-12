"""Діагностика: зберігає, що бачить браузер на сайті ДТЕК і в каналі."""
import json, pathlib, requests
from playwright.sync_api import sync_playwright
out = pathlib.Path("debug"); out.mkdir(exist_ok=True)
r = requests.get("https://t.me/s/SvitloGreat", timeout=30, headers={"User-Agent": "Mozilla/5.0"})
(out / "tg.html").write_text(r.text, encoding="utf-8")
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(locale="uk-UA", user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
    pg = ctx.new_page()
    log = []
    pg.on("response", lambda resp: log.append(f"{resp.status} {resp.request.method} {resp.url}"))
    pg.goto("https://www.dtek-kem.com.ua/ua/shutdowns", wait_until="domcontentloaded", timeout=90000)
    pg.wait_for_timeout(25000)
    (out / "page.html").write_text(pg.content(), encoding="utf-8")
    pg.screenshot(path=str(out / "page.png"), full_page=True)
    info = pg.evaluate("""() => ({title: document.title, url: location.href,
      keys: Object.keys(window).filter(k => /discon|schedule|fact|preset|street/i.test(k)),
      ds: window.DisconSchedule ? Object.keys(window.DisconSchedule) : null,
      csrf: !!document.querySelector('meta[name=csrf-token]')})""")
    (out / "info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "network.txt").write_text("\n".join(log), encoding="utf-8")
    b.close()
print(json.dumps(info, ensure_ascii=False))

import re
html = (out / "page.html").read_text(encoding="utf-8")
print("HTML len", len(html))
for kw in ["DisconSchedule", "getHomeNum", "/ajax", "csrf", "streets", "sub_type_reason", "preset", "fact", "Дніпровськ"]:
    for m in list(re.finditer(re.escape(kw), html))[:3]:
        print(f"--- {kw} @{m.start()}:", html[max(0, m.start()-200):m.start()+300].replace("\n", " "))
print("=== NETWORK")
print("\n".join(l for l in (out / "network.txt").read_text().splitlines() if not re.search(r"\.(png|jpe?g|svg|woff2?|css|gif|webp|ico)", l)))
tg = (out / "tg.html").read_text(encoding="utf-8")
print("=== TG len", len(tg))
for m in list(re.finditer(r'tgme_widget_message_text[^>]*>(.*?)</div>', tg, re.S))[-6:]:
    print("POST:", re.sub(r"<[^>]+>", " ", m.group(1))[:300])
