# Публікація цього проєкту на GitHub — покроково

Файли вже підготовлені: є `.gitignore` (ключі й дані середовища не підуть у git), `LICENSE` (MIT),
`.github/workflows/ci.yml` (тести на GitHub), `docs/architecture.svg` (схема для README) і скрипт
`publish.sh`, який робить усі перевірки та перший коміт однією командою.

---

## 1. Що потрапить у репозиторій, а що — ні

**Потрапить (46 файлів):** `app/*.py`, `tests/*.py`, `dev/tradingagents/*` (заглушка для тестів),
`run.py`, `config.yaml`, `requirements.txt`, `.env.example`, `.gitignore`, `README.md`, `GITHUB.md`,
`LICENSE`, `publish.sh`, `pytest.ini`, `docs/architecture.svg`, `.github/workflows/ci.yml`, `data/book.example.json`.

**Не потрапить (і це важливо):**

| Що | Чому |
|---|---|
| `.env` | справжні ключі (LLM, Telegram, FRED) |
| `data/assistant.db` | журнал угод — ваші торгові рішення |
| `data/cache/` | кеш котирувань, сміття в репозиторії |
| `data/book.json` | ваш капітал і відкриті позиції |
| `data/outbox.log`, `data/reports/` | історія повідомлень і звітів |
| `__pycache__/`, `.pytest_cache/`, `.venv/` | артефакти Python |

Кожен рядок перших п'яти пунктів закритий у `.gitignore` — але **перед першим push перевірте це руками** (розділ 6).

---

## 2. Передумови

```bash
git --version      # потрібен будь-який git ≥ 2.28
python3 --version  # 3.11+ для тестів і запуску
```

Акаунт на GitHub — безкоштовний. Приватний чи публічний репозиторій — ваш вибір; для приватного
GitHub Actions теж безкоштовні (2000 хв/міс, а наші тести займають ~20 секунд).

---

## 3. Крок 0 — дві обов'язкові правки (1 хвилина)

У `LICENSE` стоїть заглушка замість вашого імені, а в `README.md` — заглушка `ВАШ-ЛОГІН` у посиланнях
бейджа. **Перед заливкою** виконайте в папці проєкту:

```bash
python3 - <<'PY'
import pathlib

# замініть на свій нікнейм на GitHub
NIK = "ваш-логін"
NAME = "Ваше Ім'я"      # як підписуватись у ліцензії

p = pathlib.Path("README.md")
p.write_text(p.read_text(encoding="utf-8").replace("ВАШ-ЛОГІН", NIK), encoding="utf-8")

p = pathlib.Path("LICENSE")
t = p.read_text(encoding="utf-8").replace("<РІК>", "2026").replace("<ВАШЕ ІМ'Я АБО НІК>", NAME)
p.write_text(t, encoding="utf-8")
print("готово:", NIK, "/", NAME)
PY
```

Перевірка: `grep -n "ВАШ" README.md LICENSE` не має нічого знайти.

---

## 4. Варіант A — без git, через браузер (найшвидший)

1. Відкрийте <https://github.com/new>.
2. **Repository name:** `ta-assistant`. Виберіть **Public** або **Private**.
3. **Не** ставте галочки «Add a README file», «Add .gitignore», «Choose a license» — усе це вже є в проєкті,
   і зайві файли створять конфлікт при першому push.
4. Натисніть **Create repository**.
5. На сторінці порожнього репозиторію — посилання **uploading an existing file**.
6. Розпакуйте архів `ta-assistant-github.zip` (лежить поруч із папкою проєкту) і **перетягніть у браузер
   вміст папки `ta-assistant`** — саме вміст, а не саму папку. GitHub підтримує перетягування тек із папками.
7. У полі коментаря напишіть, наприклад, `Перша версія: план за 60 хв до відкриття US-ринку` → **Commit changes**.
8. Перевірте, що залилось усе: `app/`, `tests/`, `.github/workflows/ci.yml`, `LICENSE`. Вкладка **Actions**
   має показати запущений workflow `tests` (зелений, ~20 с).

> Якщо папка `.github` не залилась (браузер інколи її «з'їдає»), створіть файл вручну:
> **Add file → Create new file**, ім'я `.github/workflows/ci.yml`, і вставте вміст однойменного файлу з проєкту.

Мінус цього варіанта: історії комітів майже немає, і оновлювати код доведеться так само руками. Для постійної
роботи краще варіант B.

---

## 5. Варіант B — git CLI (рекомендовано)

### 5.1. Створіть порожній репозиторій

<https://github.com/new> → назва `ta-assistant` → **без** README/.gitignore/license → **Create repository**.
Скопіюйте URL — він виглядає як `https://github.com/ВАШ-ЛОГІН/ta-assistant.git`.

### 5.2. Одна команда замість десяти

```bash
cd ta-assistant
chmod +x publish.sh          # один раз
./publish.sh https://github.com/ВАШ-ЛОГІН/ta-assistant.git
```

Скрипт зробить: пошук секретів серед файлів, які пішли б у коміт → прогін 140 тестів → `git init` і перший
коміт у гілці `main` → `git push -u origin main`. Якщо щось не так (знайшовся `.env`, впав тест) — він
зупиниться **до** push і скаже, що виправити.

### 5.3. Те саме вручну (якщо хочете бачити кожен крок)

```bash
cd ta-assistant
git init -b main
git add -A
git status --short          # ПЕРЕВІРТЕ: у списку не має бути .env, data/assistant.db, data/cache/
git commit -m "Помічник для торгівлі US-акціями: план за 60 хв до відкриття ринку"
git remote add origin https://github.com/ВАШ-ЛОГІН/ta-assistant.git
git push -u origin main
```

### 5.4. Авторизація при push

- **HTTPS:** пароль не приймається. Потрібен токен:
  GitHub → аватар → **Settings → Developer settings → Personal access tokens → Fine-grained tokens →
  Generate new token** → Repository access: *Only select repositories* → `ta-assistant` →
  Permissions → **Contents: Read and write** → Generate. Далі при `git push`:
  логін — ваш нік, пароль — цей токен (зберігається в credential manager, питатиме один раз).
- **SSH:** `ssh-keygen -t ed25519 -C "ви@example.com"` → `cat ~/.ssh/id_ed25519.pub` → GitHub →
  **Settings → SSH and GPG keys → New SSH key** → вставити → і використовувати remote
  `git@github.com:ВАШ-ЛОГІН/ta-assistant.git`.

---

## 6. Варіант C — GitHub CLI

```bash
# Linux: sudo apt install gh      macOS: brew install gh
gh auth login                                    # виберіть GitHub.com → HTTPS → браузер
cd ta-assistant
git init -b main && git add -A && git commit -m "Перша версія: план за 60 хв до відкриття"
gh repo create ta-assistant --public --source=. --remote=origin --push
```

---

## 7. Гігієна секретів (прочитайте хоча б раз)

**Ніколи не комітьте:** `.env`, токени, пароли, реальні номери рахунків. Шаблон для нових користувачів —
`.env.example` (у ньому лише назви змінних і коментарі, без значень).

Перевірка перед комітом:

```bash
git status --short            # що піде в коміт
git status --ignored --short # що ігнорується (тут мають бути .env і data/)
git check-ignore -v .env      # покаже правило, яке закриває .env
```

**Якщо `.env` уже залетів у коміт:**

1. Приберіть із індексу (файл на диску лишиться):
   `git rm --cached .env && echo ".env" >> .gitignore && git commit -m "Прибрати .env з репозиторію"`.
2. В історії він **усе ще є** — почистіть історію: `pip install git-filter-repo && git filter-repo --path .env --invert-paths`,
   потім `git push --force-with-lease`.
3. **Обов'язково відкличте (rotate) всі ключі, які були в тому файлі** — LLM, Telegram, FRED. Після публікації
   ключ вважається скомпрометованим назавжди; боти-сканери збирають їх за хвилини.

Для складної автоматизації в GitHub існують **Secrets** (Settings → Secrets and variables → Actions):
вони доступні лише під час виконання workflow і ніколи не потрапляють у git.

---

## 8. Що станеться після першого push

1. **Actions → тести.** Файл `.github/workflows/ci.yml` запускає `pytest tests -q` на Python 3.11, 3.12 і 3.13 —
   без ключів, без мережі, без TradingAgents (~20 с). Там же перевірка, що в репозиторії немає `.env`.
2. **Бейдж у README** почне світитися зеленим — саме для цього в розділі 3 ви замінили `ВАШ-ЛОГІН`.
3. **Сторінка репозиторію** покаже схему з `docs/architecture.svg` (GitHub рендерить SVG із репозиторію).

Рекомендовані налаштування (за бажанням): у полі **About** — опис
«Помічник: за 60 хв до відкриття US-ринку готує план (вхід/стоп/ціль/R:R/розмір) на добових свічках»,
Topics: `python`, `trading`, `stocks`, `yahoo-finance`, `fastapi`, `technical-analysis`, `risk-management`.
Безкоштовний тариф дає все потрібне: приватні репозиторії, Actions, Issues.

---

## 9. Оновлення коду надалі

```bash
git add -A
git commit -m "Що саме змінилося — коротко по-людськи"
git push                       # після першого push із -u origin main достатньо цього
git tag -a v1.0.0 -m "Перша робоча версія" && git push --tags   # якщо хочете версію-мітку
```

Дрібниця, яка врятує нерви: **спочатку тести, потім push** (`python3 -m pytest tests -q`). CI все одно
перевірить, але помітити помилку локально — швидше.

---

## 10. А якщо треба, щоб бот працював без вашого комп'ютера

GitHub Actions для цього **не підходить**: розклад там задається в UTC, cron запускається неточно
(може «з'їхати» на 10–30 хв), а 8:30 ET зсувається між літнім/зимовим часом. При цьому найважливіше —
щоб запуск був рівно за годину до відкриття.

Правильний варіант — дешевий VPS (2–5 $/міс) і systemd:

```ini
# /etc/systemd/system/trading-assistant.service
[Unit]
Description=Trading Assistant — план за 60 хв до відкриття US-ринку
After=network-online.target

[Service]
WorkingDirectory=/opt/ta-assistant
ExecStart=/usr/bin/python3 run.py --port 8000
Restart=always
Environment=TZ=Europe/Kyiv
User=trader

[Install]
WantedBy=multi-user.target
```

Планувальник усередині сервісу (APScheduler із таймзоною `America/New_York`) сам подбає про 8:30 ET —
жодних зовнішніх cron не треба. Для тих, хто все ж використовує системний cron:
`CRON_TZ=America/New_York` + `30 8 * * 1-5` (київський аналог у березні дає вже 9:30 ET — див. розділ
«Розклад» у README).

---

## 11. Чекліст перед першим push

- [ ] Замінив `ВАШ-ЛОГІН` у README та ім'я в LICENSE (розділ 3)
- [ ] `python3 -m pytest tests -q` → `140 passed`
- [ ] `git status --short` не показує `.env`, `data/assistant.db`, `data/cache/`, `data/book.json`
- [ ] Репозиторій створений **без** README/.gitignore/license (інакше буде конфлікт при першому push)
- [ ] Розумію, що робочий `.env` лишається тільки на моїй машині (і на VPS)
- [ ] Після push: Actions зелений, бейдж у README показує `tests: passing`

## 12. Якщо щось пішло не так

| Помилка | Що робити |
|---|---|
| `src refspec main does not match any` | ще немає коміту: `git add -A && git commit -m "..."`, або гілка називається `master` → `git branch -M main` |
| `Support for password authentication was removed` | потрібен токен або SSH (розділ 5.4) |
| `Updates were rejected because the remote contains work...` | репо створено з README: `git pull --rebase origin main` (дозволити різні історії: `--allow-unrelated-histories`) або `git push --force-with-lease`, якщо вміст репозиторію вам не потрібен |
| `Please tell me who you are` | `git config user.name "Ваше Ім'я" && git config user.email "ви@example.com"` |
| `Permission denied (publickey)` | SSH-ключ не доданий у GitHub (розділ 5.4) або використайте HTTPS + токен |
| `remote: error: File ... is 105 MB` | у коміт потрапив великий файл — приберіть і додайте в `.gitignore` |
| Файл `.env` видно на GitHub | розділ 7: прибрати з git, почистити історію, **відкликати ключі** |
