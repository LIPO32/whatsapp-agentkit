# agent/providers/zapi.py — Adaptador para Z-API
# Generado por AgentKit

import os
import logging
import httpx
from fastapi import Request
from agent.providers.base import ProveedorWhatsApp, MensajeEntrante

logger = logging.getLogger("agentkit")


class ProveedorZapi(ProveedorWhatsApp):
    """Proveedor de WhatsApp usando Z-API (https://z-api.io)."""

    def __init__(self):
        self.instance_id = os.getenv("ZAPI_INSTANCE_ID")
        self.token = os.getenv("ZAPI_TOKEN")
        self.client_token = os.getenv("ZAPI_CLIENT_TOKEN")

        if not self.instance_id:
            logger.error("[Z-API] ZAPI_INSTANCE_ID no está definido en las variables de entorno")
        if not self.token:
            logger.error("[Z-API] ZAPI_TOKEN no está definido en las variables de entorno")

        self.url_base = f"https://api.z-api.io/instances/{self.instance_id}/token/{self.token}"

    def _formatear_telefono(self, telefono: str) -> str:
        """
        Normaliza el número de teléfono al formato que acepta Z-API para ENVÍO.

        Z-API requiere: solo el número con código de país, sin '+' ni '@'.
        Ejemplo correcto:   5491XXXXXXXXX
        Ejemplo incorrecto: +5491XXXXXXXXX  |  5491XXXXXXXXX@s.whatsapp.net

        Los webhooks entrantes de Z-API ya traen el número en este formato limpio.
        """
        numero = (
            telefono
            .strip()
            .replace("+", "")
            .replace(" ", "")
            .replace("-", "")
        )
        # Remover sufijos de WhatsApp si vienen de otro proveedor o histórico
        for sufijo in ("@s.whatsapp.net", "@c.us", "@g.us"):
            if numero.endswith(sufijo):
                numero = numero[: -len(sufijo)]
                break

        return numero

    def _headers(self) -> dict:
        """Construye los headers HTTP para las peticiones a Z-API."""
        headers = {"Content-Type": "application/json"}
        if self.client_token:
            headers["Client-Token"] = self.client_token
        return headers

    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        """
        Parsea el payload entrante de Z-API.

        Z-API envía un objeto JSON por webhook (un mensaje a la vez).
        Formato relevante para mensajes de texto:
        {
          "instanceId": "...",
          "messageId": "...",
          "phone": "5491XXXXXXXXX",
          "fromMe": false,
          "type": "ReceivedCallback",
          "text": { "message": "Hola!" }
        }

        Se ignoran callbacks que no sean mensajes de texto entrantes.
        """
        try:
            body = await request.json()
        except Exception as e:
            logger.error(f"[Z-API] Error al parsear JSON del webhook: {e}")
            return []

        # Z-API puede enviar distintos tipos de callback (estado de entrega, etc.)
        # Solo nos interesan los mensajes de texto entrantes.
        tipo = body.get("type", "")
        es_entrante = tipo in ("ReceivedCallback",) or not body.get("fromMe", False)

        texto_obj = body.get("text", {})
        # El texto puede estar en "text.message" o directamente en "text" como string
        if isinstance(texto_obj, dict):
            texto = texto_obj.get("message", "")
        elif isinstance(texto_obj, str):
            texto = texto_obj
        else:
            texto = ""

        telefono = body.get("phone", "")
        mensaje_id = body.get("messageId", "")
        es_propio = body.get("fromMe", False)

        # Filtrar: solo procesar mensajes de texto con contenido
        if not texto or not telefono:
            return []

        return [MensajeEntrante(
            telefono=telefono,
            texto=texto,
            mensaje_id=mensaje_id,
            es_propio=es_propio,
        )]

    async def enviar_mensaje(self, telefono: str, mensaje: str) -> bool:
        """
        Envía un mensaje de texto via Z-API.

        Endpoint: POST /instances/{INSTANCE_ID}/token/{TOKEN}/send-text
        Payload:  { "phone": "5491XXXXXXXXX", "message": "texto" }
        """
        if not self.instance_id or not self.token:
            logger.error(
                f"[Z-API] FALLO — ZAPI_INSTANCE_ID o ZAPI_TOKEN no configurados. "
                f"Mensaje a '{telefono}' NO enviado."
            )
            return False

        telefono_fmt = self._formatear_telefono(telefono)
        payload = {"phone": telefono_fmt, "message": mensaje}

        logger.info(
            f"[Z-API] Enviando mensaje → destino: '{telefono_fmt}' "
            f"(original: '{telefono}') | longitud: {len(mensaje)} chars | "
            f"preview: '{mensaje[:80].replace(chr(10), ' ')}...'"
        )

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    f"{self.url_base}/send-text",
                    json=payload,
                    headers=self._headers(),
                )

            if response.status_code == 200:
                logger.info(
                    f"[Z-API] OK — Mensaje enviado a '{telefono_fmt}' | "
                    f"status: {response.status_code} | response: {response.text[:200]}"
                )
                return True
            else:
                logger.error(
                    f"[Z-API] FALLO — destino: '{telefono_fmt}' | "
                    f"status: {response.status_code} | "
                    f"error: {response.text[:500]}"
                )
                return False

        except httpx.TimeoutException:
            logger.error(
                f"[Z-API] TIMEOUT — destino: '{telefono_fmt}' | "
                "La solicitud tardó más de 15 segundos"
            )
            return False
        except Exception as e:
            logger.error(
                f"[Z-API] EXCEPCIÓN — destino: '{telefono_fmt}' | error: {e}"
            )
            return False
