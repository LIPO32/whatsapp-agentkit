# agent/brain.py — Cerebro del agente: conexión con Claude API
# Generado por AgentKit

"""
Lógica de IA del agente. Lee el system prompt de prompts.yaml,
filtra el uso de catálogos según el tipo de mensaje, y genera
respuestas usando la API de Anthropic Claude con reintentos limitados.
"""

import asyncio
import os
import yaml
import logging
from pathlib import Path
from anthropic import AsyncAnthropic, APIStatusError
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("agentkit")

client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
KNOWLEDGE_DIR = Path("knowledge")

# ── Estado global del agente ──────────────────────────────────────────────────
_agente_detenido = False


def agente_detenido() -> bool:
    """Retorna True si el agente fue detenido por error crítico (error 402)."""
    return _agente_detenido


# ── System prompts internos ───────────────────────────────────────────────────

SYSTEM_RESUMEN = """
Sos un asistente interno de Ventas Natura. Tu única tarea es analizar conversaciones de
WhatsApp entre el agente y un cliente, y generar un resumen de venta para Silvana,
la dueña del negocio.

REGLA CRÍTICA:
- Si en la conversación se CONFIRMÓ una venta, un pedido o se ACORDÓ una entrega,
  generá el resumen con el formato indicado abajo.
- Si la conversación es solo una consulta sin venta cerrada ni entrega acordada,
  respondé únicamente con el texto: NO_RESUMEN

Cuando haya venta o entrega confirmada, usá exactamente este formato:

---RESUMEN PARA SILVANA---
Cliente: [nombre si lo mencionó, si no el número de teléfono]
Fecha: [fecha y hora del resumen]

PEDIDO:
- [Producto] x[cantidad] — [en stock / pedido especial]

ENTREGA:
- Modalidad: [retiro en persona / envío a domicilio]
- Dirección: [dirección completa, si aplica; sino "no aplica"]
- Fecha y hora acordada: [la que se mencionó, o "a confirmar"]

ACCIÓN REQUERIDA: [qué debe hacer Silvana concretamente, ej: "Preparar pedido para entrega
el martes", "Incluir en próximo pedido Natura", "Confirmar dirección con el cliente"]
---FIN RESUMEN---
"""

SYSTEM_RESUMEN_CLIENTE = """
Sos un asistente interno que mantiene un perfil compacto de clientes de Natura.
Dado el historial de conversación y el perfil anterior (si existe), generá un resumen
actualizado del cliente.

Incluí ÚNICAMENTE información mencionada explícitamente en la conversación:
- Nombre del cliente
- Dirección de entrega
- Historial de compras confirmadas
- Preferencias de productos
- Datos relevantes para futuras atenciones (alergias, restricciones, etc.)

Respondé SOLO con el resumen en formato compacto (máximo 8 líneas).
Si no hay información nueva ni relevante, respondé únicamente con: SIN_DATOS
"""


# ── Filtros ────────────────────────────────────────────────────────────────────

# Palabras que indican que el mensaje necesita consultar el catálogo de productos
PALABRAS_CLAVE_PRODUCTOS = {
    "producto", "precio", "precios", "stock", "catálogo", "catalogo",
    "compra", "comprar", "compro", "pedido", "pedir", "pido",
    "entrega", "envío", "envio", "enviar", "entregar",
    "natura", "perfume", "crema", "maquillaje", "shampoo", "champú",
    "costo", "cuesta", "cuánto", "cuanto", "vale", "disponible",
    "oferta", "promoción", "promocion", "descuento", "kit", "set",
    "colonia", "loción", "locion", "labial", "base", "sérum", "serum",
    "hidratante", "tónico", "tonico", "tratamiento", "desodorante",
    "protector", "solar", "aceite", "jabón", "jabon", "revista",
    "novedad", "novedades", "nuevo", "nueva",
}

# Frases en el mensaje del CLIENTE que indican que va a ir en persona
SENALES_CLIENTE_YENDO = {
    "voy a pasar", "paso por", "voy para allá", "voy para alla",
    "voy en camino", "ya salgo", "salgo en", "llego en",
    "paso hoy", "paso mañana", "paso esta", "voy esta",
    "voy ahora", "voy ya", "me acerco", "paso a buscar",
    "paso a retirar", "voy a retirar", "voy a buscar",
    "cuándo puedo pasar", "cuando puedo pasar",
    "a qué hora puedo pasar", "a que hora puedo pasar",
    "estoy yendo", "estoy en camino", "voy a ir",
    "puedo pasar", "quisiera pasar", "quiero pasar",
}

# Frases en la RESPUESTA DEL AGENTE que prometen avisar a Silvana (sin hacerlo)
SENALES_PROMESA_AVISO = {
    "le aviso a silvana", "le digo a silvana", "aviso a silvana",
    "le comunico a silvana", "notifico a silvana",
    "le voy a avisar", "le voy a decir a silvana",
    "ya le aviso", "ya le digo", "le paso el mensaje a silvana",
    "silvana te va a estar esperando", "silvana va a estar",
    "le mando un mensaje a silvana", "le escribo a silvana",
}

# Frases que indican que el agente no tuvo información suficiente para responder
SENALES_INFORMACION_FALTANTE = {
    "no tengo esa información",
    "no cuento con esa información",
    "no tengo acceso a esa información",
    "no encontré esa información",
    "no está disponible en este momento",
    "esa información no está disponible",
    "no tengo información disponible",
    "no tengo esos datos",
    "no dispongo de esa información",
    "no figura en los catálogos",
    "no encontré en los catálogos",
    "no tengo información sobre",
    "no cuento con información sobre",
}

# Palabras que indican nueva información del cliente (nombre, dirección, preferencias)
PALABRAS_CLAVE_INFO_CLIENTE = {
    "me llamo", "mi nombre es", "soy", "llámame", "llamame",
    "vivo en", "mi dirección", "mi casa", "mi domicilio",
    "barrio", "calle", "esquina",
    "me gusta", "me encanta", "prefiero", "no me gusta",
    "soy alérgica", "soy alergica", "tengo alergia", "soy sensible",
}


def necesita_knowledge(mensaje: str) -> bool:
    """
    Retorna True si el mensaje requiere consultar el catálogo de productos.
    Retorna False para saludos, agradecimientos y consultas no relacionadas a productos,
    reduciendo significativamente el costo por request.
    """
    mensaje_lower = mensaje.lower()
    return any(palabra in mensaje_lower for palabra in PALABRAS_CLAVE_PRODUCTOS)


def detecto_cliente_yendo_en_persona(texto_cliente: str) -> bool:
    """
    Retorna True si el cliente indica que va a ir en persona a buscar productos.
    Se usa para enviar alerta inmediata a Silvana.
    """
    texto_lower = texto_cliente.lower()
    return any(senal in texto_lower for senal in SENALES_CLIENTE_YENDO)


def detecto_promesa_aviso_silvana(respuesta: str) -> bool:
    """
    Retorna True si el agente prometió avisar a Silvana en su respuesta.
    Indica que hay que ejecutar esa notificación realmente vía Whapi.
    """
    respuesta_lower = respuesta.lower()
    return any(senal in respuesta_lower for senal in SENALES_PROMESA_AVISO)


def extraer_tiempo_llegada(texto: str) -> str:
    """
    Intenta extraer el tiempo de llegada mencionado en el mensaje del cliente.
    Retorna el texto encontrado o 'No especificado'.
    """
    import re
    patrones = [
        r"en \d+\s*minutos?",
        r"en \d+\s*horas?",
        r"en media hora",
        r"en un rato",
        r"ahora mismo",
        r"ya mismo",
        r"esta tarde",
        r"esta noche",
        r"esta mañana",
        r"mañana",
        r"en un momento",
        r"en \d+\s*rato",
        r"esta semana",
        r"el (lunes|martes|miércoles|miercoles|jueves|viernes|sábado|sabado|domingo)",
    ]
    texto_lower = texto.lower()
    for patron in patrones:
        match = re.search(patron, texto_lower)
        if match:
            return match.group(0)
    return "No especificado"


def detecto_informacion_faltante(respuesta: str) -> bool:
    """
    Retorna True si la respuesta del agente contiene señales de que no tuvo
    información suficiente para responder correctamente al cliente.
    Usado para activar la alerta a Silvana.
    """
    respuesta_lower = respuesta.lower()
    return any(senal in respuesta_lower for senal in SENALES_INFORMACION_FALTANTE)


def hay_info_nueva_cliente(mensaje: str) -> bool:
    """
    Retorna True si el mensaje contiene información nueva del cliente
    (nombre, dirección, preferencias) que justifique actualizar su perfil.
    """
    mensaje_lower = mensaje.lower()
    return any(palabra in mensaje_lower for palabra in PALABRAS_CLAVE_INFO_CLIENTE)


# ── Helpers de configuración ──────────────────────────────────────────────────

def cargar_config_prompts() -> dict:
    """Lee toda la configuración desde config/prompts.yaml."""
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/prompts.yaml no encontrado")
        return {}


def cargar_system_prompt() -> str:
    config = cargar_config_prompts()
    return config.get("system_prompt", "Eres un asistente útil. Respondé en español.")


def obtener_mensaje_error() -> str:
    config = cargar_config_prompts()
    return config.get(
        "error_message",
        "Lo siento, estoy teniendo un pequeño problema técnico. Por favor intentá de nuevo en unos minutos 🙏",
    )


def obtener_mensaje_fallback() -> str:
    config = cargar_config_prompts()
    return config.get(
        "fallback_message",
        "Disculpá, no entendí bien tu consulta. ¿Podés contarme qué producto estás buscando?",
    )


def leer_catalogos_pdf() -> str:
    """
    Lee los últimos 2 archivos de la carpeta /knowledge.
    Solo se llama cuando necesita_knowledge() retorna True.
    """
    if not KNOWLEDGE_DIR.exists():
        return ""

    archivos = []
    extensiones_validas = {".txt", ".md", ".csv", ".json"}

    for archivo in KNOWLEDGE_DIR.iterdir():
        if archivo.is_file() and not archivo.name.startswith("."):
            archivos.append(archivo)

    if not archivos:
        return ""

    archivos.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    archivos = archivos[:2]

    contenido_total = []
    for archivo in archivos:
        try:
            if archivo.suffix.lower() in extensiones_validas:
                texto = archivo.read_text(encoding="utf-8")
                contenido_total.append(f"=== Catálogo: {archivo.name} ===\n{texto}")
            elif archivo.suffix.lower() == ".pdf":
                try:
                    import pypdf
                    reader = pypdf.PdfReader(str(archivo))
                    texto = "\n".join(page.extract_text() or "" for page in reader.pages)
                    contenido_total.append(f"=== Catálogo PDF: {archivo.name} ===\n{texto}")
                except ImportError:
                    logger.warning(f"pypdf no instalado — no se pudo leer {archivo.name}")
        except Exception as e:
            logger.error(f"Error leyendo {archivo.name}: {e}")

    return "\n\n".join(contenido_total)


# ── Helper de llamadas a la API con reintentos ────────────────────────────────

async def _llamar_claude(**kwargs):
    """
    Llama a la API de Claude con manejo de errores y reintentos limitados.

    - Error 402 (sin créditos): no reintentar, detener el agente, propagar excepción.
    - Error 529 (API sobrecargada): no reintentar, propagar excepción.
    - Otros errores: hasta 2 reintentos con espera progresiva de 2s y 4s.
    """
    global _agente_detenido
    MAX_REINTENTOS = 2
    ESPERAS = [2, 4]

    for intento in range(MAX_REINTENTOS + 1):
        try:
            return await client.messages.create(**kwargs)

        except APIStatusError as e:
            if e.status_code == 402:
                _agente_detenido = True
                logger.critical(
                    "ERROR 402 — Créditos de API agotados. "
                    "El agente fue DETENIDO. Recargá créditos en platform.anthropic.com"
                )
                raise

            if e.status_code == 529:
                logger.error("ERROR 529 — API de Claude sobrecargada. Sin reintentos.")
                raise

            if intento < MAX_REINTENTOS:
                espera = ESPERAS[intento]
                logger.warning(
                    f"Error API {e.status_code} (intento {intento + 1}/{MAX_REINTENTOS}). "
                    f"Reintentando en {espera}s..."
                )
                await asyncio.sleep(espera)
            else:
                logger.error(f"Error Claude API tras {MAX_REINTENTOS} reintentos: {e}")
                raise

        except Exception as e:
            if intento < MAX_REINTENTOS:
                espera = ESPERAS[intento]
                logger.warning(
                    f"Error inesperado (intento {intento + 1}/{MAX_REINTENTOS}): {e}. "
                    f"Reintentando en {espera}s..."
                )
                await asyncio.sleep(espera)
            else:
                logger.error(f"Error inesperado tras {MAX_REINTENTOS} reintentos: {e}")
                raise


# ── Funciones principales ─────────────────────────────────────────────────────

async def generar_respuesta(
    mensaje: str,
    historial: list[dict],
    resumen_cliente: str | None = None,
) -> str:
    """
    Genera una respuesta usando Claude API.

    Args:
        mensaje: El mensaje nuevo del usuario.
        historial: Últimos 4 mensajes de la conversación (o historial completo en test local).
        resumen_cliente: Perfil compacto del cliente guardado en memoria (opcional).

    Returns:
        La respuesta generada por Claude.
    """
    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback()

    # System prompt base
    system_prompt = cargar_system_prompt()

    # Incorporar el perfil del cliente al contexto si existe
    if resumen_cliente:
        system_prompt += f"\n\n## Perfil del cliente actual\n{resumen_cliente}"

    # Solo cargar catálogos si el mensaje lo requiere — reduce costo por request
    if necesita_knowledge(mensaje):
        catalogos = leer_catalogos_pdf()
        if catalogos:
            logger.debug("Catálogo estático cargado para este request")
            system_prompt += f"\n\n## Catálogos de productos disponibles\n{catalogos}"

        # Stock en tiempo real desde Google Sheets (tiene prioridad sobre los catálogos)
        try:
            from agent.sheets import obtener_stock_para_prompt
            stock_texto = await obtener_stock_para_prompt()
            if stock_texto:
                logger.debug("Stock en tiempo real cargado para este request")
                system_prompt += f"\n\n{stock_texto}"
        except Exception as e:
            logger.warning(f"[Sheets] No se pudo cargar stock en tiempo real: {e}")
    else:
        logger.debug("Catálogo omitido (mensaje no relacionado a productos)")

    # Construir lista de mensajes: historial reciente + mensaje actual
    mensajes = [{"role": m["role"], "content": m["content"]} for m in historial]
    mensajes.append({"role": "user", "content": mensaje})

    try:
        response = await _llamar_claude(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system_prompt,
            messages=mensajes,
        )
        respuesta = response.content[0].text
        logger.info(
            f"Respuesta generada ({response.usage.input_tokens} in / {response.usage.output_tokens} out)"
        )
        return respuesta

    except APIStatusError as e:
        if e.status_code == 402:
            logger.critical("Agente detenido — sin créditos de API.")
        elif e.status_code == 529:
            logger.error("API sobrecargada — no se pudo generar respuesta.")
        return obtener_mensaje_error()

    except Exception:
        return obtener_mensaje_error()


async def generar_resumen_cliente(
    telefono: str,
    resumen_anterior: str | None,
    historial_reciente: list[dict],
) -> str | None:
    """
    Genera o actualiza el perfil compacto del cliente para memoria persistente.
    Se llama cuando el mensaje contiene información nueva del cliente o cuando
    se confirma una compra.

    Returns:
        Nuevo resumen del cliente, o None si no hay datos relevantes.
    """
    if not historial_reciente:
        return resumen_anterior

    contexto = ""
    if resumen_anterior:
        contexto = f"Perfil anterior del cliente:\n{resumen_anterior}\n\n"

    conversacion = "\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in historial_reciente
    )

    try:
        response = await _llamar_claude(
            model="claude-sonnet-4-6",
            max_tokens=256,
            system=SYSTEM_RESUMEN_CLIENTE,
            messages=[{
                "role": "user",
                "content": (
                    f"Número de teléfono: {telefono}\n\n"
                    f"{contexto}"
                    f"Conversación reciente:\n{conversacion}"
                ),
            }],
        )

        resultado = response.content[0].text.strip()

        if resultado == "SIN_DATOS":
            return resumen_anterior  # Sin cambios

        logger.info(f"Perfil de cliente actualizado: {telefono}")
        return resultado

    except Exception as e:
        logger.error(f"Error actualizando perfil de cliente {telefono}: {e}")
        return resumen_anterior  # Mantener el perfil anterior ante cualquier error


async def generar_resumen_para_silvana(
    telefono_cliente: str,
    historial_completo: list[dict],
) -> str | None:
    """
    Analiza la conversación y genera un resumen para Silvana si detecta
    que se cerró una venta o se acordó una entrega.
    """
    if not historial_completo:
        return None

    conversacion_texto = "\n".join(
        f"{m['role'].upper()}: {m['content']}"
        for m in historial_completo
    )

    try:
        response = await _llamar_claude(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=SYSTEM_RESUMEN,
            messages=[{
                "role": "user",
                "content": (
                    f"Número de teléfono del cliente: {telefono_cliente}\n\n"
                    f"Conversación:\n{conversacion_texto}"
                ),
            }],
        )

        resultado = response.content[0].text.strip()

        if resultado == "NO_RESUMEN" or "---RESUMEN PARA SILVANA---" not in resultado:
            return None

        logger.info("Venta detectada — resumen generado para Silvana")
        return resultado

    except Exception as e:
        logger.error(f"Error generando resumen para Silvana: {e}")
        return None
