#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

echo "[1/5] Actualizando Termux..."
pkg update -y && pkg upgrade -y

echo "[2/5] Instalando Python y git..."
pkg install -y python git

echo "[3/5] Creando entorno virtual..."
if [ ! -d "$HOME/.venv" ]; then
  python -m venv "$HOME/.venv"
fi
source "$HOME/.venv/bin/activate"
pip install --upgrade pip wheel

echo "[4/5] Instalando dependencias Python..."
pip install -r requirements.txt

echo "[5/5] Instalando Chromium de Playwright (headless)..."
python -m playwright install chromium

echo "✅ Listo. Copia 'Modelo Comercial.xlsx' y revisa '.env'."
echo "   Ejecuta:  ./run_autoapple.sh 123456"
