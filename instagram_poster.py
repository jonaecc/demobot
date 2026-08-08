"""
instagram_poster.py — Publica en Instagram cuando el bot abre/cierra una operación
====================================================================================
Diseñado para NUNCA interferir con el trading real:
  - Pensado para ejecutarse en un hilo aparte (no bloquea el loop del bot)
  - Cualquier error se loguea y se descarta — nunca propaga una excepción
  - Se activa/desactiva por completo con la variable INSTAGRAM_ENABLED

FLUJO (Instagram Graph API — Content Publishing):
  1. Genera una imagen (card) con Pillow con los datos de la operación
  2. Sube esa imagen a imgbb (hosting gratuito) para obtener una URL pública
     → Instagram exige que la imagen esté en una URL pública, no acepta bytes directos
  3. Crea un "container" en Instagram con esa URL + caption
  4. Publica el container

CONFIGURACIÓN (variables de entorno — ver guía al final del archivo):
  INSTAGRAM_ENABLED     → "true" / "false"
  IG_ACCESS_TOKEN        → token de acceso de la cuenta profesional de Instagram
  IG_USER_ID              → ID numérico de la cuenta profesional de Instagram
  IMGBB_API_KEY           → API key gratuita de https://api.imgbb.com/
  IG_BACKGROUND_PATH (opcional) → ruta a una imagen propia para usar de fondo
  IG_HASHTAGS         (opcional) → hashtags propios, separados por coma/espacio

Dependencias nuevas (agregar a requirements.txt):
  pillow
  requests
  python-dotenv
"""

import os
import io
import base64
import threading
import requests
from datetime import datetime, timezone
from PIL import Image, ImageDraw, ImageFont, ImageOps

# Carga variables desde un archivo ".env" en la raíz del proyecto (mismo
# directorio que este archivo) — pensado para probar todo en local antes de
# subir el bot a Railway. Si el .env no existe, no hace nada (no rompe nada).
# Si una variable YA está seteada en el entorno real (ej. Railway), el .env
# NUNCA la pisa — override=False es el comportamiento por defecto de dotenv.
try:
    from dotenv import load_dotenv
    _dir = os.path.dirname(os.path.abspath(__file__))
    _dot_env = os.path.join(_dir, ".env")
    _env     = os.path.join(_dir, "env")
    if os.path.exists(_dot_env):
        load_dotenv(_dot_env)
    elif os.path.exists(_env):
        load_dotenv(_env)
except ImportError:
    pass  # python-dotenv no instalado - seguimos solo con variables de entorno reales

GRAPH_API_VERSION = "v23.0"  # revisar versión vigente en developers.facebook.com


def _elegir_host(access_token: str) -> str:
    """Meta tiene DOS productos distintos, con distinto host de API y distinto
    formato de token:
      • "Facebook Login for Business" (cuenta vinculada a una Página de FB)
        → token con prefijo 'EAA...'  → host graph.facebook.com
      • "Instagram API con Instagram Login" (sin Página de FB, login directo)
        → token con otro prefijo (no EAA) → host graph.instagram.com
    En vez de hacer que el usuario recuerde cuál usar, lo detectamos solos
    por el prefijo del token — así no importa de cuál de los dos flujos
    vengan tus credenciales."""
    return "https://graph.facebook.com" if access_token.startswith("EAA") \
        else "https://graph.instagram.com"

# Tamaño del post: 1080x1350 (4:5 vertical) — es el formato que Instagram prioriza
# en el feed desde 2025/2026 (ocupa más pantalla al hacer scroll que el clásico
# cuadrado 1:1, y calza mejor con el recorte de la grilla de perfil).
POST_W = 1080
POST_H = 1350

# Carpeta fonts/ que se distribuye junto a este archivo (ver fonts/Poppins-*.ttf)
FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
_FONT_FILES = {
    "regular":   "Poppins-Regular.ttf",
    "medium":    "Poppins-Medium.ttf",
    "semibold":  "Poppins-SemiBold.ttf",
    "bold":      "Poppins-Bold.ttf",
    "extrabold": "Poppins-ExtraBold.ttf",
}
_font_cache = {}


# ══════════════════════════════════════════════════════════════════
# UTILIDADES DE DIBUJO
# ══════════════════════════════════════════════════════════════════

def _font(size, weight="regular"):
    """Poppins (moderna, la misma familia que usan muchas cuentas de trading en IG).
    Si por algún motivo el .ttf no está disponible, cae a una fuente del sistema
    y, como último recurso, a la fuente por defecto de Pillow — nunca rompe."""
    key = (size, weight)
    if key in _font_cache:
        return _font_cache[key]
    path = os.path.join(FONTS_DIR, _FONT_FILES.get(weight, _FONT_FILES["regular"]))
    f = None
    try:
        f = ImageFont.truetype(path, size)
    except Exception:
        for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
                   if weight != "regular" else
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",):
            try:
                f = ImageFont.truetype(p, size)
                break
            except Exception:
                continue
    if f is None:
        f = ImageFont.load_default()
    _font_cache[key] = f
    return f


def _cargar_fondo(path, W, H):
    """Carga una imagen de fondo propia (logo, foto de marca, textura, etc.) y la
    recorta/ajusta tipo 'cover' a WxH. Devuelve None si no hay path o si falla —
    en ese caso se usa el degradado por defecto, nunca rompe la generación."""
    if not path:
        return None
    try:
        bg = Image.open(path).convert("RGB")
        return ImageOps.fit(bg, (W, H), method=Image.LANCZOS)
    except Exception:
        return None


def _fondo_degradado(W, H, accent):
    """Fondo por defecto cuando no se configuró una imagen propia: degradado
    oscuro con un tinte sutil del color de la operación (verde/rojo)."""
    top    = (9, 11, 16)
    tint   = tuple(int(c * 0.16) for c in accent)
    bottom = tuple(min(255, top[i] + tint[i]) for i in range(3))
    img  = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)
    for y in range(H):
        t = y / (H - 1)
        color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        draw.line([(0, y), (W, y)], fill=color)
    return img


def _con_overlay_legible(img, panel_top, panel_bottom, W, H):
    """Oscurece el fondo (cualquiera sea) para que el texto siempre sea legible,
    y agrega un panel translúcido tipo 'glass' detrás del bloque de datos."""
    img = img.convert("RGBA")
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rectangle([0, 0, W, H], fill=(6, 8, 12, 115))                     # oscurecido general
    od.rounded_rectangle([50, panel_top, W - 50, panel_bottom],
                          radius=28, fill=(6, 8, 12, 150))                # panel de datos
    return Image.alpha_composite(img, overlay).convert("RGB")


def _badge(draw, x, y, text, font, fg, bg, icono=None):
    """Chip/pill redondeado (p.ej. 'LONG' + flecha) — look moderno tipo template de IG.
    El ícono se dibuja como vector (no como caracter de fuente) para evitar
    glifos faltantes ('tofu boxes') en símbolos que Poppins no incluye."""
    pad_x, pad_y = 30, 16
    icon_w, icon_gap = (26, 16) if icono else (0, 0)
    w = draw.textlength(text, font=font)
    x0, y0 = x, y
    x1 = x0 + w + icon_gap + icon_w + pad_x * 2
    y1 = y0 + font.size + pad_y * 2
    draw.rounded_rectangle([x0, y0, x1, y1], radius=(y1 - y0) // 2, fill=bg)
    draw.text((x0 + pad_x, y0 + pad_y - 4), text, font=font, fill=fg)
    if icono:
        tx = x0 + pad_x + w + icon_gap
        cy = (y0 + y1) // 2
        h = 16
        if icono == "up":
            pts = [(tx, cy + h), (tx + icon_w, cy + h), (tx + icon_w / 2, cy - h)]
        else:
            pts = [(tx, cy - h), (tx + icon_w, cy - h), (tx + icon_w / 2, cy + h)]
        draw.polygon(pts, fill=fg)
    return y1


def _fila(draw, y, label, val, W, f_label, f_val, muted, text_color):
    draw.text((90, y), label, font=f_label, fill=muted)
    w = draw.textlength(val, font=f_val)
    draw.text((W - 90 - w, y - 8), val, font=f_val, fill=text_color)


# ══════════════════════════════════════════════════════════════════
# 1a. IMAGEN — APERTURA DE OPERACIÓN
# ══════════════════════════════════════════════════════════════════

def generar_imagen_apertura(symbol, side, entry_price, tp_pct, sl_pct,
                             leverage, scenario=None, fondo_path=None,
                             timeout_velas=None) -> bytes:
    W, H = POST_W, POST_H
    text_color = (245, 247, 250)
    muted      = (168, 176, 186)
    accent     = (34, 197, 94) if side == "buy" else (239, 68, 68)

    base = _cargar_fondo(fondo_path, W, H) or _fondo_degradado(W, H, accent)

    panel_top, panel_h = 530, 750
    img = _con_overlay_legible(base, panel_top, panel_top + panel_h, W, H)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, W, 10], fill=accent)   # barra superior de color

    f_tag    = _font(30, "medium")
    f_symbol = _font(66, "extrabold")
    f_badge  = _font(44, "bold")
    f_label  = _font(32, "regular")
    f_val    = _font(50, "semibold")
    f_cta    = _font(28, "medium")

    y = 120
    draw.ellipse([90, y + 8, 106, y + 24], fill=accent)
    draw.text((122, y), "BEHAVIOR BOT / OPEN POSITION", font=f_tag, fill=muted)
    y += 66
    draw.text((90, y), symbol, font=f_symbol, fill=text_color)

    y += 108
    direccion = "LONG" if side == "buy" else "SHORT"
    _badge(draw, 90, y, direccion, f_badge, fg=(10, 12, 16), bg=accent,
           icono="up" if side == "buy" else "down")

    draw.ellipse([90, panel_top - 66, 100, panel_top - 56], fill=accent)
    draw.text((114, panel_top - 72), "Follow every open & close in real time",
               font=f_cta, fill=muted)

    y = panel_top + 70
    filas = [
        ("Entry / Entrada",          f"${entry_price:,.4f}"),
        ("Leverage / Apalancamiento", f"{leverage}x"),
        ("Take Profit",              f"+{tp_pct:.2f}%" if tp_pct else "--"),
        ("Stop Loss",                f"-{sl_pct:.2f}%" if sl_pct else "--"),
        ("Timeout",                  f"{timeout_velas} candles/velas" if timeout_velas else "--"),
    ]
    for label, val in filas:
        _fila(draw, y, label, val, W, f_label, f_val, muted, text_color)
        y += 110

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    draw.text((90, H - 100), ts, font=f_tag, fill=muted)
    if scenario:
        w = draw.textlength(scenario, font=f_tag)
        draw.text((W - 90 - w, H - 100), scenario, font=f_tag, fill=muted)

    buf = io.BytesIO()
    img = img.convert("RGB")
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════════
# 1b. IMAGEN — CIERRE DE OPERACIÓN (resultado)
# ══════════════════════════════════════════════════════════════════

def generar_imagen_cierre(symbol, side, entry_price, exit_price,
                           pnl_usd, scenario=None, fondo_path=None) -> bytes:
    W, H = POST_W, POST_H
    text_color = (245, 247, 250)
    muted      = (168, 176, 186)
    gano       = pnl_usd > 0
    accent     = (34, 197, 94) if gano else (239, 68, 68)

    base = _cargar_fondo(fondo_path, W, H) or _fondo_degradado(W, H, accent)

    panel_top, panel_h = 530, 600
    img = _con_overlay_legible(base, panel_top, panel_top + panel_h, W, H)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, W, 10], fill=accent)

    f_tag    = _font(30, "medium")
    f_symbol = _font(66, "extrabold")
    f_badge  = _font(44, "bold")
    f_label  = _font(32, "regular")
    f_val    = _font(50, "semibold")
    f_cta    = _font(28, "medium")

    move_pct = ((exit_price - entry_price) / entry_price * 100) if side == "buy" \
        else ((entry_price - exit_price) / entry_price * 100)

    y = 120
    draw.ellipse([90, y + 8, 106, y + 24], fill=accent)
    draw.text((122, y), "BEHAVIOR BOT · OPERACIÓN CERRADA", font=f_tag, fill=muted)
    y += 66
    draw.text((90, y), symbol, font=f_symbol, fill=text_color)

    y += 108
    resultado = "GANANCIA" if gano else "PÉRDIDA"
    _badge(draw, 90, y, resultado, f_badge, fg=(10, 12, 16), bg=accent)

    draw.ellipse([90, panel_top - 66, 100, panel_top - 56], fill=accent)
    draw.text((114, panel_top - 72), "Seguí cada apertura y cierre en tiempo real",
               font=f_cta, fill=muted)

    y = panel_top + 70
    filas = [
        ("Entrada",    f"${entry_price:,.4f}"),
        ("Salida",     f"${exit_price:,.4f}"),
        ("Movimiento", f"{move_pct:+.2f}%"),
        ("PnL",        f"${pnl_usd:+,.2f}"),
    ]
    for label, val in filas:
        _fila(draw, y, label, val, W, f_label, f_val, muted, text_color)
        y += 130

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    draw.text((90, H - 100), ts, font=f_tag, fill=muted)
    if scenario:
        w = draw.textlength(scenario, font=f_tag)
        draw.text((W - 90 - w, H - 100), scenario, font=f_tag, fill=muted)

    buf = io.BytesIO()
    img = img.convert("RGB")
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════════
# 2. HOSTING DE LA IMAGEN (imgbb)
# ══════════════════════════════════════════════════════════════════

def _subir_a_imgbb(image_bytes: bytes, api_key: str) -> str:
    """
    Cuarto intento: Imgur.
    Imgur es altamente confiable y sus dominios (i.imgur.com) son permitidos
    sin restricciones por Meta/Facebook/Instagram.
    """
    b64 = base64.b64encode(image_bytes).decode()
    
    resp = requests.post(
        "https://api.imgur.com/3/image",
        headers={"Authorization": "Client-ID 546c25a59c58ad7"},
        data={"image": b64, "type": "base64"},
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"Imgur rechazó la subida ({resp.status_code}): {resp.text}")
    
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Imgur devolvió error: {data}")
        
    return data["data"]["link"]


# ══════════════════════════════════════════════════════════════════
# 3. PUBLICACIÓN EN INSTAGRAM (Graph API — container → publish)
# ══════════════════════════════════════════════════════════════════

def listar_cuentas_disponibles(access_token: str):
    """Dado un token de 'Facebook Login for Business' (prefijo 'EAA'), devuelve
    la lista de Páginas de Facebook a las que ese token tiene acceso, y para
    cada una el ID de la cuenta de Instagram profesional vinculada (si tiene).
    Sirve para encontrar el IG_USER_ID que REALMENTE corresponde a tu token,
    en vez de adivinar — ver: python test_instagram.py diagnosticar"""
    token = access_token.strip()
    resp = requests.get(
        "https://graph.facebook.com/v23.0/me/accounts",
        params={"access_token": token}, timeout=20,
    )
    if not resp.ok:
        raise RuntimeError(f"No se pudo listar las Páginas ({resp.status_code}): {resp.text}")
    paginas = resp.json().get("data", [])

    resultado = []
    for pagina in paginas:
        page_id = pagina.get("id")
        r2 = requests.get(
            f"https://graph.facebook.com/v23.0/{page_id}",
            params={"fields": "instagram_business_account", "access_token": token},
            timeout=20,
        )
        ig_id = None
        if r2.ok:
            ig_id = (r2.json().get("instagram_business_account") or {}).get("id")
        resultado.append({
            "page_id": page_id,
            "nombre": pagina.get("name"),
            "ig_user_id": ig_id,
        })
    return resultado


def _publicar_en_instagram(ig_user_id: str, access_token: str,
                            image_url: str, caption: str) -> str:
    base = f"{_elegir_host(access_token)}/{GRAPH_API_VERSION}/{ig_user_id}"

    import time
    _codigos_transitorios = {2207003, 2207001, 2207032}
    max_intentos_container = 3
    r1 = None
    for intento in range(max_intentos_container):
        r1 = requests.post(f"{base}/media", data={
            "image_url": image_url,
            "caption": caption,
            "access_token": access_token,
        }, timeout=20)
        if r1.ok:
            break
        err1 = r1.json().get("error", {})
        if err1.get("error_subcode") in _codigos_transitorios and intento < max_intentos_container - 1:
            time.sleep(8)
            continue
        raise RuntimeError(f"Meta rechazó la creación del container ({r1.status_code}): {r1.text}")
    creation_id = r1.json()["id"]

    import time
    max_retries = 5
    for attempt in range(max_retries):
        r2 = requests.post(f"{base}/media_publish", data={
            "creation_id": creation_id,
            "access_token": access_token,
        }, timeout=20)
        
        if r2.ok:
            return r2.json()["id"]
            
        err = r2.json().get("error", {})
        # 9007 / 2207027 = Media ID is not available (still processing)
        if err.get("code") == 9007 or err.get("error_subcode") == 2207027:
            if attempt < max_retries - 1:
                time.sleep(10)  # Wait 10s before retrying
                continue
                
        raise RuntimeError(f"Meta rechazó la publicación del container ({r2.status_code}): {r2.text}")
        
    return None  # Should not be reached


# ══════════════════════════════════════════════════════════════════
# 4. PUNTOS DE ENTRADA SEGUROS (llamar desde live_worker.py)
# ══════════════════════════════════════════════════════════════════

def _env_limpio(nombre):
    """Lee una variable de entorno sacando espacios, saltos de línea y
    comillas envolventes accidentales — errores muy comunes al copiar a
    mano un token largo (p.ej. quedó con un espacio o un \\n pegado al final,
    o el editor le agregó comillas que no correspondían)."""
    val = os.environ.get(nombre, "")
    val = val.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        val = val[1:-1].strip()
    return val


def _config_lista():
    return (
        os.environ.get("INSTAGRAM_ENABLED", "false").lower() == "true"
        and _env_limpio("IG_ACCESS_TOKEN")
        and _env_limpio("IG_USER_ID")
        and _env_limpio("IMGBB_API_KEY")
    )


def diagnosticar_configuracion():
    """Devuelve qué variables están seteadas y cuáles faltan. Pensado para
    herramientas de diagnóstico/testing (ver test_instagram.py) — así el
    error no es un silencio raro sino una lista clara de qué falta."""
    return {
        "INSTAGRAM_ENABLED": os.environ.get("INSTAGRAM_ENABLED", "false").lower() == "true",
        "IG_ACCESS_TOKEN":   bool(_env_limpio("IG_ACCESS_TOKEN")),
        "IG_USER_ID":         bool(_env_limpio("IG_USER_ID")),
        "IMGBB_API_KEY":      bool(_env_limpio("IMGBB_API_KEY")),
    }


def diagnosticar_token():
    """Datos NO sensibles del token (largo, si tiene espacios/saltos de línea
    raros, primeros/últimos caracteres) para detectar un copy-paste roto sin
    exponer el token completo. Pensado para test_instagram.py."""
    crudo = os.environ.get("IG_ACCESS_TOKEN", "")
    limpio = _env_limpio("IG_ACCESS_TOKEN")
    return {
        "longitud": len(limpio),
        "tenia_espacios_o_comillas_de_mas": crudo.strip() != limpio or crudo != crudo.strip(),
        "preview": f"{limpio[:6]}...{limpio[-4:]}" if len(limpio) > 12 else "(demasiado corto)",
    }


def _log_fn(log, nombre, msg):
    if log:
        try:
            getattr(log, nombre, getattr(log, "info", print))(msg)
        except (UnicodeEncodeError, UnicodeDecodeError):
            # Fallback: quitar caracteres no-ASCII para consolas cp1252
            safe = msg.encode("ascii", errors="replace").decode("ascii")
            try:
                getattr(log, nombre, getattr(log, "info", print))(safe)
            except Exception:
                print(safe)


# Hashtags por defecto — se pueden reemplazar sin tocar código con la
# variable de entorno IG_HASHTAGS (separados por coma o espacio, con o sin '#').
DEFAULT_HASHTAGS = ["trading", "tradingbot", "crypto", "futures",
                     "criptomonedas", "inversiones"]


def _armar_hashtags(symbol):
    custom = os.environ.get("IG_HASHTAGS")
    if custom:
        tags = [t.strip().lstrip("#") for t in custom.replace(",", " ").split() if t.strip()]
    else:
        tags = list(DEFAULT_HASHTAGS)
    simbolo = symbol.replace("/", "").replace(":", "")
    if simbolo and simbolo.lower() not in [t.lower() for t in tags]:
        tags.append(simbolo)
    return " ".join(f"#{t}" for t in tags)


def post_apertura(side, symbol, entry_price, tp_pct, sl_pct,
                   leverage, timeout_velas=None, scenario=None, log=None):
    """
    Publica la apertura de una operacion. Nunca lanza excepciones hacia afuera:
    un fallo aca jamas debe frenar al bot de trading.
    """
    if not _config_lista():
        return

    try:
        img_bytes = generar_imagen_apertura(
            symbol=symbol, side=side, entry_price=entry_price,
            tp_pct=tp_pct, sl_pct=sl_pct, leverage=leverage, scenario=scenario,
            fondo_path=os.environ.get("IG_BACKGROUND_PATH"),
            timeout_velas=timeout_velas,
        )
        image_url = _subir_a_imgbb(img_bytes, _env_limpio("IMGBB_API_KEY"))

        direccion_en = "LONG" if side == "buy" else "SHORT"
        _tp_str = f"+{tp_pct:.2f}%" if tp_pct is not None else "N/A"
        _sl_str = f"-{sl_pct:.2f}%" if sl_pct is not None else "N/A"
        _to_str = f"{timeout_velas} candles" if timeout_velas else "N/A"
        caption = (
            f"New position opened / Nueva posición abierta\n\n"
            f"{symbol} -- {direccion_en}\n"
            f"Entry / Entrada: ${entry_price:,.4f}\n"
            f"TP: {_tp_str} | SL: {_sl_str}\n"
            f"Timeout: {_to_str}\n\n"
            f"{_armar_hashtags(symbol)}"
        )
        media_id = _publicar_en_instagram(
            _env_limpio("IG_USER_ID"), _env_limpio("IG_ACCESS_TOKEN"), image_url, caption
        )
        _log_fn(log, "success", f"[IG] Apertura publicada en Instagram (media_id={media_id})")
    except Exception as e:
        _log_fn(log, "warning", f"[IG] No se pudo publicar apertura en Instagram: {e}")


def post_cierre(side, symbol, entry_price, exit_price, pnl_usd,
                 scenario=None, log=None):
    """
    Publica el resultado de una operacion cerrada (ganancia o perdida).
    Misma filosofia que post_apertura: nunca rompe el bot, correr en un hilo.
    """
    if not _config_lista():
        return
    try:
        img_bytes = generar_imagen_cierre(
            symbol=symbol, side=side, entry_price=entry_price,
            exit_price=exit_price, pnl_usd=pnl_usd, scenario=scenario,
            fondo_path=os.environ.get("IG_BACKGROUND_PATH"),
        )
        image_url = _subir_a_imgbb(img_bytes, _env_limpio("IMGBB_API_KEY"))

        gano = pnl_usd > 0
        if gano:
            header = "Position closed with profit / Operacion cerrada con ganancia"
        else:
            header = "Position closed with loss / Operacion cerrada con perdida"
        caption = (
            f"{header}\n\n"
            f"{symbol}\n"
            f"Entry / Entrada: ${entry_price:,.4f} -> Exit / Salida: ${exit_price:,.4f}\n"
            f"PnL: ${pnl_usd:+,.2f}\n\n"
            f"{_armar_hashtags(symbol)}"
        )
        media_id = _publicar_en_instagram(
            _env_limpio("IG_USER_ID"), _env_limpio("IG_ACCESS_TOKEN"), image_url, caption
        )
        _log_fn(log, "success", f"[IG] Cierre publicado en Instagram (media_id={media_id})")
    except Exception as e:
        _log_fn(log, "warning", f"[IG] No se pudo publicar cierre en Instagram: {e}")


# ══════════════════════════════════════════════════════════════════
# 5. API PÚBLICA — esto es lo ÚNICO que live_worker.py necesita llamar
#    (encapsula el threading.Thread y el manejo de errores)
# ══════════════════════════════════════════════════════════════════

def notificar_apertura(**kwargs):
    """Dispara la publicación de apertura en un hilo aparte. Nunca bloquea ni lanza excepciones."""
    log = kwargs.get("log")
    try:
        threading.Thread(target=post_apertura, kwargs=kwargs, daemon=True).start()
    except Exception as e:
        _log_fn(log, "warning", f"⚠️ No se pudo lanzar publicación de apertura en Instagram: {e}")


def notificar_cierre(**kwargs):
    """Dispara la publicación de cierre en un hilo aparte. Nunca bloquea ni lanza excepciones."""
    log = kwargs.get("log")
    try:
        threading.Thread(target=post_cierre, kwargs=kwargs, daemon=True).start()
    except Exception as e:
        _log_fn(log, "warning", f"⚠️ No se pudo lanzar publicación de cierre en Instagram: {e}")


"""
GUÍA DE CONFIGURACIÓN
══════════════════════════════════════════════════════════════════
1) Convertí tu cuenta de Instagram a cuenta "Profesional" (Business o Creator)
   desde la app de Instagram. Con el flujo que usa esta guía (Instagram Login,
   ver paso 4) NO hace falta vincularla a una Página de Facebook.

2) Entrá a https://developers.facebook.com → creá una app tipo "Business".
   Agregá el producto "Instagram" a la app.

3) Como vos sos el dueño/único usuario de la app, NO necesitás pasar por
   Meta App Review: dejá la app en modo "Development" y agregate a vos
   mismo con el rol "Instagram Tester" (App roles → Roles → Add People).

4) Generá un token de acceso de larga duración desde
   Instagram → API setup with Instagram login → "Generate access tokens".
   Guardalo como IG_ACCESS_TOKEN.
   (Este es el motivo por el que el código llama a graph.instagram.com y no
   a graph.facebook.com — son dos productos distintos de Meta, con host
   distinto; ver GRAPH_HOST arriba en este archivo.)

5) El mismo panel te muestra el ID numérico de tu cuenta profesional
   → guardalo como IG_USER_ID.

6) Creá una cuenta gratis en https://imgbb.com/, andá a
   https://api.imgbb.com/ y copiá tu API key → guardala como IMGBB_API_KEY.

7) En Railway, agregá estas variables de entorno al servicio del bot:
     INSTAGRAM_ENABLED = true
     IG_ACCESS_TOKEN    = <tu token>
     IG_USER_ID          = <tu id numérico>
     IMGBB_API_KEY        = <tu api key de imgbb>

   Opcionales:
     IG_BACKGROUND_PATH = ruta a una imagen propia (logo, foto, textura) que se
                           usa de fondo en la tarjeta. Si no la seteás, se usa
                           un degradado oscuro con el color verde/rojo de la
                           operación. La imagen se sube al repo junto al bot,
                           por ej. "assets/fondo.jpg", y acá se referencia esa
                           ruta relativa. Cualquier tamaño/proporción sirve —
                           se recorta automáticamente a 1080x1350 (4:5).
     IG_HASHTAGS         = tus propios hashtags, separados por coma o espacio,
                           con o sin '#' (ej: "trading, futuros, cripto").
                           Si no la seteás, se usan los hashtags por defecto.

Sobre los hashtags: Instagram NO los genera ni sugiere automáticamente para
publicaciones hechas por API — esa sugerencia solo existe dentro de la app
cuando una persona escribe el caption a mano. Por eso el módulo arma el
caption (texto + hashtags) por su cuenta antes de publicar; ver _armar_hashtags().

Sobre la fuente: se usa Poppins (Google Fonts, licencia libre), incluida en
la carpeta fonts/ que se distribuye junto a este archivo — así el resultado
se ve igual en cualquier servidor, sin depender de qué fuentes tenga
instaladas el contenedor de Railway.

Límites a tener en cuenta (Meta, 2026):
  - Máx. ~25 publicaciones vía API por cuenta cada 24hs.
  - El token de acceso de larga duración dura ~60 días y hay que
    refrescarlo (podés automatizarlo después; por ahora, renovación manual).
"""
