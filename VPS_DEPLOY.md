# Деплой на VPS (без изменений nginx первого сервиса)

Этот бот работает через Telegram long polling и **не принимает входящие HTTP-запросы**.  
Значит, для него не нужен `nginx`, и текущая конфигурация первого сервиса не затрагивается.

## 1. Полный Git-флоу (кратко)

1. Локально: коммитите изменения и пушите в удаленный репозиторий.
2. На VPS: делаете `git pull`, обновляете зависимости и перезапускаете `systemd`-сервис.
3. `nginx` не трогаете.

## 2. Первая выгрузка проекта в Git (локальная машина)

Если проект еще не в удаленном репозитории:

```bash
cd <PATH_TO_PROJECT>
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin <REPO_URL>
git push -u origin main
```

Если репозиторий уже есть, достаточно:

```bash
cd <PATH_TO_PROJECT>
git add .
git commit -m "Deploy update"
git push
```

## 3. Подготовка сервера

Команды ниже для Ubuntu/Debian:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
```

Создайте отдельного системного пользователя и директории:

```bash
sudo useradd --system --shell /usr/sbin/nologin --home-dir /home/joinguard --create-home joinguard || true
sudo mkdir -p /opt/join-guard-bot /var/lib/join-guard
sudo chown -R joinguard:joinguard /opt/join-guard-bot /var/lib/join-guard
```

## 4. Загрузка проекта на VPS через Git

На VPS (под пользователем `joinguard`) клонируйте репозиторий:

```bash
sudo -u joinguard git clone REPO_URL /opt/join-guard-bot
```

Если проект уже есть на VPS:

```bash
cd /opt/join-guard-bot
sudo -u joinguard git fetch --all
sudo -u joinguard git checkout main
sudo -u joinguard git pull --ff-only
sudo chown -R joinguard:joinguard /opt/join-guard-bot
```

Если `git clone` пишет `already exists and is not an empty directory`, сделайте:

```bash
sudo mv /opt/join-guard-bot /opt/join-guard-bot.bak.$(date +%F-%H%M%S)
sudo mkdir -p /opt/join-guard-bot
sudo chown joinguard:joinguard /opt/join-guard-bot
sudo -u joinguard git clone REPO_URL /opt/join-guard-bot
```

## 5. Установка зависимостей

```bash
cd /opt/join-guard-bot
sudo -u joinguard python3 -m venv .venv
sudo -u joinguard ./.venv/bin/pip install --upgrade pip
sudo -u joinguard ./.venv/bin/pip install -r requirements.txt
```

## 6. Настройка окружения

```bash
cd /opt/join-guard-bot
sudo -u joinguard cp .env.example .env
sudo -u joinguard nano .env
```

Рекомендуемые переменные:

```env
BOT_TOKEN=ваш_токен_бота
CHANNEL_ID=-1001234567890
ADMIN_IDS=111111111,222222222
DB_PATH=/var/lib/join-guard/join_guard.db
MIN_ACCOUNT_AGE_DAYS=30
MAX_CAPTCHA_ATTEMPTS=3
```

## 7. Запуск как systemd-сервис

В репозитории уже есть шаблон: `deploy/systemd/join-guard.service`.

```bash
sudo cp /opt/join-guard-bot/deploy/systemd/join-guard.service /etc/systemd/system/join-guard.service
sudo systemctl daemon-reload
sudo systemctl enable join-guard
sudo systemctl start join-guard
```

Проверка:

```bash
sudo systemctl status join-guard --no-pager
sudo journalctl -u join-guard -f
```

## 8. Обновление проекта (локально -> VPS)

### 8.1 Локально (выгрузка изменений в Git)

```bash
cd <PATH_TO_PROJECT>
git add .
git commit -m "Update bot"
git push
```

### 8.2 На VPS (загрузка обновлений из Git)

```bash
cd /opt/join-guard-bot
sudo -u joinguard git pull --ff-only
sudo -u joinguard ./.venv/bin/pip install -r requirements.txt
sudo systemctl restart join-guard
sudo systemctl status join-guard --no-pager
```

Если `requirements.txt` не менялся, шаг с `pip install` можно пропустить.

## 9. Полезные команды

```bash
sudo systemctl restart join-guard
sudo systemctl stop join-guard
sudo systemctl start join-guard
sudo journalctl -u join-guard -n 100 --no-pager
```

Если в логах есть ошибка `can't use getUpdates method while webhook is active`, выполните:

```bash
set -a; source /opt/join-guard-bot/.env; set +a
curl -s "https://api.telegram.org/bot${BOT_TOKEN}/deleteWebhook?drop_pending_updates=false"
sudo systemctl restart join-guard
```

## Что важно для вашего случая с двумя активными сервисами

- Бот не открывает порт и не конфликтует с портами `nginx`.
- Мы не меняем существующие `server`-блоки.
- Мы не делаем `nginx reload` в этой инструкции.
