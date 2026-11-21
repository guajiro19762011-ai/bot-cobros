#!/usr/bin/env bash
# Instalador del Bot de Cobros en un servidor Ubuntu (Oracle Cloud).
# Uso:  sudo bash instalar.sh BOT_TOKEN ALLOWED_IDS
set -euo pipefail
BOT_TOKEN="${1:?Falta el token del bot}"
ALLOWED_IDS="${2:?Falta tu ID de Telegram}"
REPO="https://github.com/guajiro19762011-ai/bot-cobros.git"

apt-get update -qq && apt-get install -y -qq git python3 python3-venv tzdata >/dev/null
timedatectl set-timezone America/Bogota || true

id cobros >/dev/null 2>&1 || useradd -r -m -d /opt/bot-cobros -s /usr/sbin/nologin cobros
rm -rf /opt/bot-cobros/app
git clone -q "$REPO" /opt/bot-cobros/app
mkdir -p /opt/bot-cobros/datos            # la base de datos vive aquí y nunca se borra
python3 -m venv /opt/bot-cobros/venv
/opt/bot-cobros/venv/bin/pip install -q -r /opt/bot-cobros/app/requirements.txt
chown -R cobros:cobros /opt/bot-cobros

cat > /etc/bot-cobros.env <<ENV
BOT_TOKEN=$BOT_TOKEN
ALLOWED_IDS=$ALLOWED_IDS
DB_PATH=/opt/bot-cobros/datos/cobros.db
ENV
chmod 600 /etc/bot-cobros.env

cat > /etc/systemd/system/bot-cobros.service <<UNIT
[Unit]
Description=Bot de Cobros (Telegram)
After=network-online.target
Wants=network-online.target

[Service]
User=cobros
WorkingDirectory=/opt/bot-cobros/app
EnvironmentFile=/etc/bot-cobros.env
ExecStart=/opt/bot-cobros/venv/bin/python bot.py
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now bot-cobros

# Cada día a las 3:00 a.m. baja las mejoras del código y reinicia el bot
cat > /etc/cron.d/bot-cobros <<CRON
0 3 * * * root cd /opt/bot-cobros/app && git pull -q && /opt/bot-cobros/venv/bin/pip install -q -r requirements.txt && chown -R cobros:cobros /opt/bot-cobros && systemctl restart bot-cobros
CRON
echo "✅ Bot de Cobros instalado y corriendo."
