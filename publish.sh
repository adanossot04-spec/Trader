#!/usr/bin/env bash
# publish.sh — підготовка проєкту до публікації на GitHub.
#
#   ./publish.sh                      перевірки + локальний коміт (нікуди не пушить)
#   ./publish.sh <git-remote-url>     те саме + push у вказаний репозиторій
#   ./publish.sh --no-tests <url>     не проганяти тести (швидко, але без гарантій)
#
# Що робить скрипт:
#   1) шукає секрети й дані середовища серед файлів, які пішли б у git;
#   2) проганяє 140 офлайнових тестів (без мережі та ключів);
#   3) git init (за потреби) + перший коміт у гілці main;
#   4) за наявності URL — додає remote origin і робить push.
set -euo pipefail

# ── кольори, якщо це термінал ────────────────────────────────────────────────
if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'
else B=''; G=''; Y=''; R=''; N=''; fi
say()  { printf '%s\n' "${B}$*${N}"; }
ok()   { printf '%s\n' "${G}✓${N} $*"; }
warn() { printf '%s\n' "${Y}!${N} $*"; }
die()  { printf '%s\n' "${R}✗ $*${N}" >&2; exit 1; }

RUN_TESTS=1
REMOTE=''
for arg in "$@"; do
  case "$arg" in
    --no-tests) RUN_TESTS=0 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    -*) die "Невідомий прапорець: $arg" ;;
    *)  REMOTE="$arg" ;;
  esac
done

cd "$(dirname "$0")"
say "── Публікація $(basename "$PWD") на GitHub ───────────────────────────────"

command -v git >/dev/null || die "Не знайдено git. Встановіть: sudo apt install git (або brew install git)"
command -v python3 >/dev/null || die "Не знайдено python3 (потрібен лише для тестів)."

# ── 1. Ініціалізація репозиторію та індексу ─────────────────────────────────
if [ ! -d .git ]; then
  git init -q -b main 2>/dev/null || { git init -q; git checkout -q -b main; }
  ok "створено git-репозиторій (гілка main)"
else
  ok "репозиторій уже існує — оновлюю індекс"
fi

git add -A
STAGED="$(git diff --cached --name-only)"

if [ -z "$STAGED" ]; then
  warn "немає змін для коміту — все вже закомічено"
else
  # ── 2. Гігієна: що саме потрапляє в коміт ──────────────────────────────────
  if printf '%s\n' "$STAGED" | grep -vE '^\.env\.(example|sample|template)$' | grep -qxE '\.env(\..*)?'; then
    die "у коміт потрапив файл із справжніми ключами (.env)! Приберіть його:
      git rm --cached .env && echo '.env' >> .gitignore   # і перевірте, що в .gitignore є «.env»"
  fi
  if printf '%s\n' "$STAGED" | grep -qE '^data/(assistant\.db|cache/|reports/|book\.json)'; then
    die "у коміт потрапили дані середовища (журнал/кеш/портфель). Перевірте .gitignore і: git rm -r --cached data"
  fi
  if printf '%s\n' "$STAGED" | grep -qE '\.(pem|key|p12)$'; then
    die "у коміт потрапив приватний ключ (*.pem/*.key/*.p12)"
  fi

  SECRETS="$(printf '%s\n' "$STAGED" | while IFS= read -r f; do
      [ -f "$f" ] || continue
      grep -InE 'sk-[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{30,}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}|[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY-----' "$f" 2>/dev/null || true
    done)"
  if [ -n "$SECRETS" ]; then
    printf '%s\n' "$SECRETS"
    die "схоже на справжній ключ у файлах вище — приберіть його перед комітом"
  fi
  ok "секретів і даних середовища в коміті немає ($(printf '%s\n' "$STAGED" | wc -l | tr -d ' ') файлів)"
fi

# ── 3. Тести ────────────────────────────────────────────────────────────────
if [ "$RUN_TESTS" = 1 ]; then
  if python3 -c "import pytest" 2>/dev/null; then
    say "── тести ─────────────────────────────────────────────────────────────────"
    if python3 -m pytest tests -q >/tmp/publish-tests.log 2>&1; then
      SUMMARY="$(grep -E '[0-9]+ (passed|failed|error)' /tmp/publish-tests.log | tail -1)"
      ok "${SUMMARY:-тести пройшли}${SUMMARY:+ — усі тести пройшли}"
    else
      tail -30 /tmp/publish-tests.log
      die "тести не проходять — коміт скасовано (див. /tmp/publish-tests.log)"
    fi
  else
    warn "pytest не встановлено: pip install -r requirements.txt (тести пропущено)"
  fi
fi

# ── 4. Коміт ────────────────────────────────────────────────────────────────
if ! git config user.email >/dev/null 2>&1 || [ -z "$(git config user.email || true)" ]; then
  git config user.name  "$(git config --global user.name  || echo 'Trading Assistant')"
  git config user.email "$(git config --global user.email || echo 'you@example.com')"
  warn "не було git-ідентичності — поставив локальну. Змініть:
      git config user.name 'Ваше Ім'я' && git config user.email 'ви@example.com'"
fi

if [ -n "$STAGED" ]; then
  git commit -q -m "Помічник для торгівлі US-акціями: план за 60 хв до відкриття ринку

- запуск о 8:30 ET (пн–пт) = рівно за годину до відкриття, таймзона біржі
- добові свічки лише закритих сесій (Yahoo без ключа → Twelve Data → Alpha Vantage → synthetic)
- рівні: вхід / стоп / ціль / R:R, розмір від ризику, текст заявки брокеру
- премаркет-ґеп 04:00–09:30 ET: угода скасовується автоматично, якщо ціна проти плану
- детермінований ризик-гейт без LLM; LLM пише лише коментар
- журнал SQLite, Telegram/outbox, дашборд і REST API українською
- 140 офлайнових тестів"
  ok "коміт створено: $(git log -1 --oneline)"
fi

# ── 5. Push (якщо дали URL) ─────────────────────────────────────────────────
if [ -n "$REMOTE" ]; then
  case "$REMOTE" in
    *github.com*) ;;
    *) warn "URL не схожий на github.com — продовжую, бо може бути SSH-аліас" ;;
  esac
  if git remote get-url origin >/dev/null 2>&1; then
    git remote set-url origin "$REMOTE"
  else
    git remote add origin "$REMOTE"
  fi
  git branch -M main
  say "── push ──────────────────────────────────────────────────────────────────"
  git push -u origin main
  ok "готово. Далі: Actions → переконайтесь, що workflow «tests» зелений"
else
  say "── локально все готово ───────────────────────────────────────────────────"
  cat <<'EOT'
Тепер створіть порожній репозиторій на https://github.com/new (без README і .gitignore)
і виконайте одну з команд:

  # HTTPS (запитає логін і токен замість пароля — див. GITHUB.md, розділ 4)
  git remote add origin https://github.com/ВАШ-ЛОГІН/ta-assistant.git && git push -u origin main

  # SSH (якщо вже додали ключ у GitHub)
  git remote add origin git@github.com:ВАШ-ЛОГІН/ta-assistant.git && git push -u origin main

Або просто перезапустіть цей скрипт із URL:
  ./publish.sh https://github.com/ВАШ-ЛОГІН/ta-assistant.git
EOT
fi
