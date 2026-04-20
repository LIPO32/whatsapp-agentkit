import os
import logging
import httpx
from fastapi import Request
from agent.providers.base import ProveedorWhatsApp, MensajeEntrante

logger = logging.getLogger("agentkit")


class ProveedorTwilio(ProveedorWhatsApp):
    """Proveedor de WhatsApp usando Twilio (sandbox y producción)."""

    def __init__(self):
        self.account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        self.auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        self.from_number = os.getenv("TWILIO_PHONE_NUMBER", "")

        if not self.account_sid:
            logger.error("[Twilio] TWILIO_ACCOUNT_SID no configurado")
        if not self.auth_token:
            logger.error("[Twilio] TWILIO_AUTH_TOKEN no configurado")

        self.api_url = f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Messages.json"

    def _formatear_para_twilio(self, telefono: str) -> str:
        """Convierte número al formato whatsapp:+XXXXXXXXXXX que usa Twilio."""
        if telefono.startswith("whatsapp:"):
            return telefono
        numero = telefono.strip().replace(" ", "").replace("-", "")
        if not numero.startswith("+"):
            numero = f"+{numero}"
        return f"whatsapp:{numero}"

    def _extraer_numero(self, telefono_twilio: str) -> str:
        """Extrae el número limpio desde 'whatsapp:+XXXXXXXXXXX'."""
        return telefono_twilio.replace("whatsapp:", "").strip()

    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        """
        Parsea el payload de Twilio.

        Twilio envía form-encoded (no JSON):
          From=whatsapp%3A%2B5491XXXXXXXXX
          Body=Hola!
          MessageSid=SMxxx
          WaId=5491XXXXXXXXX
        """
        try:
            form = await request.form()
        except Exception as e:
            logger.error(f"[Twilio] Error al parsear form del webhook: {e}")
            return []

        from_raw = form.get("From", "")
        body = form.get("Body", "")
        message_sid = form.get("MessageSid", "")

        if not from_raw or not body:
            return []

        telefono = self._extraer_numero(from_raw)

        return [MensajeEntrante(
            telefono=telefono,
            texto=body,
            mensaje_id=message_sid,
            es_propio=False,
        )]

    async def enviar_mensaje(self, telefono: str, mensaje: str) -> bool:
        """Envía un mensaje de texto via Twilio WhatsApp."""
        if not self.account_sid or not self.auth_token:
            logger.error(f"[Twilio] FALLO — credenciales no configuradas. Mensaje a '{telefono}' NO enviado.")
            return False

        to = self._formatear_para_twilio(telefono)
        from_num = self._formatear_para_twilio(self.from_number)

        logger.info(f"[Twilio] Enviando mensaje → destino: '{to}' | preview: '{mensaje[:80]}'")

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    self.api_url,
                    data={"From": from_num, "To": to, "Body": mensaje},
                    auth=(self.account_sid, self.auth_token),
                )

            if response.status_code in (200, 201):
                logger.info(f"[Twilio] OK — Mensaje enviado a '{to}'")
                return True
            else:
                logger.error(f"[Twilio] FALLO → '{to}' | status: {response.status_code} | {response.text[:500]}")
                return False

        except httpx.TimeoutException:
            logger.error(f"[Twilio] TIMEOUT → '{to}'")
            return False
        except Exception as e:
            logger.error(f"[Twilio] EXCEPCIÓN → '{to}' | {e}")
            return False
