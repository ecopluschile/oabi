#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
autoapple_termux.py — On-device (Android/Termux) con Playwright (Chromium headless)

Flujo:
 1) Login Multibanda
 2) Extrae IDs con "Confirmar en OABI"
 3) Visita cada ID → extrae campos → normaliza marca/modelo/país → genera temp_oabi.xlsx
 4) Login OABI (2FA via --token o input)
 5) Inscripción Administrativa
 6) Validación por IMEI
 7) Confirmación por ID en Multibanda

Credenciales via ENV:
  MB_USER, MB_PASS, OABI_USER, OABI_PASS

Token 2FA via --token (o input interactivo)
"""

import os, re, time, argparse, unicodedata, sys
from urllib.parse import urljoin
from difflib import get_close_matches
import pandas as pd
from dotenv import load_dotenv

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ========================= CONFIG =========================
ARCHIVO_TEMP = "temp_oabi.xlsx"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CATALOGO_MODELOS_XLSX = os.path.join(BASE_DIR, "Modelo Comercial.xlsx")

MB_BASE_URL = "https://multibanda.com/"  # producción
OABI_LOGIN_URL = "https://www.oabi.cl/sistema-oabi/login"

NAV_TIMEOUT = 30_000
DEF_TIMEOUT = 12_000
CONTENT_TIMEOUT = 15_000

# Carga .env si existe
load_dotenv(override=True)
MB_USER = os.getenv("MB_USER", "")
MB_PASS = os.getenv("MB_PASS", "")
OABI_USER = os.getenv("OABI_USER", "")
OABI_PASS = os.getenv("OABI_PASS", "")

if not (MB_USER and MB_PASS and OABI_USER and OABI_PASS):
    print("⚠️ Faltan variables de entorno MB_USER/MB_PASS/OABI_USER/OABI_PASS (en .env o exportadas).")
    # No salimos para permitir ver ayuda/--help; pero fallará al loguear.

KNOWN_BRANDS = {
    "APPLE","SAMSUNG","XIAOMI","HUAWEI","MOTOROLA","NOKIA","SONY","OPPO","VIVO",
    "REALME","GOOGLE","ZTE","LG","ONEPLUS","ALCATEL","TECNO","INFINIX","HONOR",
    "BLU","CAT","LENOVO","ASUS","MEIZU","MICROSOFT","BLACKBERRY","PANASONIC",
    "SHARP","TCL","UMIDIGI","ULEFONE","DOOGEE"
}

BRAND_HINTS = [
    (r'(?i)\breno\s*\d', "Oppo"),
    (r'(?i)\bgalaxy\b', "Samsung"),
    (r'(?i)\biphone\b', "Apple"),
    (r'(?i)\bredmi\b', "Xiaomi"),
    (r'(?i)\bmi\s*\d', "Xiaomi"),
    (r'(?i)\bpixel\b', "Google"),
]

# ========================= UTILIDADES =========================
def sleep(s: float): time.sleep(s)

def _strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or "") if unicodedata.category(c) != 'Mn')

def _norm_key(s: str) -> str:
    s = _strip_accents(s).strip().upper()
    s = s.replace("|", " ")
    s = re.sub(r'\s+', ' ', s)
    return s

def _pretty_cap(s: str) -> str:
    s = (s or "").strip()
    if not s: return s
    u = _norm_key(s)
    if u == "APPLE":  return "Apple"
    if u == "IPHONE": return "iPhone"
    out = []
    for w in s.split():
        wu = w.upper()
        if wu in {"5G","4G","3G","LTE","NR","128","256","512","GB"}:
            out.append(wu)
        elif wu == "IPHONE":
            out.append("iPhone")
        elif len(w) <= 2:
            out.append(wu)
        else:
            out.append(w.capitalize())
    return " ".join(out)

def _strip_brand_prefix(modelo_u: str, marca_u: str) -> str:
    t = (modelo_u or "").strip()
    if not t: return t
    if marca_u == "APPLE":
        for pref in ("APPLE ", "IPHONE "):
            if t.startswith(pref): t = t[len(pref):].strip()
        return t
    if marca_u == "SAMSUNG":
        for pref in ("SAMSUNG GALAXY ", "SAMSUNG "):
            if t.startswith(pref): t = t[len(pref):].strip()
        return t
    if t.startswith(marca_u + " "):
        t = t[len(marca_u) + 1:].strip()
    return t

def _fix_model_spacing_specific(modelo: str) -> str:
    s = (modelo or "").strip()
    s = re.sub(r'(?i)\breno\s*(\d+)\b', r'Reno \1', s)
    s = re.sub(r'(?i)\biphone\s*(\d+)\b', r'iPhone \1', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s

def _finalize_model_case(modelo: str) -> str:
    s = (modelo or "").strip()
    s = re.sub(r'(?i)\b(5g|4g|3g|lte|nr|128|256|512|gb)\b', lambda m: m.group(1).upper(), s)
    words = s.split()
    fixed = []
    for w in words:
        up = w.upper()
        low = w.lower()
        if up in {"5G","4G","3G","LTE","NR","128","256","512","GB"}:
            fixed.append(up)
        elif low == "iphone":
            fixed.append("iPhone")
        elif low == "reno":
            fixed.append("Reno")
        else:
            fixed.append(w.capitalize() if len(w) > 2 else w.upper())
    return " ".join(fixed)

def infer_brand_from_model(modelo_raw: str):
    mk = modelo_raw or ""
    for pat, brand in BRAND_HINTS:
        if re.search(pat, mk):
            return brand
    return None

# ====== País ======
def _norm_country_key(s: str) -> str:
    s = _strip_accents(s or "")
    s = s.upper()
    s = re.sub(r'[^A-Z0-9]+', ' ', s).strip()
    s = re.sub(r'\s+', ' ', s)
    return s

PAIS_MAP = {
    "USA":"Estados Unidos","U S A":"Estados Unidos","US":"Estados Unidos","U S":"Estados Unidos",
    "UNITED STATES":"Estados Unidos","UNITED STATES OF AMERICA":"Estados Unidos","EEUU":"Estados Unidos",
    "EE UU":"Estados Unidos","E E U U":"Estados Unidos","U S OF A":"Estados Unidos",
    "UNITED KINGDON":"Reino Unido","UNITED KINGDOM":"Reino Unido","UK":"Reino Unido","U K":"Reino Unido",
    "GREAT BRITAIN":"Reino Unido","GB":"Reino Unido","ENGLAND":"Reino Unido","SCOTLAND":"Reino Unido","WALES":"Reino Unido",
    "NORTHERN IRELAND":"Reino Unido",
    "CANADA":"Canadá","MEXICO":"México","MEJICO":"México",
    "BRAZIL":"Brasil","ARGENTINA":"Argentina","CHILE":"Chile","COLOMBIA":"Colombia","PERU":"Perú",
    "VENEZUELA":"Venezuela","ECUADOR":"Ecuador","BOLIVIA":"Bolivia","PARAGUAY":"Paraguay","URUGUAY":"Uruguay",
    "SPAIN":"España","PORTUGAL":"Portugal","FRANCE":"Francia","GERMANY":"Alemania","DEUTSCHLAND":"Alemania",
    "ITALY":"Italia","NETHERLANDS":"Países Bajos","HOLLAND":"Países Bajos","NL":"Países Bajos",
    "BELGIUM":"Bélgica","BELGIQUE":"Bélgica","BELGIE":"Bélgica",
    "SWITZERLAND":"Suiza","SUISSE":"Suiza","SCHWEIZ":"Suiza","SVIZZERA":"Suiza",
    "AUSTRIA":"Austria","SWEDEN":"Suecia","NORWAY":"Noruega","DENMARK":"Dinamarca","FINLAND":"Finlandia",
    "IRELAND":"Irlanda","EIRE":"Irlanda","GREECE":"Grecia","POLAND":"Polonia","POLSKA":"Polonia",
    "CZECH REPUBLIC":"Chequia","CZECHIA":"Chequia","HUNGARY":"Hungría","ROMANIA":"Rumanía",
    "RUSSIA":"Rusia","RUSSIAN FEDERATION":"Rusia","UKRAINE":"Ucrania","TURKEY":"Turquía","TURKIYE":"Turquía",
    "CHINA":"China","PRC":"China","MAINLAND CHINA":"China","HONG KONG":"Hong Kong (China)",
    "MACAU":"Macao (China)","MACAO":"Macao (China)","TAIWAN":"Taiwán","JAPAN":"Japón",
    "SOUTH KOREA":"Corea del Sur","REPUBLIC OF KOREA":"Corea del Sur","KOREA":"Corea del Sur",
    "NORTH KOREA":"Corea del Norte","DPRK":"Corea del Norte","INDIA":"India","PAKISTAN":"Pakistán",
    "BANGLADESH":"Bangladés","SRI LANKA":"Sri Lanka","NEPAL":"Nepal","INDONESIA":"Indonesia",
    "PHILIPPINES":"Filipinas","MALAYSIA":"Malasia","SINGAPORE":"Singapur","THAILAND":"Tailandia",
    "VIETNAM":"Vietnam","VIET NAM":"Vietnam","CAMBODIA":"Camboya","KAMPUCHEA":"Camboya","LAOS":"Laos",
    "AUSTRALIA":"Australia","NEW ZEALAND":"Nueva Zelanda","NZ":"Nueva Zelanda",
    "SOUTH AFRICA":"Sudáfrica","RSA":"Sudáfrica","EGYPT":"Egipto","MOROCCO":"Marruecos","ALGERIA":"Argelia",
    "TUNISIA":"Túnez","NIGERIA":"Nigeria","KENYA":"Kenia","ETHIOPIA":"Etiopía","GHANA":"Ghana",
    "COTE D IVOIRE":"Costa de Marfil","COTE DIVOIRE":"Costa de Marfil","COTE D’IVOIRE":"Costa de Marfil",
    "IVORY COAST":"Costa de Marfil","SAUDI ARABIA":"Arabia Saudita","KSA":"Arabia Saudita",
    "UNITED ARAB EMIRATES":"Emiratos Árabes Unidos","UAE":"Emiratos Árabes Unidos","QATAR":"Catar",
    "KUWAIT":"Kuwait","OMAN":"Omán","BAHRAIN":"Baréin","ISRAEL":"Israel","JORDAN":"Jordania","LEBANON":"Líbano",
}

def normalizar_pais(pais_raw: str) -> str:
    key = _norm_country_key(pais_raw)
    if key in PAIS_MAP:
        return PAIS_MAP[key]
    es = {
        "Argentina":"Argentina","Bolivia":"Bolivia","Brasil":"Brasil","Canadá":"Canadá","Chile":"Chile",
        "Colombia":"Colombia","Costa Rica":"Costa Rica","Cuba":"Cuba","Ecuador":"Ecuador","El Salvador":"El Salvador",
        "España":"España","Estados Unidos":"Estados Unidos","Francia":"Francia","Guatemala":"Guatemala","Honduras":"Honduras",
        "Italia":"Italia","México":"México","Nicaragua":"Nicaragua","Panamá":"Panamá","Paraguay":"Paraguay",
        "Perú":"Perú","Portugal":"Portugal","Puerto Rico":"Puerto Rico","Reino Unido":"Reino Unido","República Dominicana":"República Dominicana",
        "Uruguay":"Uruguay","Venezuela":"Venezuela"
    }
    norm_es = { _norm_country_key(k): v for k,v in es.items() }
    if key in norm_es:
        return norm_es[key]
    return _pretty_cap(pais_raw or "Chile")

# ====== Catálogo ======
def cargar_catalogo_modelos(path_xlsx):
    try:
        df = pd.read_excel(path_xlsx, sheet_name=0)
    except Exception as e:
        print(f"⚠️ No se pudo leer el catálogo: {e}. Se usarán reglas por defecto.")
        return {}, {}, {}

    cols = {c: _norm_key(c) for c in df.columns}
    inv = {v: k for k, v in cols.items()}

    def pick(*candidatas):
        for c in candidatas:
            if c in inv: return inv[c]
        return None

    col_marca_raw   = pick("MARCA", "BRAND")
    col_modelo_raw  = pick("MODELO", "MODEL", "MODELO COMERCIAL", "COMERCIAL")
    col_marca_norm  = pick("MARCA NORMALIZADA", "MARCA NORMAL", "BRAND NORMALIZED", "BRAND NORM", "MARCA STD")
    col_modelo_norm = pick("MODELO NORMALIZADO", "MODELO NORMAL", "MODEL NORMALIZED", "MODEL NORM", "MODELO STD")

    if not col_marca_raw or not col_modelo_raw:
        print("⚠️ El catálogo no tiene columnas de marca/modelo reconocibles. Se usarán reglas por defecto.")
        return {}, {}, {}

    if not col_marca_norm:  col_marca_norm  = col_marca_raw
    if not col_modelo_norm: col_modelo_norm = col_modelo_raw

    exact_map, brand_map, modelos_por_marca = {}, {}, {}

    for _, r in df.iterrows():
        marca_raw  = _norm_key(str(r.get(col_marca_raw, "")))
        modelo_raw = _norm_key(str(r.get(col_modelo_raw, "")))

        marca_n    = _pretty_cap(str(r.get(col_marca_norm, "")) or str(r.get(col_marca_raw, "")))
        modelo_n   = _pretty_cap(str(r.get(col_modelo_norm, "")) or str(r.get(col_modelo_raw, "")))

        if marca_raw:
            brand_map[marca_raw] = marca_n
        if marca_raw and modelo_raw:
            exact_map[(marca_raw, modelo_raw)] = (marca_n, modelo_n)
            modelos_por_marca.setdefault(_norm_key(marca_n), set()).add(modelo_n)

    return exact_map, brand_map, modelos_por_marca

EXACT_MAP, BRAND_MAP, MODELOS_POR_MARCA = cargar_catalogo_modelos(CATALOGO_MODELOS_XLSX)

def _elegir_modelo_catalogo(marca_norm: str, preferencia: str = "") -> str:
    marca_u = _norm_key(marca_norm)
    modelos = sorted(list(MODELOS_POR_MARCA.get(marca_u, set())), key=lambda x: (_norm_key(x)))
    if not modelos:
        return ""
    if preferencia:
        close = get_close_matches(_pretty_cap(preferencia), modelos, n=1, cutoff=0.80)
        if close:
            return close[0]
    modelos_orden = sorted(modelos, key=lambda x: (len(_norm_key(x)), _norm_key(x)))
    return modelos_orden[0] if modelos_orden else ""

def _pareja_en_catalogo(marca_norm: str, modelo_norm: str) -> bool:
    m = _norm_key(marca_norm)
    mo = _norm_key(modelo_norm)
    if not m or not mo: return False
    return mo in { _norm_key(x) for x in MODELOS_POR_MARCA.get(m, set()) }

def normalizar_marca_modelo(marca_raw: str, modelo_raw: str):
    if _norm_key(marca_raw) == "IPHONE":
        return "Apple", "iPhone"

    m_key = _norm_key(marca_raw or "")
    mo_key = _norm_key(modelo_raw or "")

    if (m_key, mo_key) in EXACT_MAP:
        m_norm, mo_norm = EXACT_MAP[(m_key, mo_key)]
        return _pretty_cap(m_norm), _finalize_model_case(_fix_model_spacing_specific(mo_norm))

    if m_key in BRAND_MAP:
        m_norm = BRAND_MAP[m_key]
        marca_u = _norm_key(m_norm)
        base_modelo = _strip_brand_prefix(mo_key, marca_u)
        modelos_posibles = MODELOS_POR_MARCA.get(marca_u, set())

        if marca_u in {"TECNO", "VIVO"}:
            if not base_modelo:
                if modelos_posibles:
                    elegido = _elegir_modelo_catalogo(m_norm)
                    print(f"ℹ️ Marca {m_norm} sin modelo → usando catálogo: {elegido}")
                    return _pretty_cap(m_norm), _finalize_model_case(_fix_model_spacing_specific(elegido))
                else:
                    print(f"⚠️ Marca {m_norm} sin modelos en catálogo → usando placeholder")
                    return _pretty_cap(m_norm), "Modelo"
            if not _pareja_en_catalogo(m_norm, base_modelo):
                elegido = _elegir_modelo_catalogo(m_norm, preferencia=base_modelo) or _elegir_modelo_catalogo(m_norm)
                print(f"ℹ️ {m_norm} modelo '{base_modelo}' no encontrado → usando '{elegido}' del catálogo")
                return _pretty_cap(m_norm), _finalize_model_case(_fix_model_spacing_specific(elegido))
            for mm in modelos_posibles:
                if _norm_key(mm) == _norm_key(base_modelo):
                    return _pretty_cap(m_norm), _finalize_model_case(_fix_model_spacing_specific(mm))

        if not base_modelo:
            if modelos_posibles:
                elegido = _elegir_modelo_catalogo(m_norm)
                print(f"ℹ️ {m_norm} sin modelo → usando '{elegido}' del catálogo")
                return _pretty_cap(m_norm), _finalize_model_case(_fix_model_spacing_specific(elegido))
            else:
                return ("Apple", "iPhone") if marca_u == "APPLE" else (_pretty_cap(m_norm), "Modelo")

        if modelos_posibles:
            for mm in modelos_posibles:
                if _norm_key(mm) == _norm_key(base_modelo):
                    return _pretty_cap(m_norm), _finalize_model_case(_fix_model_spacing_specific(mm))
            candidato = _elegir_modelo_catalogo(m_norm, preferencia=base_modelo)
            if candidato:
                return _pretty_cap(m_norm), _finalize_model_case(_fix_model_spacing_specific(candidato))

        base = _strip_brand_prefix(mo_key, marca_u)
        mo_norm = _finalize_model_case(_fix_model_spacing_specific(_pretty_cap(base))) if base else ("iPhone" if marca_u == "APPLE" else "Modelo")
        return _pretty_cap(m_norm), mo_norm

    inferred = infer_brand_from_model(modelo_raw)
    if inferred:
        return normalizar_marca_modelo(inferred, modelo_raw)

    return "Apple", "iPhone"

# ========================= PLAYWRIGHT HELPERS =========================
def wait_invisible_loading(page):
    try:
        page.wait_for_selector("#mb-loading", state="hidden", timeout=6000)
    except Exception:
        pass
    try:
        page.wait_for_selector(".modal-backdrop", state="hidden", timeout=4000)
    except Exception:
        pass

def robust_click(page, selector, timeout=12000):
    try:
        page.wait_for_selector(selector, timeout=timeout)
        page.locator(selector).scroll_into_view_if_needed()
        page.click(selector, timeout=timeout)
        return True
    except Exception:
        return False

def get_input_value(page, selectors):
    for sel in selectors:
        try:
            el = page.locator(sel)
            if el.count() > 0:
                v = el.input_value(timeout=2000)
                if v is not None:
                    v = v.strip()
                    if v:
                        return v
        except Exception:
            continue
    return ""

def _inner_text(page, sel):
    try:
        return page.locator(sel).inner_text(timeout=2000).strip()
    except Exception:
        return ""

def leer_tipo_documento(page):
    # select#form_document_type si existe
    try:
        if page.locator("#form_document_type").count() > 0:
            txt = page.locator("#form_document_type option:checked").inner_text(timeout=2000).strip()
            n = _norm_key(txt)
            if "PASAP" in n: return "Pasaporte"
            if "RUT" in n or "DNI" in n: return "RUT (DNI)"
    except Exception:
        pass
    # fallback por valor/texto
    try:
        val = page.locator("#form_document_type").input_value(timeout=1500).strip()
        if val:
            n = _norm_key(val)
            if "PASAP" in n: return "Pasaporte"
            if "RUT" in n or "DNI" in n: return "RUT (DNI)"
    except Exception:
        pass
    txt = _inner_text(page, "#form_document_type")
    n = _norm_key(txt)
    if "PASAP" in n: return "Pasaporte"
    if "RUT" in n or "DNI" in n: return "RUT (DNI)"
    return "Pasaporte"

def leer_pais(page):
    for sel in [
        '//*[@id="formulario"]/div[1]/div/div/div[3]/div[10]/div/div/p/b',
        "//form[@id='formulario']//p//b"
    ]:
        try:
            el = page.locator(sel)
            if el.count() > 0:
                t = el.first.inner_text(timeout=1500).strip()
                if t:
                    return t
        except Exception:
            continue
    return "Chile"

def model_error_present(page):
    try:
        el = page.locator('//*[@id="cert_new_model_dropdown-error"]')
        if el.count() > 0:
            txt = el.inner_text(timeout=1500).strip().lower()
            return ("obligatorio" in txt) or ("requerido" in txt) or ("required" in txt)
    except Exception:
        pass
    return False

def forzar_marca_modelo_generico(page):
    # Simula TAB + escribir Apple + ENTER + TAB + iPhone + ENTER + TAB
    page.keyboard.press("Shift+Tab")
    sleep(0.2)
    page.keyboard.type("Apple")
    page.keyboard.press("Enter")
    page.keyboard.press("Tab")
    sleep(0.2)
    page.keyboard.type("iPhone")
    page.keyboard.press("Enter")
    page.keyboard.press("Tab")
    sleep(0.2)

# ========================= FLUJO =========================
def login_multibanda(page):
    page.goto(MB_BASE_URL, timeout=NAV_TIMEOUT)
    page.fill("#floatingInput", MB_USER, timeout=DEF_TIMEOUT)
    page.fill("#floatingPassword", MB_PASS, timeout=DEF_TIMEOUT)
    page.keyboard.press("Enter")
    page.wait_for_url(re.compile(r"index\.php"), timeout=NAV_TIMEOUT)
    page.goto(urljoin(MB_BASE_URL, "index.php?do=submission/pending_adm_submissions_landing_page&rType=page&navbarp=1"), timeout=NAV_TIMEOUT)

def obtener_ids_validos(page):
    page.wait_for_selector("#tabla-ordenable", timeout=NAV_TIMEOUT)
    sleep(1.5)
    filas = page.locator("//table[@id='tabla-ordenable']/tbody/tr")
    total = filas.count()
    print(f"🔍 Revisando {total} filas...")
    ids = []
    for i in range(total):
        fila = filas.nth(i)
        try:
            boton = fila.locator("./td[9]/div/a")
            if boton.count() == 0:
                continue
            if "Confirmar en OABI" in boton.inner_text(timeout=1500):
                id_texto = fila.locator("./th").inner_text(timeout=1500).strip()
                if id_texto.isdigit():
                    ids.append(id_texto)
                    print(f"✅ ID válido: {id_texto}")
        except Exception:
            continue
    print(f"🔎 Total IDs con botón Confirmar en OABI: {len(ids)}")
    return ids

def extraer_y_normalizar_datos(page, ids_validos):
    datos = []
    for id_multibanda in ids_validos:
        try:
            confirm_url = urljoin(MB_BASE_URL, f"index.php?do=submission/confirm_automatic_process&id={id_multibanda}&rType=page")
            page.goto(confirm_url, timeout=NAV_TIMEOUT)
            sleep(0.6)

            imei_1 = get_input_value(page, ['//input[@type="text" and @value][contains(@id,"imei")][1]',
                                            '/html/body/div[1]/section/form/div[1]/div/div/div[3]/div[2]/div/div/input'])
            imei_2 = get_input_value(page, ['/html/body/div[1]/section/form/div[1]/div/div/div[3]/div[3]/div/div/input'])
            numero_serie = get_input_value(page, ['/html/body/div[1]/section/form/div[1]/div/div/div[3]/div[4]/div/div/input'])
            numero_documento = get_input_value(page, ['/html/body/div[1]/section/form/div[1]/div/div/div[3]/div[6]/div/div/input'])

            nombre = get_input_value(page, [
                '/html/body/div[1]/section/form/div[1]/div/div/div[3]/div[7]/div/div/input',
                "//input[contains(@id,'name') or contains(@name,'name')]",
                "input[name*='name'], input[id*='name']",
            ])

            tipo_documento = leer_tipo_documento(page)

            try:
                marca_raw = _inner_text(page, '//*[@id="formulario"]/div[1]/div/div/div[3]/div[8]/div/div/p/b') or "Apple"
            except Exception:
                marca_raw = "Apple"
            try:
                modelo_raw = _inner_text(page, '//*[@id="formulario"]/div[1]/div/div/div[3]/div[9]/div/div/p/b')
            except Exception:
                modelo_raw = ""

            marca_norm, modelo_norm = normalizar_marca_modelo(marca_raw, modelo_raw)

            pais_raw = leer_pais(page)
            pais = normalizar_pais(pais_raw)

            cantidad = "2" if (imei_2 or "").strip() else "1"

            if _norm_key(marca_norm) in MODELOS_POR_MARCA and not _pareja_en_catalogo(marca_norm, modelo_norm):
                elegido = _elegir_modelo_catalogo(marca_norm, preferencia=modelo_norm) or _elegir_modelo_catalogo(marca_norm)
                if elegido:
                    print(f"ℹ️ Ajuste final catálogo: {marca_norm} '{modelo_norm}' → '{elegido}'")
                    modelo_norm = _finalize_model_case(_fix_model_spacing_specific(elegido))

            datos.append({
                "id": id_multibanda,
                "cantidad_imei": cantidad,
                "imei_1": imei_1,
                "imei_2": (imei_2 or "").strip(),
                "numero_serie": numero_serie,
                "tipo_documento": tipo_documento,
                "numero_documento": numero_documento,
                "marca": marca_norm,
                "modelo_comercial": modelo_norm,
                "detalles_tecnicos": "Compra Internacional",
                "nombre": nombre,
                "pais_origen": pais,
                "descripcion": "Uso personal"
            })

        except Exception as e:
            print(f"❌ Error extrayendo ID {id_multibanda}: {e}")
            continue

    df = pd.DataFrame(datos)
    df.to_excel(ARCHIVO_TEMP, index=False)
    print(f"💾 Guardado {ARCHIVO_TEMP} con {len(df)} filas")
    return datos

def login_oabi(page, token_2fa: str):
    page.goto(OABI_LOGIN_URL, timeout=NAV_TIMEOUT)
    page.fill("#username", os.getenv("OABI_USER",""), timeout=DEF_TIMEOUT)
    page.fill("#password", os.getenv("OABI_PASS",""), timeout=DEF_TIMEOUT)
    page.keyboard.press("Enter")
    page.wait_for_selector('xpath=//*[@id="token"]', timeout=NAV_TIMEOUT)
    if not token_2fa:
        token_2fa = input("🔐 Ingresa el token 2FA de OABI: ").strip()
    page.fill('xpath=//*[@id="token"]', token_2fa)
    page.keyboard.press("Enter")
    sleep(7)
    wait_invisible_loading(page)

def abrir_inscripcion_administrativa(page):
    robust_click(page, 'xpath=/html/body/div[1]/div[1]/ul/li[5]/a')
    wait_invisible_loading(page)
    robust_click(page, 'xpath=/html/body/div[1]/div[1]/ul/li[5]/ul/li[1]/a')
    wait_invisible_loading(page)

def validar_imei_en_oabi(page, imei_1, numero_documento=""):
    abrir_inscripcion_administrativa(page)
    page.wait_for_selector('xpath=//*[@id="in_imei"]', timeout=NAV_TIMEOUT)
    campo_imei = page.locator('xpath=//*[@id="in_imei"]')
    try: campo_imei.fill("")
    except Exception: pass
    campo_imei.type(str(imei_1))
    page.keyboard.press("Enter")
    sleep(1.5); wait_invisible_loading(page)
    try:
        page.wait_for_selector("//table/tbody/tr", timeout=10_000)
        filas = page.locator("//table/tbody/tr")
        n = filas.count()
        for i in range(n):
            t = filas.nth(i).inner_text(timeout=1500).strip()
            if str(imei_1) in t or (numero_documento and str(numero_documento) in t):
                return True
    except Exception:
        pass
    return False

def select_document_type(page, tipo_documento_text: str) -> bool:
    want = re.sub(r'[^a-z0-9]', '', (tipo_documento_text or '').lower())
    target_pasaporte = ('pasap' in want) or ('pasaporte' in want)
    target_rutdni = ('rut' in want) or ('dni' in want) or ('rutdni' in want)

    # Dropdown bootstrap
    if robust_click(page, 'xpath=/html/body/div[1]/div[2]/div/div/div/div/div/div[2]/div[5]/div/div/form/div[2]/div[4]/div/div/button/span[1]') \
       or robust_click(page, 'xpath=//button[contains(@data-toggle,"dropdown")]'):
        try:
            page.wait_for_selector("//div[contains(@class,'dropdown-menu')]", timeout=3000)
        except Exception:
            pass
        opciones = page.locator("//div[contains(@class,'dropdown-menu')]//a[normalize-space()]")
        if opciones.count() == 0:
            opciones = page.locator("//div[contains(@class,'dropdown-menu')]//*[self::a or self::button or self::li][normalize-space()]")
        choice_idx = -1
        for i in range(opciones.count()):
            txt = opciones.nth(i).inner_text(timeout=1500).strip()
            n = re.sub(r'[^a-z0-9]', '', txt.lower())
            if target_pasaporte and ('pasap' in n or 'pasaporte' in n):
                choice_idx = i; break
            if target_rutdni and (('rut' in n) or ('dni' in n) or ('rutdni' in n)):
                choice_idx = i; break
        if choice_idx < 0 and opciones.count() > 0:
            choice_idx = 0
        if choice_idx >= 0:
            opciones.nth(choice_idx).click()
            return True

    # select nativo
    if page.locator("//select[contains(@id,'document') or contains(@name,'document')]").count() > 0:
        sel = page.locator("//select[contains(@id,'document') or contains(@name,'document')]").first
        opts = sel.locator("option")
        chosen = False
        for i in range(opts.count()):
            t = opts.nth(i).inner_text(timeout=1500).strip()
            n = re.sub(r'[^a-z0-9]', '', t.lower())
            if target_pasaporte and ('pasap' in n or 'pasaporte' in n):
                sel.select_option(index=i); chosen=True; break
            if target_rutdni and (('rut' in n) or ('dni' in n) or ('rutdni' in n)):
                sel.select_option(index=i); chosen=True; break
        if not chosen and opts.count() > 0:
            sel.select_option(index=0)
        return True

    return False

def procesar_oabi_y_confirmar(page_oabi, page_mb, fila):
    id_multibanda = fila["id"]
    print(f"🟢 Procesando ID {id_multibanda}...")

    wait_invisible_loading(page_oabi)
    robust_click(page_oabi, 'xpath=/html/body/div[1]/div[1]/ul/li[5]/a')
    wait_invisible_loading(page_oabi)
    robust_click(page_oabi, 'xpath=/html/body/div[1]/div[1]/ul/li[5]/ul/li[1]/a/span[2]')
    sleep(1.2)

    wait_invisible_loading(page_oabi)
    robust_click(page_oabi, 'xpath=/html/body/div[1]/div[2]/div/div/div/div/div/div[2]/div[2]/button')

    page_oabi.wait_for_selector("#cant_imeis", timeout=NAV_TIMEOUT)
    page_oabi.fill("#cant_imeis", str(fila["cantidad_imei"]))
    page_oabi.keyboard.press("Tab")
    sleep(0.8)

    # IMEIs
    try:
        page_oabi.fill("#cert_new_imei_1", str(fila["imei_1"]))
    except Exception:
        page_oabi.fill("#cert_new_imei_01", str(fila["imei_1"]))
    if fila["cantidad_imei"] == "2":
        try:
            page_oabi.fill("#cert_new_imei_2", str(fila["imei_2"]))
        except Exception:
            page_oabi.fill("#cert_new_imei_02", str(fila["imei_2"]))

    page_oabi.fill("#num_serie", str(fila["numero_serie"]))

    # Tipo documento
    if not select_document_type(page_oabi, fila["tipo_documento"]):
        robust_click(page_oabi, 'xpath=/html/body/div[1]/div[2]/div/div/div/div/div/div[2]/div[5]/div/div/form/div[2]/div[4]/div/div/button')
        page_oabi.locator("//div[@class='dropdown-menu']//a[contains(.,'Pasaporte')]").first.click(timeout=3000)

    # Número documento
    ok_doc = False
    for sel in ["#cert_new_number_pasaporte", "#cert_new_number_dni", "#cert_new_number_rut",
                "//input[contains(@id,'pasaporte') or contains(@id,'dni') or contains(@id,'rut')]"]:
        try:
            page_oabi.fill(sel, str(fila["numero_documento"]))
            ok_doc = True
            break
        except Exception:
            continue
    if not ok_doc:
        print("⚠️ No se pudo rellenar número de documento.")

    # Marca/Modelo normalizados (via teclado y TAB)
    page_oabi.keyboard.press("Tab")  # foco marca
    page_oabi.keyboard.type(str(fila["marca"]))
    page_oabi.keyboard.press("Enter")
    page_oabi.keyboard.press("Tab")  # foco modelo
    sleep(0.6)
    page_oabi.keyboard.type(str(fila["modelo_comercial"]))
    page_oabi.keyboard.type(" ")
    page_oabi.keyboard.press("Backspace")
    page_oabi.keyboard.press("Enter")
    page_oabi.keyboard.press("Tab")
    sleep(0.6)

    # Si exige modelo, forzar Apple/iPhone
    if model_error_present(page_oabi):
        print(f"⚠️ Modelo inválido. Forzando Apple/iPhone (ID {id_multibanda})...")
        forzar_marca_modelo_generico(page_oabi)
        if model_error_present(page_oabi):
            print("⚠️ Reintentando forzar Apple/iPhone...")
            forzar_marca_modelo_generico(page_oabi)

    # Detalles / Nombre / País / Descripción
    page_oabi.fill("#cert_new_detalles_tec", str(fila["detalles_tecnicos"]))
    page_oabi.fill("#cert_new_name", str(fila["nombre"]))
    page_oabi.keyboard.press("Tab")  # foco país
    page_oabi.keyboard.type(str(fila["pais_origen"]))
    page_oabi.keyboard.press("Enter")
    page_oabi.keyboard.press("Tab")

    page_oabi.fill("#cert_new_description", str(fila["descripcion"]))
    page_oabi.keyboard.press("Enter")
    sleep(0.5)
    wait_invisible_loading(page_oabi)
    print(f"✅ Enviado a OABI: ID {id_multibanda}")

    # Validación por IMEI
    ok_oabi = validar_imei_en_oabi(page_oabi, fila["imei_1"], fila["numero_documento"])
    if not ok_oabi and fila.get("imei_2"):
        ok_oabi = validar_imei_en_oabi(page_oabi, fila["imei_2"], fila["numero_documento"])
    if not ok_oabi:
        print(f"❌ No se visualiza IMEI en Inscripción Administrativa (ID {id_multibanda}). No se confirma en MB.")
        return

    # Confirmación en Multibanda por ID
    page_mb.goto(urljoin(MB_BASE_URL, "index.php?do=submission/pending_adm_submissions_landing_page&rType=page&navbarp=1"), timeout=NAV_TIMEOUT)
    page_mb.wait_for_selector("#tabla-ordenable", timeout=NAV_TIMEOUT)
    sleep(0.8)

    if page_mb.locator("#buscador").count() > 0:
        page_mb.fill("#buscador", str(id_multibanda))
        page_mb.keyboard.press("Enter")
        sleep(0.8)
    robust_click(page_mb, 'xpath=/html/body/div[1]/section[2]/div/div/div[2]/table/tbody/tr/td[9]/div/a')
    robust_click(page_mb, 'xpath=/html/body/div[3]/div/div/div[3]/button[2]')

    print(f"🔐 ✅ Confirmado en Multibanda por ID {id_multibanda}")

def main():
    ap = argparse.ArgumentParser(description="MB↔OABI on-device (Playwright/Termux)")
    ap.add_argument("--token", help="Token 2FA de OABI", default="")
    args = ap.parse_args()

    token_2fa = (args.token or "").strip()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)  # Cambia a False si quieres ver el navegador (más pesado)
        # Un contexto para MB y otro para OABI evita cruces de sesión/cookies
        ctx_mb = browser.new_context()
        ctx_oabi = browser.new_context()

        page_mb = ctx_mb.new_page()
        page_oabi = ctx_oabi.new_page()

        # ----- Multibanda: login + IDs
        print("🌐 Login a Multibanda…")
        login_multibanda(page_mb)
        ids = obtener_ids_validos(page_mb)
        if not ids:
            print("⚠️ No se encontraron IDs con 'Confirmar en OABI'. Fin.")
            browser.close()
            return

        # ----- Extraer/normalizar + Excel
        registros = extraer_y_normalizar_datos(page_mb, ids)

        # ----- OABI: login + 2FA
        print("🌐 Login a OABI…")
        login_oabi(page_oabi, token_2fa)

        # ----- Proceso OABI + confirmación MB por cada fila
        for fila in registros:
            try:
                procesar_oabi_y_confirmar(page_oabi, page_mb, fila)
            except Exception as e:
                print(f"❌ Error en ID {fila.get('id')}: {e}")
                continue

        browser.close()
        print("🏁 Proceso completo finalizado.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n⛔ Cancelado por usuario.")
        sys.exit(130)
