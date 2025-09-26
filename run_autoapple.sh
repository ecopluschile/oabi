#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

# Activa venv
if [ -d "$HOME/.venv" ]; then
  source "$HOME/.venv/bin/activate"
else
  echo "No existe ~/.venv. Ejecuta: bash termux_install.sh"
  exit 1
fi

# Carga .env si existe
if [ -f ".env" ]; then
  set -a; source .env; set +a
fi

# Verifica script principal
if [ ! -f "autoapple_termux.py" ]; then
  echo "Falta autoapple_termux.py en el directorio actual."
  exit 1
fi

TOKEN="${1:-}"
if [ -n "$TOKEN" ]; then
  python autoapple_termux.py --token "$TOKEN"
else
  python autoapple_termux.py
fi
