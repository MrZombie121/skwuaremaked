# 🚀 Інструкція із завантаження та розгортання SKYWATCH v2.0 на хостингу

Для хостингу (Render, Railway, Fly.io, Heroku, VPS / Ubuntu / Docker, cPanel Python App) усі необхідні файли зібрано у готовій папці: **`hosting_dist/`**.

---

## 📁 Що завантажувати на хостинг

Завантажте вміст папки **`hosting_dist`** (або запакуйте її у zip/архів).

### Структура папок і файлів для хостингу:
1. **`ui/`** — Фронтенд веб-інтерфейсу:
   - `ui/index.html` (веб-радар, HUD, тактичні панелі)
   - `ui/css/style.css` (темна військова тема)
   - `ui/js/app.js` (60 FPS рушій карти, Google Maps, Web Audio)
2. **`markers/`** — Тактичні PNG-іконки повітряних цілей (`shahed.png`, `rs.png`, `missile.png`, `ballistic.png`, `kab.png`, `recon.png`, `fpv.png`, `decoy.png`).
3. **`server/`** — Бекенд веб-сервера (`server/app.py` — FastAPI, WebSocket хаб, REST API).
4. **`core/`** — Тактичні обчислювальні модулі:
   - `geo_engine.py` (600+ населених пунктів України)
   - `nlp_parser.py` (мульти-звітний розбір повідомлень)
   - `deduplicator.py` (розведення цілей, конуси небезпеки, кінематика)
   - `telegram_service.py` (Telethon зв'язок і синхронізація 33 каналів)
   - `neptun_service.py` (потік додаткового джерела)
   - `simulator.py` (імітатор тривог)
   - `models.py` & `db.py` (Pydantic моделі та JSON БД)
5. **`data/`** — База даних JSON:
   - `channels.json` (33 підключені канали)
   - `settings.json` (налаштування сесії та папки)
   - `targets.json`, `history.json`, `messages_log.json`
6. **`config.py`** — Глобальна конфігурація з підтримкою змінних середовища `$PORT` та `$HOST`.
7. **`web_app.py`** — Точка входу веб-сервера.
8. **`requirements.txt`** — Список залежностей Python.
9. **`Procfile`** & **`Dockerfile`** — Конфіги автозапуску на хмарних хостингах.

---

## ⚙️ Команда запуску на хостингу

### Варіант А: Через Python
```bash
python web_app.py --no-browser
```
або через Uvicorn:
```bash
uvicorn server.app:app --host 0.0.0.0 --port 8080
```

### Варіант Б: Через Docker
```bash
docker-compose up -d --build
```

---

## 🔒 Змінні середовища (Environment Variables) на хостингу (за потреби):
- `PORT` = `8080` (або порт, що видає хостинг)
- `TG_API_ID` = `32502863`
- `TG_API_HASH` = `8b337b539d0bbe39e0d87c5d1f782f4f`
- `TG_SESSION_STRING` = `(рядок сесії із data/settings.json)`
