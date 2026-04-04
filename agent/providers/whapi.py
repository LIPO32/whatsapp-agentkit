# agent/providers/whapi.py — Adaptador para Whapi.cloud
# Generado por AgentKit

import os
import base64
import logging
import httpx
from fastapi import Request
from agent.providers.base import ProveedorWhatsApp, MensajeEntrante

logger = logging.getLogger("agentkit")

WHAPI_URL_ENVIO = "https://gate.whapi.cloud/messages/text"
WHAPI_URL_DOCUMENTO = "https://gate.whapi.cloud/messages/document"


class ProveedorWhapi(ProveedorWhatsApp):
    """Proveedor de WhatsApp usando Whapi.cloud (REST API simple)."""

    def __init__(self):
        self.token = os.getenv("WHAPI_TOKEN")
        if not self.token:
            logger.error("WHAPI_TOKEN no está definido en las variables de entorno")

    def _formatear_telefono(self, telefono: str) -> str:
        """
        Normaliza el número de teléfono al formato que acepta Whapi para ENVÍO.

        Whapi requiere: NUMEROPAIS@s.whatsapp.net
        Ejemplo correcto: 542215639673@s.whatsapp.net
        Ejemplo incorrecto: +542215639673  |  542215639673  |  542215639673@c.us

        Los mensajes entrantes (chat_id) ya traen @s.whatsapp.net o @c.us.
        Los números guardados en env vars (ej: TELEFONO_DUENA) vienen sin sufijo.
        """
        numero = (
            telefono
            .strip()
            .replace("+", "")
            .replace(" ", "")
            .replace("-", "")
        )
        # Reemplazar @c.us por @s.whatsapp.net (formato incorrecto para envío)
        if numero.endswith("@c.us"):
            numero = numero.replace("@c.us", "@s.whatsapp.net")

        # Agregar sufijo si no lo tiene
        if not numero.endswith("@s.whatsapp.net"):
            numero = f"{numero}@s.whatsapp.net"

        return numero

    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        """Parsea el payload de Whapi.cloud."""
        body = await request.json()
        mensajes = []
        for msg in body.get("messages", []):
            mensajes.append(MensajeEntrante(
                telefono=msg.get("chat_id", ""),
                texto=msg.get("text", {}).get("body", ""),
                mensaje_id=msg.get("id", ""),
                es_propio=msg.get("from_me", False),
            ))
        return mensajes

    async def enviar_mensaje(self, telefono: str, mensaje: str) -> bool:
        """
        Envía un mensaje de texto via Whapi.cloud.

        Logs detallados en cada paso para facilitar el diagnóstico de envíos fallidos.
        """
        if not self.token:
            logger.error(
                f"[Whapi] FALLO — WHAPI_TOKEN no configurado. "
                f"Mensaje a '{telefono}' NO enviado."
            )
            return False

        telefono_fmt = self._formatear_telefono(telefono)
        payload = {"to": telefono_fmt, "body": mensaje}
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

        logger.info(
            f"[Whapi] Enviando mensaje → destino: '{telefono_fmt}' "
            f"(original: '{telefono}') | longitud: {len(mensaje)} chars | "
            f"preview: '{mensaje[:80].replace(chr(10), ' ')}...'"
        )

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    WHAPI_URL_ENVIO,
                    json=payload,
                    headers=headers,
                )

            if response.status_code == 200:
                logger.info(
                    f"[Whapi] OK — Mensaje enviado a '{telefono_fmt}' | "
                    f"status: {response.status_code} | response: {response.text[:200]}"
                )
                return True
            else:
                logger.error(
                    f"[Whapi] FALLO — destino: '{telefono_fmt}' | "
                    f"status: {response.status_code} | "
                    f"error: {response.text[:500]}"
                )
                return False

        except httpx.TimeoutException:
            logger.error(
                f"[Whapi] TIMEOUT — destino: '{telefono_fmt}' | "
                "La solicitud tardó más de 15 segundos"
            )
            return False
        except Exception as e:
            logger.error(
                f"[Whapi] EXCEPCIÓN — destino: '{telefono_fmt}' | error: {e}"
            )
            return False

    async def enviar_documento(self, telefono: str, ruta_archivo: str, nombre_archivo: str, caption: str = "") -> bool:
        """
        Envía un archivo como documento via Whapi.cloud.
        Lee el archivo, lo codifica en base64 y lo envía al endpoint /messages/document.
        """
        if not self.token:
            logger.error("[Whapi] FALLO enviar_documento — WHAPI_TOKEN no configurado")
            return False

        telefono_fmt = self._formatear_telefono(telefono)

        try:
            with open(ruta_archivo, "rb") as f:
                contenido = f.read()
        except OSError as e:
            logger.error(f"[Whapi] FALLO enviar_documento — no se pudo leer '{ruta_archivo}': {e}")
            return False

        # Detectar MIME type según extensión
        ext = nombre_archivo.rsplit(".", 1)[-1].lower() if "." in nombre_archivo else ""
        mime_map = {
            "ics": "text/calendar",
            "pdf": "application/pdf",
            "txt": "text/plain",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "csv": "text/csv",
        }
        mime = mime_map.get(ext, "application/octet-stream")

        b64 = base64.b64encode(contenido).decode("utf-8")
        data_uri = f"data:{mime};base64,{b64}"

        payload = {
            "to": telefono_fmt,
            "media": data_uri,
            "filename": nombre_archivo,
        }
        if caption:
            payload["caption"] = caption

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

        logger.info(f"[Whapi] Enviando documento '{nombre_archivo}' → '{telefono_fmt}'")

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(WHAPI_URL_DOCUMENTO, json=payload, headers=headers)

            if response.status_code == 200:
                logger.info(f"[Whapi] Documento enviado OK → '{telefono_fmt}'")
                return True
            else:
                logger.error(
                    f"[Whapi] FALLO enviar_documento → '{telefono_fmt}' | "
                    f"status: {response.status_code} | {response.text[:500]}"
                )
                return False

        except Exception as e:
            logger.error(f"[Whapi] EXCEPCIÓN enviar_documento → '{telefono_fmt}' | {e}")
            return False
