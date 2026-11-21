#!/data/data/com.termux/files/usr/bin/bash
# Arranca el bot de cobros en Termux y lo reinicia solo si se cae.
cd ~/bot || exit 1
termux-wake-lock            # evita que Android duerma a Termux
source ~/bot/config.sh      # aquí están BOT_TOKEN y ALLOWED_IDS
export DB_PATH=~/bot/cobros.db
while true; do
  python bot.py >> ~/bot/registro.log 2>&1
  echo "$(date) - el bot se detuvo, reiniciando en 10 s..." >> ~/bot/registro.log
  sleep 10
done
