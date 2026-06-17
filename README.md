# Prediction Game Bot (play-money) + Mini App

Телеграм-игра «угадай исход» в духе Polymarket, но на **игровых фантиках**.
Фантики нельзя купить и нельзя вывести — это игра на рейтинг, не ставки на деньги.
Никакого кошелька, крипты, депозитов и выплат тут нет.

## Что умеет
- Бинарные рынки ДА/НЕТ; «шансы» плавают от объёма ставок (parimutuel-пул).
- При закрытии победившая сторона делит **весь пул** пропорционально ставкам.
- Ежедневный бонус со стриком (`/bonus`), рейтинг (`/top`), профиль с местом (`/me`).
- Админ: создать/заморозить/рассчитать рынок.
- **Mini App** — веб-интерфейс внутри Telegram (рынки, ставки, рейтинг, бонус).

## Команды
**Игрок:** `/start`, `/markets`, `/balance`, `/bonus`, `/me`, `/top`, `/app`
**Админ:** `/new <вопрос>`, `/close <id>`, `/resolve`

---

## 1. Запуск бота
```bash
cd ~/Desktop/predict_play_bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # впиши BOT_TOKEN и свой ADMIN_IDS
python bot.py
```
Свой id для `ADMIN_IDS` узнаёшь у @userinfobot. Без `WEBAPP_URL` бот работает как
обычный чат-бот — мини-апп просто выключен.

## 2. Mini App (сайт через GitHub Pages)
Фронт — это статический `webapp/index.html`. Бэкенд — API внутри `bot.py`.

**Шаг 1. Подними API с HTTPS.** Telegram открывает мини-апп только по HTTPS, и фронт
должен ходить в API тоже по HTTPS. Варианты:
- быстрый тест: туннель к локальному `bot.py` →
  `cloudflared tunnel --url http://localhost:8080` (даст https-адрес);
- боевой: VPS + домен + reverse-proxy (nginx/caddy) на `API_PORT`.

**Шаг 2. Пропиши адрес API во фронт.** В `webapp/index.html` замени
`REPLACE_WITH_YOUR_API_URL` на адрес из шага 1 (без слэша в конце).

**Шаг 3. Выложи фронт на GitHub Pages.**
```bash
git init && git add . && git commit -m "prediction mini app"
git branch -M main
git remote add origin https://github.com/<логин>/<репо>.git
git push -u origin main
```
GitHub → репозиторий → Settings → Pages → Source: `main` / `/ (root)` или папка `/webapp`.
Получишь URL вида `https://<логин>.github.io/<репо>/`.

**Шаг 4. Свяжи с ботом.** В `.env` пропиши `WEBAPP_URL=https://<логин>.github.io/<репо>/webapp/`
(или корень, смотря что выбрал в Pages), перезапусти `bot.py`. У бота появится кнопка-меню
«🔮 Играть» и команда `/app`. Также можно прописать Web App в @BotFather → Bot Settings → Menu Button.

## Безопасность
- `BOT_TOKEN` — только в `.env`. В чат, скрины и git не выкладывать.
- **Не вшивай токены в URL git-remote** (`https://user:TOKEN@github.com/...`). Используй
  `gh auth login` / SSH / credential helper. Если вшил — отзови токен на GitHub и пересоздай.
- `.env` и `*.db` уже в `.gitignore`.
