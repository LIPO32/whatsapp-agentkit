# agent/tools.py — Herramientas del agente para Ventas Natura
# Generado por AgentKit

"""
Herramientas específicas del negocio.
Gestión de stock, pedidos, agenda y notificaciones para Silvana Natura.
"""

import os
import json
import yaml
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("agentkit")

# Archivo donde se guarda el stock de productos disponibles
STOCK_FILE = Path("config/stock.json")

# Archivo donde se guardan los pedidos registrados
PEDIDOS_FILE = Path("config/pedidos.json")


# ─────────────────────────────────────────────
# STOCK
# ─────────────────────────────────────────────

def cargar_stock() -> dict:
    """Carga el stock actual desde config/stock.json."""
    if not STOCK_FILE.exists():
        return {}
    try:
        return json.loads(STOCK_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Error leyendo stock: {e}")
        return {}


def guardar_stock(stock: dict):
    """Persiste el stock actualizado en config/stock.json."""
    STOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    STOCK_FILE.write_text(json.dumps(stock, ensure_ascii=False, indent=2), encoding="utf-8")


def agregar_producto_stock(nombre: str, descripcion: str, precio: float, cantidad: int = 1):
    """
    Agrega o actualiza un producto en el stock.
    La dueña puede llamar a esto cuando saca una foto y quiere registrar un producto.
    """
    stock = cargar_stock()
    stock[nombre.lower()] = {
        "nombre": nombre,
        "descripcion": descripcion,
        "precio": precio,
        "cantidad": cantidad,
        "actualizado": datetime.utcnow().isoformat()
    }
    guardar_stock(stock)
    logger.info(f"Producto agregado al stock: {nombre}")
    return stock[nombre.lower()]


def verificar_stock(producto: str) -> dict:
    """
    Verifica si un producto está disponible en stock.
    Retorna la info del producto o None si no está en stock.
    """
    stock = cargar_stock()
    producto_lower = producto.lower()
    # Búsqueda exacta primero
    if producto_lower in stock:
        return stock[producto_lower]
    # Búsqueda parcial por nombre
    for clave, datos in stock.items():
        if producto_lower in clave or clave in producto_lower:
            return datos
    return {}


def listar_stock() -> list[dict]:
    """Retorna todos los productos disponibles en stock."""
    stock = cargar_stock()
    return [v for v in stock.values() if v.get("cantidad", 0) > 0]


def reducir_stock(producto: str, cantidad: int = 1):
    """Reduce la cantidad disponible de un producto al confirmar un pedido."""
    stock = cargar_stock()
    producto_lower = producto.lower()
    if producto_lower in stock:
        stock[producto_lower]["cantidad"] = max(0, stock[producto_lower]["cantidad"] - cantidad)
        guardar_stock(stock)


# ─────────────────────────────────────────────
# PEDIDOS
# ─────────────────────────────────────────────

def cargar_pedidos() -> list:
    """Carga todos los pedidos registrados."""
    if not PEDIDOS_FILE.exists():
        return []
    try:
        return json.loads(PEDIDOS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Error leyendo pedidos: {e}")
        return []


def guardar_pedidos(pedidos: list):
    """Persiste la lista de pedidos."""
    PEDIDOS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PEDIDOS_FILE.write_text(json.dumps(pedidos, ensure_ascii=False, indent=2), encoding="utf-8")


def registrar_pedido(
    telefono: str,
    nombre_cliente: str,
    productos: list[dict],
    modalidad_entrega: str,
    direccion: str = "",
    fecha_entrega: str = "",
    notas: str = ""
) -> dict:
    """
    Registra un pedido confirmado.

    Args:
        telefono: Número del cliente
        nombre_cliente: Nombre del cliente si lo proporcionó
        productos: Lista de dicts con {"nombre": ..., "cantidad": ..., "modalidad": "stock"|"pedido_especial"}
        modalidad_entrega: "retiro" o "envio_domicilio"
        direccion: Dirección si es envío a domicilio
        fecha_entrega: Fecha y hora acordada
        notas: Observaciones adicionales
    """
    pedidos = cargar_pedidos()
    nuevo_pedido = {
        "id": len(pedidos) + 1,
        "telefono": telefono,
        "nombre_cliente": nombre_cliente,
        "productos": productos,
        "modalidad_entrega": modalidad_entrega,
        "direccion": direccion,
        "fecha_entrega": fecha_entrega,
        "notas": notas,
        "estado": "pendiente",
        "fecha_registro": datetime.utcnow().isoformat()
    }
    pedidos.append(nuevo_pedido)
    guardar_pedidos(pedidos)

    # Reducir stock para productos que estaban en stock
    for prod in productos:
        if prod.get("modalidad") == "stock":
            reducir_stock(prod["nombre"], prod.get("cantidad", 1))

    logger.info(f"Pedido #{nuevo_pedido['id']} registrado para {telefono}")
    return nuevo_pedido


def obtener_pedidos_pendientes() -> list[dict]:
    """Retorna todos los pedidos en estado pendiente."""
    pedidos = cargar_pedidos()
    return [p for p in pedidos if p.get("estado") == "pendiente"]


def cancelar_pedido(pedido_id: int) -> bool:
    """Cancela un pedido por su ID."""
    pedidos = cargar_pedidos()
    for pedido in pedidos:
        if pedido["id"] == pedido_id:
            pedido["estado"] = "cancelado"
            guardar_pedidos(pedidos)
            return True
    return False


def reprogramar_pedido(pedido_id: int, nueva_fecha: str) -> bool:
    """Reprograma la fecha de entrega de un pedido."""
    pedidos = cargar_pedidos()
    for pedido in pedidos:
        if pedido["id"] == pedido_id:
            pedido["fecha_entrega"] = nueva_fecha
            pedido["estado"] = "reprogramado"
            guardar_pedidos(pedidos)
            return True
    return False


# ─────────────────────────────────────────────
# CATÁLOGOS
# ─────────────────────────────────────────────

def obtener_info_negocio() -> dict:
    """Carga la información del negocio desde business.yaml."""
    try:
        with open("config/business.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/business.yaml no encontrado")
        return {}


def listar_catalogos_disponibles() -> list[str]:
    """Lista los catálogos PDF disponibles en /knowledge."""
    knowledge = Path("knowledge")
    if not knowledge.exists():
        return []
    archivos = [
        f.name for f in knowledge.iterdir()
        if f.is_file() and not f.name.startswith(".")
    ]
    archivos.sort()
    return archivos
