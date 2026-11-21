# Bot de Cobros para Telegram — Instalación

## 1. Crear el bot en Telegram
1. En Telegram busca **@BotFather** y escribe `/newbot`.
2. Ponle nombre (ej: *Cobros Garcy*) y un usuario que termine en `bot` (ej: `cobrosgarcy_bot`).
3. BotFather te da un **TOKEN** (algo como `7123456789:AAH...`). Guárdalo, es secreto.

## 2. Saber tu ID de Telegram
Busca **@userinfobot** en Telegram, escríbele y te responde tu **ID** (un número).
Si vas a dejar que otra persona use el bot (un empleado), consigue también su ID.

## 3. Probarlo en un computador
1. Instala Python 3.10 o más nuevo (python.org). En Windows marca "Add Python to PATH".
2. Pon `bot.py` y `requirements.txt` en una carpeta. Abre una terminal en esa carpeta.
3. Instala lo necesario:
   ```
   pip install -r requirements.txt
   ```
4. Arranca el bot (cambia los valores por los tuyos):
   - Windows (PowerShell):
     ```
     $env:BOT_TOKEN="TU_TOKEN"; $env:ALLOWED_IDS="TU_ID"; python bot.py
     ```
   - Mac / Linux:
     ```
     BOT_TOKEN="TU_TOKEN" ALLOWED_IDS="TU_ID" python bot.py
     ```
   Para varias personas: `ALLOWED_IDS="111111,222222"`
5. Abre tu bot en Telegram y escribe `/start`.

El bot solo funciona mientras el programa esté abierto. Los datos quedan en el archivo `cobros.db`.

## 4. Dejarlo encendido 24/7 en Oracle Cloud (gratis)
1. Oracle → ☰ → **Compute → Instances → Create instance**. Nombre: `bot-cobros`.
2. **Image**: Canonical **Ubuntu 24.04**. **Shape**: Ampere `VM.Standard.A1.Flex`, 1 OCPU y 6 GB
   (o `VM.Standard.E2.1.Micro`). Debe decir *Always Free-eligible*.
3. **SSH keys**: No SSH keys. **Networking**: lo que viene por defecto.
4. **Show advanced options → Management → Initialization script → Paste cloud-init script** y pega
   (cambia TU_TOKEN y TU_ID):
   ```
   #!/bin/bash
   apt-get update -qq && apt-get install -y -qq git
   git clone -q https://github.com/guajiro19762011-ai/bot-cobros.git /tmp/bc
   bash /tmp/bc/servidor/instalar.sh "TU_TOKEN" "TU_ID" > /var/log/bot-cobros-instalacion.log 2>&1
   ```
5. **Create**. En unos 10 minutos escribe `/start` a tu bot.

El bot se reinicia solo si se cae, baja mejoras del código a las 3:00 a.m. y te manda un
respaldo de la base de datos todas las noches a las 10:00 p.m.

## 5. Cómo se usa (orden recomendado)
1. `/nuevoproducto` → crea tus productos.
2. `/compra` → registra la mercancía que compras (sube el stock).
3. `/nuevocliente` → crea clientes.
4. `/fiar` → carga las deudas que ya tenías anotadas en el cuaderno (con su fecha real).
5. `/venta` → cada venta, de contado o a crédito (con fecha límite de pago).
6. `/abono` → cada vez que un cliente paga. Se descuenta primero de la deuda más vieja.

Consultas: `/deuda Juan`, `/cliente Juan`, `/deudores`, `/vencidos`, `/inventario`, `/resumen mes`.
También puedes escribir normal: *cuánto debe Juan*, *inventario*, *vencidos*.

Todos los días a las **7:30 a.m.** el bot te avisa quién vence hoy, mañana y quién está vencido.
