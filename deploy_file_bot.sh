#!/usr/bin/env bash
# Запуск:
# chmod +x deploy_file_bot.sh
# ./deploy_file_bot.sh
#
# После запуска:
# 1. Напишите тестовому MAX-боту /start
# 2. Отправьте аудиофайл
# 3. Бот вернёт file_id/token/raw данные

set -e

SERVICE_NAME="max-file-data-bot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "Рабочая директория: $SCRIPT_DIR"

if [ ! -f "get_file_data_bot.py" ]; then
  echo "Ошибка: get_file_data_bot.py не найден рядом с deploy_file_bot.sh"
  exit 1
fi

if [ ! -f ".env" ]; then
  cat > .env.example <<'EOF'
MAX_BOT_TOKEN=your_test_max_bot_token_here
ADMIN_IDS=
EOF
  echo "Создайте .env и укажите MAX_BOT_TOKEN"
  exit 1
fi

echo "Создаю виртуальное окружение..."
python3 -m venv venv

echo "Активирую venv..."
# shellcheck disable=SC1091
source venv/bin/activate

echo "Обновляю pip..."
pip install -U pip

echo "Устанавливаю зависимости..."
pip install maxapi python-dotenv

CURRENT_USER="$(id -un)"
PYTHON_PATH="${SCRIPT_DIR}/venv/bin/python"
BOT_PATH="${SCRIPT_DIR}/get_file_data_bot.py"
ENV_PATH="${SCRIPT_DIR}/.env"

echo "Создаю systemd service: ${SERVICE_FILE}"
sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=MAX File Data Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=${SCRIPT_DIR}
EnvironmentFile=${ENV_PATH}
ExecStart=${PYTHON_PATH} ${BOT_PATH}
Restart=always
RestartSec=5
User=${CURRENT_USER}

[Install]
WantedBy=multi-user.target
EOF

echo "Перезагружаю systemd..."
sudo systemctl daemon-reload

echo "Включаю сервис ${SERVICE_NAME}..."
sudo systemctl enable "$SERVICE_NAME"

echo "Перезапускаю сервис ${SERVICE_NAME}..."
sudo systemctl restart "$SERVICE_NAME"

echo
echo "Готово. Команды для проверки:"
echo "sudo systemctl status ${SERVICE_NAME}"
echo "sudo journalctl -u ${SERVICE_NAME} -f"
