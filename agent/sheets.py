# agent/sheets.py — Integración con Google Sheets para stock en tiempo real
# Generado por AgentKit

"""
Lee y actualiza el stock de productos desde Google Sheets.
Columnas del sheet: MARCA | CANTIDAD | NOMBRE | PRECIO | CATEGORIA | LINK

Autenticación:
- Local: archivo en GOOGLE_CREDENTIALS_PATH (default: credentials/google_sheets.json)
- Railway: variable de entorno GOOGLE_CREDENTIALS_JSON (contenido del JSON como string)

El service account debe tener acceso de Editor al Google Sheet.
"""

import asyncio
import json
import logging
import os

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger("agentkit")

SHEET_ID = os.getenv("GOOGLE_SHEETS_ID", "1KaWI3OHx6-BgbyXIGtbRW49izGnk8Kh9sx6edxgyJik")
CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials/google_sheets.json")

SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

# Orden exacto de columnas del sheet (A=0, B=1, C=2, ...)
_COL_MARCA = 0
_COL_CANTIDAD = 1
_COL_NOMBRE = 2
_COL_PRECIO = 3
_COL_CATEGORIA = 4
_COL_LINK = 5


# ── Autenticación ─────────────────────────────────────────────────────────────

def _get_client() -> gspread.Client:
    """
    Crea un cliente gspread autenticado.
    Usa GOOGLE_CREDENTIALS_JSON (Railway) si existe, sino el archivo local.
    """
    json_var = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if json_var:
        info = json.loads(json_var)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(CREDENTIALS_PATH, scopes=SCOPES)
    return gspread.authorize(creds)


def _get_sheet() -> gspread.Worksheet:
    """Retorna la primera hoja del Google Sheet configurado."""
    client = _get_client()
    return client.open_by_key(SHEET_ID).sheet1


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fila_a_dict(fila: list) -> dict:
    """Convierte una fila (lista) a dict con las claves del sheet."""
    def _get(idx: int) -> str:
        return fila[idx].strip() if idx < len(fila) else ""

    return {
        "MARCA": _get(_COL_MARCA),
        "CANTIDAD": _get(_COL_CANTIDAD),
        "NOMBRE": _get(_COL_NOMBRE),
        "PRECIO": _get(_COL_PRECIO),
        "CATEGORIA": _get(_COL_CATEGORIA),
        "LINK": _get(_COL_LINK),
    }


def _nombre_coincide(nombre_producto: str, query: str) -> bool:
    """
    Retorna True si el query aparece como subcadena en el nombre del producto.
    Mínimo 4 caracteres para evitar falsos positivos con queries muy cortos.
    """
    q = query.lower().strip()
    if len(q) < 4:
        return False
    return q in nombre_producto.lower()


def _es_fila_header(fila: list) -> bool:
    """Detecta si la primera fila es la cabecera del sheet."""
    return bool(fila) and fila[0].upper().strip() == "MARCA"


# ── Funciones síncronas (se ejecutan en executor) ────────────────────────────

def _sync_get_stock() -> list[dict]:
    """Lee todas las filas de datos y las retorna como lista de dicts."""
    try:
        sheet = _get_sheet()
        filas = sheet.get_all_values()
        if not filas:
            return []
        datos = filas[1:] if _es_fila_header(filas[0]) else filas
        return [_fila_a_dict(f) for f in datos if any(c.strip() for c in f)]
    except Exception as e:
        logger.error(f"[Sheets] Error leyendo stock: {e}")
        return []


def _sync_get_producto(nombre: str) -> dict | None:
    """Busca el primer producto cuyo NOMBRE contenga el query. Retorna None si no encuentra."""
    try:
        for producto in _sync_get_stock():
            if _nombre_coincide(producto["NOMBRE"], nombre):
                return producto
        return None
    except Exception as e:
        logger.error(f"[Sheets] Error buscando '{nombre}': {e}")
        return None


def _sync_descontar_unidad(nombre: str) -> bool:
    """
    Resta 1 a CANTIDAD del primer producto que coincida con el nombre.
    Si CANTIDAD llega a 0, la deja en 0 (no va a negativo).
    Retorna True si se actualizó correctamente.
    """
    try:
        sheet = _get_sheet()
        filas = sheet.get_all_values()
        if not filas:
            return False

        tiene_header = _es_fila_header(filas[0])
        datos = filas[1:] if tiene_header else filas

        for idx, fila in enumerate(datos):
            nombre_fila = fila[_COL_NOMBRE].strip() if len(fila) > _COL_NOMBRE else ""
            if not _nombre_coincide(nombre_fila, nombre):
                continue

            try:
                cantidad_actual = int(fila[_COL_CANTIDAD]) if len(fila) > _COL_CANTIDAD and fila[_COL_CANTIDAD].strip() else 0
            except (ValueError, TypeError):
                cantidad_actual = 0

            nueva_cantidad = max(0, cantidad_actual - 1)

            # Fila en gspread es 1-based; si hay header, los datos empiezan en fila 2
            row_num = idx + 2 if tiene_header else idx + 1
            sheet.update_cell(row_num, _COL_CANTIDAD + 1, nueva_cantidad)

            logger.info(
                f"[Sheets] Stock actualizado: '{nombre_fila}' "
                f"{cantidad_actual} → {nueva_cantidad}"
            )
            return True

        logger.warning(f"[Sheets] No se encontró producto para descontar: '{nombre}'")
        return False

    except Exception as e:
        logger.error(f"[Sheets] Error descontando unidad de '{nombre}': {e}")
        return False


# ── API pública async ─────────────────────────────────────────────────────────

async def get_stock() -> list[dict]:
    """Lee todas las filas del sheet y retorna lista de productos como dicts."""
    return await asyncio.to_thread(_sync_get_stock)


async def get_producto(nombre: str) -> dict | None:
    """Busca un producto por nombre (búsqueda flexible). Retorna dict o None."""
    return await asyncio.to_thread(_sync_get_producto, nombre)


async def descontar_unidad(nombre: str) -> bool:
    """Resta 1 a CANTIDAD del producto encontrado. Mínimo 0. Retorna True si fue exitoso."""
    return await asyncio.to_thread(_sync_descontar_unidad, nombre)


async def obtener_stock_para_prompt() -> str:
    """
    Retorna el stock formateado para inyectar en el system prompt de Claude.
    Los datos del sheet tienen prioridad sobre los catálogos estáticos de /knowledge.
    """
    productos = await get_stock()
    if not productos:
        return ""

    lineas = [
        "## Stock en tiempo real (Google Sheets — prioridad sobre catálogos)",
        "Reglas de disponibilidad:",
        "- CANTIDAD = 0 → NO está en stock. Informar al cliente y ofrecer pedido especial.",
        "- CANTIDAD ≥ 1 → Disponible. Informar precio y cantidad. Si hay LINK, ofrecerlo.",
        "",
        "NOMBRE | MARCA | PRECIO | CANTIDAD | CATEGORIA | LINK",
        "─" * 70,
    ]

    for p in productos:
        nombre = p["NOMBRE"]
        marca = p["MARCA"]
        precio = p["PRECIO"]
        cantidad = p["CANTIDAD"] or "0"
        categoria = p["CATEGORIA"]
        link = p["LINK"]

        estado = f"CANT:{cantidad}" if cantidad != "0" else "CANT:0 ⚠SIN STOCK"
        entrada = f"{nombre} | {marca} | {precio} | {estado} | {categoria}"
        if link and cantidad != "0":
            entrada += f" | {link}"
        lineas.append(entrada)

    return "\n".join(lineas)


async def extraer_producto_de_venta(
    texto_cliente: str,
    respuesta_agente: str,
) -> str | None:
    """
    Intenta identificar el producto vendido buscando nombres conocidos del sheet
    en el texto del cliente y la respuesta del agente.
    Retorna el NOMBRE exacto del producto encontrado, o None si no se identifica.
    """
    productos = await get_stock()
    if not productos:
        return None

    texto_combinado = (texto_cliente + " " + respuesta_agente).lower()

    for p in productos:
        nombre = p.get("NOMBRE", "").strip()
        if nombre and len(nombre) >= 4 and nombre.lower() in texto_combinado:
            return nombre

    return None
