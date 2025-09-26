# autoapple-termux (Multibanda ↔ OABI on-device)

Ejecución **on-device** (Android) con **Termux + Playwright (Chromium headless)**:
- Login Multibanda
- Extrae IDs con botón **"Confirmar en OABI"**
- Extrae y **normaliza**: marca, modelo y país usando `Modelo Comercial.xlsx`
- Login OABI (2FA por token)
- Inscripción Administrativa por IMEI
- Validación y **confirmación por ID** en Multibanda

> **Credenciales** por `.env` (no las hardcodees).  
> **Token 2FA** se pasa por CLI (`./run_autoapple.sh 123456`) o se solicita por input.

---

## 🔧 Requisitos
- Android con **Termux** (F-Droid recomendado)
- Espacio ≥ 1 GB (Chromium de Playwright)
- Conexión a Internet estable

---

## 🚀 Instalación
```bash
pkg update -y && pkg upgrade -y
termux-setup-storage
pkg install -y git
git clone https://github.com/<TU_USUARIO>/autoapple-termux.git
cd autoapple-termux
bash termux_install.sh
