# agent/providers/__init__.py — Factory de proveedores
# Generado por AgentKit

"""
Selecciona el proveedor de WhatsApp según la variable WHATSAPP_PROVIDER en .env.
"""

import os
from agent.providers.base import ProveedorWhatsApp


def obtener_proveedor() -> ProveedorWhatsApp:
    """Retorna el proveedor de WhatsApp configurado en .env."""
    proveedor = os.getenv("WHATSAPP_PROVIDER", "whapi").lower()

    if proveedor == "whapi":
        from agent.providers.whapi import ProveedorWhapi
        return ProveedorWhapi()
    elif proveedor == "zapi":
        from agent.providers.zapi import ProveedorZapi
        return ProveedorZapi()
    elif proveedor == "twilio":
        from agent.providers.twilio import ProveedorTwilio
        return ProveedorTwilio()
    else:
        raise ValueError(f"Proveedor no soportado: {proveedor}. Usá: whapi | zapi | twilio")
