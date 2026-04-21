# agent/main.py — Servidor FastAPI + Webhook de WhatsApp
# Generado por AgentKit

"""
Servidor principal del agente Silvana Natura.
Responde 200 OK a Whapi inmediatamente al recibir el webhook y procesa
los mensajes en segundo plano para evitar reintentos por timeout.
"""

import os
import re
import uuid
import logging
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv

from agent.brain import (
    agente_detenido,
    generar_respuesta,
    generar_resumen_para_silvana,
    generar_resumen_cliente,
    hay_info_nueva_cliente,
    detecto_informacion_faltante,
    detecto_cliente_yendo_en_persona,
    detecto_promesa_aviso_silvana,
    extraer_tiempo_llegada,
)
from agent.memory import (
    inicializar_db,
    guardar_mensaje,
    obtener_contexto_cliente,
    guardar_resumen_cliente,
    obtener_historial,
    obtener_ultimos_clientes,
    obtener_todos_resumenes,
    obtener_resumen_cliente,
    buscar_telefono_por_query,
    puede_notificar_silvana,
    registrar_notificacion_silvana,
)
from agent.providers import obtener_proveedor
from agent.providers.base import MensajeEntrante

load_dotenv()

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
log_level = logging.DEBUG if ENVIRONMENT == "development" else logging.INFO
logging.basicConfig(level=log_level)
logger = logging.getLogger("agentkit")

proveedor = obtener_proveedor()
PORT = int(os.getenv("PORT", 8000))

# Número de la dueña — recibe resúmenes de ventas y alertas de error
# Formato Argentina: 54 (país) + 9 (móvil) + 221 (área) + número
TELEFONO_DUENA = os.getenv("TELEFONO_DUENA", "5492215639673")

# Evita enviar la alerta de créditos agotados más de una vez por sesión
_notificacion_402_enviada = False


# ══════════════════════════════════════════════════════════════════════════════
# MODO ADMINISTRADOR — Solo para mensajes del número de Silvana
# ══════════════════════════════════════════════════════════════════════════════

# Palabras clave para detectar intención de cada consulta
_PALABRAS_CLIENTES = {
    "clientes", "quién escribió", "quien escribio", "quiénes escribieron",
    "quienes escribieron", "actividad reciente", "últimos", "ultimos",
    "quién me escribió", "quien me escribio", "conversaciones recientes",
    "clientes activos", "quién contactó", "quien contacto",
}
_PALABRAS_PEDIDOS = {
    "pedidos", "pendiente", "pendientes", "a entregar", "por entregar",
    "qué tengo que entregar", "que tengo que entregar", "entregas",
    "qué hay pendiente", "que hay pendiente", "qué falta entregar",
    "que falta entregar",
}
_PALABRAS_HISTORIAL = {
    "historial", "conversación de", "conversacion de", "qué dijo", "que dijo",
    "qué habló", "que hablo", "ver a", "información de", "informacion de",
}
_PALABRAS_ICS = {
    "evento", "anotá", "anota", "agenda", "agendá", "recordatorio",
    "reunión", "reunion", "cita", "compromiso", "calendario",
}


def _nombre_corto_resumen(resumen: str, telefono: str) -> str:
    """Extrae un identificador legible del resumen del cliente."""
    if not resumen:
        return telefono
    primera_linea = resumen.split("\n")[0].strip()
    if len(primera_linea) > 60:
        primera_linea = primera_linea[:60] + "..."
    return primera_linea or telefono


def _detectar_intencion(texto: str) -> str:
    """
    Detecta la intención del mensaje de Silvana.
    Retorna: 'clientes' | 'pedidos' | 'historial' | 'ics' | 'buscar_cliente' | 'natural'
    """
    texto_lower = texto.lower()

    if any(p in texto_lower for p in _PALABRAS_CLIENTES):
        return "clientes"

    if any(p in texto_lower for p in _PALABRAS_PEDIDOS):
        return "pedidos"

    # Evento de calendario: necesita fecha + hora o keyword de evento
    tiene_fecha = bool(re.search(r'\d{1,2}/\d{1,2}/\d{4}', texto))
    tiene_hora = bool(re.search(r'\d{2}:\d{2}', texto))
    tiene_keyword_ics = any(p in texto_lower for p in _PALABRAS_ICS)
    if tiene_fecha and (tiene_hora or tiene_keyword_ics):
        return "ics"

    if any(p in texto_lower for p in _PALABRAS_HISTORIAL):
        return "historial"

    return "natural"


def _generar_ics_contenido(nombre: str, fecha: str, hora: str, descripcion: str) -> str:
    """
    Genera contenido de archivo .ics válido según RFC 5545.
    fecha: DD/MM/YYYY  |  hora: HH:MM
    """
    dt_inicio = None
    for fmt in ("%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M"):
        try:
            dt_inicio = datetime.strptime(f"{fecha} {hora}", fmt)
            break
        except ValueError:
            continue

    if dt_inicio is None:
        raise ValueError(f"Formato de fecha/hora no reconocido: '{fecha} {hora}'")

    dt_fin = dt_inicio + timedelta(hours=1)
    uid = str(uuid.uuid4())
    now_utc = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    dtstart = dt_inicio.strftime("%Y%m%dT%H%M%S")
    dtend = dt_fin.strftime("%Y%m%dT%H%M%S")

    def _esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")

    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//Silvana Natura AgentKit//ES\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"DTSTAMP:{now_utc}\r\n"
        f"DTSTART:{dtstart}\r\n"
        f"DTEND:{dtend}\r\n"
        f"SUMMARY:{_esc(nombre)}\r\n"
        f"DESCRIPTION:{_esc(descripcion)}\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )


async def _obtener_clientes() -> str:
    """Lista los últimos 10 clientes con actividad reciente."""
    clientes = await obtener_ultimos_clientes(10)
    if not clientes:
        return "Todavía no hay clientes registrados."

    resumenes = {r["telefono"]: r["resumen"] for r in await obtener_todos_resumenes()}
    lineas = [f"📋 *Últimos {len(clientes)} clientes*\n"]
    for i, c in enumerate(clientes, 1):
        tel = c["telefono"]
        nombre = _nombre_corto_resumen(resumenes.get(tel, ""), tel)
        ultima = c["ultima_actividad"].strftime("%d/%m %H:%M") if c["ultima_actividad"] else "?"
        tel_limpio = tel.replace("@s.whatsapp.net", "").replace("@c.us", "")
        lineas.append(f"{i}. {nombre}\n   📞 {tel_limpio} — {ultima}")

    return "\n".join(lineas)


async def _obtener_historial_cliente(query: str) -> str:
    """Muestra el historial completo de un cliente buscado por nombre o número."""
    telefono = await buscar_telefono_por_query(query.strip())
    if not telefono:
        return f"No encontré ningún cliente con '{query.strip()}'."

    resumen = await obtener_resumen_cliente(telefono)
    historial = await obtener_historial(telefono, limite=40)
    tel_limpio = telefono.replace("@s.whatsapp.net", "").replace("@c.us", "")

    lineas = [f"📜 *Historial de {tel_limpio}*"]
    if resumen:
        lineas.append(f"\n🗂 *Perfil:* {resumen}\n")
    lineas.append("─" * 30)

    if not historial:
        lineas.append("(sin mensajes guardados)")
    else:
        for msg in historial:
            prefijo = "👤" if msg["role"] == "user" else "🤖"
            texto_truncado = msg["content"][:300] + ("..." if len(msg["content"]) > 300 else "")
            lineas.append(f"{prefijo} {texto_truncado}")

    return "\n".join(lineas)


async def _obtener_pedidos() -> str:
    """Lista pedidos pendientes detectados en los perfiles de clientes."""
    resumenes = await obtener_todos_resumenes()
    SENALES = ["pendiente", "pedido a natura", "por llegar", "a entregar", "esperando", "encargado"]

    pendientes = [
        r for r in resumenes
        if any(s in r["resumen"].lower() for s in SENALES)
    ]

    if not pendientes:
        return "No encontré pedidos pendientes en los perfiles de clientes en este momento."

    lineas = [f"📦 *Pedidos pendientes detectados ({len(pendientes)})*\n"]
    for p in pendientes:
        tel_limpio = p["telefono"].replace("@s.whatsapp.net", "").replace("@c.us", "")
        lineas.append(f"• 📞 {tel_limpio}\n  {p['resumen'][:200]}\n")

    return "\n".join(lineas)


async def _respuesta_admin_natural(pregunta: str) -> str:
    """
    Usa Claude para responder en lenguaje natural a cualquier pregunta de Silvana
    sobre el estado del negocio. Construye un resumen del contexto disponible y
    lo incorpora al system prompt antes de generar la respuesta.
    """
    from agent.brain import _llamar_claude

    # Recopilar contexto disponible del negocio
    clientes = await obtener_ultimos_clientes(10)
    todos_resumenes = await obtener_todos_resumenes()
    resumenes_idx = {r["telefono"]: r["resumen"] for r in todos_resumenes}

    SENALES_PEDIDO = ["pendiente", "pedido a natura", "por llegar", "a entregar", "esperando", "encargado"]
    pedidos_pendientes = [
        r for r in todos_resumenes
        if any(s in r["resumen"].lower() for s in SENALES_PEDIDO)
    ]

    # Construir resumen de contexto
    resumen_clientes = ""
    if clientes:
        lineas = []
        for c in clientes:
            tel = c["telefono"]
            tel_limpio = tel.replace("@s.whatsapp.net", "").replace("@c.us", "")
            nombre = _nombre_corto_resumen(resumenes_idx.get(tel, ""), tel_limpio)
            ultima = c["ultima_actividad"].strftime("%d/%m %H:%M") if c["ultima_actividad"] else "?"
            lineas.append(f"- {nombre} ({tel_limpio}) — última actividad: {ultima}")
        resumen_clientes = "\n".join(lineas)
    else:
        resumen_clientes = "Sin clientes registrados todavía."

    resumen_pedidos = ""
    if pedidos_pendientes:
        lineas = []
        for p in pedidos_pendientes:
            tel_limpio = p["telefono"].replace("@s.whatsapp.net", "").replace("@c.us", "")
            lineas.append(f"- {tel_limpio}: {p['resumen'][:150]}")
        resumen_pedidos = "\n".join(lineas)
    else:
        resumen_pedidos = "No hay pedidos pendientes detectados."

    system = (
        "Sos el asistente interno de Silvana, dueña de Ventas Natura.\n"
        "Respondés sus preguntas sobre el estado del negocio de forma conversacional, "
        "clara y concisa. Usás la información disponible del sistema para dar respuestas "
        "concretas y útiles. Si no tenés información suficiente para responder algo, "
        "decíselo directamente sin inventar datos.\n\n"
        f"## Clientes recientes ({len(clientes)} activos)\n{resumen_clientes}\n\n"
        f"## Pedidos pendientes ({len(pedidos_pendientes)} detectados)\n{resumen_pedidos}"
    )

    try:
        response = await _llamar_claude(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=system,
            messages=[{"role": "user", "content": pregunta}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        logger.error(f"[Admin] Error generando respuesta natural: {e}")
        return "No pude procesar tu consulta en este momento. Intentá de nuevo."


async def _crear_ics(texto: str) -> tuple[str, str | None]:
    """
    Genera un archivo .ics a partir de un mensaje en lenguaje natural.
    Formato esperado en el texto: [nombre] DD/MM/YYYY HH:MM [descripción]

    Returns: (mensaje_confirmacion, ruta_archivo_ics_o_None)
    """
    # Parsear: todo antes de DD/MM/YYYY es el nombre, luego fecha, hora, descripción
    patron = r'^(.+?)\s+(\d{1,2}/\d{1,2}/\d{4})\s+(\d{2}:\d{2})\s+(.+)$'
    match = re.match(patron, texto.strip())
    if not match:
        # Intentar sin descripción explícita (usar el nombre como descripción)
        patron2 = r'^(.+?)\s+(\d{1,2}/\d{1,2}/\d{4})\s+(\d{2}:\d{2})\s*$'
        match = re.match(patron2, texto.strip())
        if match:
            nombre = match.group(1).strip()
            fecha = match.group(2).strip()
            hora = match.group(3).strip()
            descripcion = nombre
        else:
            return (
                "Para crear el evento necesito el nombre, la fecha y la hora.\n"
                "Por ejemplo: *Entrega Laura 15/04/2026 14:30 llevar crema y perfume*",
                None,
            )
    else:
        nombre = match.group(1).strip()
        fecha = match.group(2).strip()
        hora = match.group(3).strip()
        descripcion = match.group(4).strip()

    # Limpiar palabras de relleno del nombre (ej: "anotame un evento", "agenda")
    for prefijo in ["anotame un evento", "anotá un evento", "agenda", "creame un evento",
                    "crear evento", "nuevo evento", "evento"]:
        if nombre.lower().startswith(prefijo):
            nombre = nombre[len(prefijo):].strip().lstrip(":- ").strip()
            break

    if not nombre:
        nombre = descripcion[:40]

    try:
        contenido = _generar_ics_contenido(nombre, fecha, hora, descripcion)
    except ValueError as e:
        return f"No pude interpretar la fecha o la hora: {e}", None

    nombre_archivo = f"evento_{uuid.uuid4().hex[:8]}.ics"
    ruta = os.path.join(tempfile.gettempdir(), nombre_archivo)
    try:
        with open(ruta, "w", encoding="utf-8") as f:
            f.write(contenido)
    except OSError as e:
        return f"Error al crear el archivo: {e}", None

    confirmacion = (
        f"📅 *Evento generado*\n"
        f"• Nombre: {nombre}\n"
        f"• Fecha: {fecha} {hora}\n"
        f"• Descripción: {descripcion}\n"
        f"• Duración: 1 hora\n\n"
        "Enviando archivo .ics..."
    )
    return confirmacion, ruta


async def procesar_modo_admin(texto: str) -> None:
    """
    Procesa mensajes de Silvana en modo administrador usando lenguaje natural.
    Detecta la intención del mensaje y responde con la información relevante
    del negocio de forma conversacional, sin necesidad de comandos especiales.
    """
    texto_stripped = texto.strip()
    intencion = _detectar_intencion(texto_stripped)

    respuesta_texto = None
    ruta_ics = None

    if intencion == "clientes":
        respuesta_texto = await _obtener_clientes()

    elif intencion == "pedidos":
        respuesta_texto = await _obtener_pedidos()

    elif intencion == "ics":
        respuesta_texto, ruta_ics = await _crear_ics(texto_stripped)

    elif intencion == "historial":
        # Extraer el nombre o número del cliente desde el texto
        respuesta_texto = await _obtener_historial_cliente(texto_stripped)

    else:
        # Intentar buscar un cliente por nombre o número mencionado
        telefono_encontrado = await buscar_telefono_por_query(texto_stripped)
        if telefono_encontrado:
            respuesta_texto = await _obtener_historial_cliente(texto_stripped)
        else:
            # Respuesta conversacional con Claude usando contexto del negocio
            respuesta_texto = await _respuesta_admin_natural(texto_stripped)

    # Enviar respuesta de texto
    if respuesta_texto:
        await proveedor.enviar_mensaje(TELEFONO_DUENA, respuesta_texto)

    # Si se generó un archivo .ics, enviarlo como documento
    if ruta_ics:
        nombre_archivo = os.path.basename(ruta_ics)
        exito_doc = await proveedor.enviar_documento(
            TELEFONO_DUENA,
            ruta_ics,
            nombre_archivo,
            caption="Evento de calendario — abrilo para agregarlo a tu app de calendario.",
        )
        try:
            os.remove(ruta_ics)
        except OSError:
            pass

        if not exito_doc:
            await proveedor.enviar_mensaje(
                TELEFONO_DUENA,
                "⚠️ No pude enviar el archivo .ics. "
                "Copiá los datos del evento de arriba para agregarlo manualmente a tu calendario.",
            )


# ══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicializa la base de datos al arrancar el servidor."""
    await inicializar_db()
    logger.info("Base de datos inicializada")
    logger.info(f"Servidor Silvana Natura corriendo en puerto {PORT}")
    logger.info(f"Proveedor: {proveedor.__class__.__name__}")
    logger.info(f"[Config] TELEFONO_DUENA en runtime: '{TELEFONO_DUENA}'")
    logger.info(
        f"[Config] WHAPI_TOKEN configurado: "
        f"{'SÍ (***' + os.getenv('WHAPI_TOKEN', '')[-4:] + ')' if os.getenv('WHAPI_TOKEN') else 'NO ❌'}"
    )
    yield


app = FastAPI(
    title="Silvana Natura — Agente WhatsApp",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/")
async def health_check():
    """Endpoint de salud para Railway/monitoreo."""
    return {"status": "ok", "agente": "Silvana Natura"}


@app.get("/test-silvana")
async def test_notificacion_silvana():
    """
    Endpoint de prueba manual: envía un mensaje de test a Silvana via Whapi.
    Usarlo para verificar que el token, el número y la integración funcionan.

    Llamar desde el navegador o con:
        curl https://TU-APP.up.railway.app/test-silvana
    """
    telefono_destino = TELEFONO_DUENA
    mensaje_prueba = (
        "🧪 TEST AGENTE — Mensaje de prueba\n"
        "Si recibís esto, el sistema de notificaciones está funcionando correctamente.\n"
        f"Número configurado: {telefono_destino}\n"
        f"Proveedor: {proveedor.__class__.__name__}"
    )

    logger.info(f"[Test] Iniciando prueba de envío a Silvana → '{telefono_destino}'")
    exito = await proveedor.enviar_mensaje(telefono_destino, mensaje_prueba)

    if exito:
        logger.info("[Test] Mensaje de prueba enviado correctamente a Silvana")
        return {
            "resultado": "OK",
            "mensaje": "Mensaje de prueba enviado correctamente",
            "telefono_destino": telefono_destino,
        }
    else:
        logger.error("[Test] FALLO al enviar mensaje de prueba — revisá los logs para ver el error Whapi")
        return {
            "resultado": "FALLO",
            "mensaje": "El mensaje no se pudo enviar. Revisá los logs del servidor para ver el error exacto de Whapi.",
            "telefono_destino": telefono_destino,
            "token_configurado": bool(os.getenv("WHAPI_TOKEN")),
        }


@app.get("/webhook")
async def webhook_verificacion(request: Request):
    """Verificación GET del webhook (no-op para Whapi, requerido por Meta)."""
    resultado = await proveedor.validar_webhook(request)
    if resultado is not None:
        return PlainTextResponse(str(resultado))
    return {"status": "ok"}


@app.post("/webhook")
async def webhook_handler(request: Request, background_tasks: BackgroundTasks):
    """
    Responde HTTP 200 OK a Whapi inmediatamente después de parsear el payload.
    El procesamiento real (Claude API, envío de respuesta) ocurre en segundo plano.
    Esto evita que Whapi reintente la entrega cuando Claude tarda o falla.
    """
    try:
        mensajes = await proveedor.parsear_webhook(request)
    except Exception as e:
        logger.error(f"Error parseando webhook: {e}")
        return {"status": "ok"}  # Siempre 200 para evitar reintentos de Whapi

    background_tasks.add_task(procesar_mensajes, mensajes)
    return {"status": "ok"}


async def procesar_mensajes(mensajes: list[MensajeEntrante]):
    """
    Procesa los mensajes entrantes de forma asíncrona en segundo plano.
    El webhook ya respondió 200 antes de que esta función se ejecute.
    """
    global _notificacion_402_enviada

    for msg in mensajes:
        # Ignorar mensajes propios (del agente) o sin texto
        if msg.es_propio or not msg.texto:
            continue

        # Detectar si el mensaje viene de Silvana (la dueña)
        numero_limpio = (
            msg.telefono
            .replace("+", "")
            .replace("@s.whatsapp.net", "")
            .replace("@c.us", "")
        )
        es_silvana = (numero_limpio == TELEFONO_DUENA or numero_limpio in TELEFONO_DUENA)

        if es_silvana:
            # Modo administrador: procesar comandos y consultas de Silvana
            logger.info(f"[Admin] Mensaje de Silvana: {msg.texto}")
            try:
                await procesar_modo_admin(msg.texto)
            except Exception as e:
                logger.error(f"[Admin] Error procesando comando de Silvana: {e}")
                await proveedor.enviar_mensaje(
                    TELEFONO_DUENA,
                    f"⚠️ Error al procesar el comando: {e}",
                )
            continue

        # Verificar si el agente fue detenido por créditos agotados (error 402)
        if agente_detenido():
            if not _notificacion_402_enviada:
                _notificacion_402_enviada = True
                alerta = (
                    "⚠️ ALERTA CRÍTICA — Créditos de API agotados (error 402)\n\n"
                    "El agente está DETENIDO y no puede responder a los clientes.\n\n"
                    "Para reactivarlo:\n"
                    "1. Ingresá a platform.anthropic.com\n"
                    "2. Settings → Billing → Add credits\n"
                    "3. Reiniciá el servidor en Railway"
                )
                logger.warning(f"Enviando alerta de créditos agotados a Silvana ({TELEFONO_DUENA})")
                await proveedor.enviar_mensaje(TELEFONO_DUENA, alerta)
            logger.warning(f"Agente detenido — mensaje de {msg.telefono} no procesado")
            return

        logger.info(f"Mensaje de {msg.telefono}: {msg.texto}")

        try:
            # Obtener contexto del cliente: perfil compacto + últimos 4 mensajes
            contexto = await obtener_contexto_cliente(msg.telefono)
            resumen_cliente = contexto["resumen"]
            mensajes_recientes = contexto["mensajes_recientes"]

            # Generar respuesta con Claude (historial reducido + perfil de cliente)
            respuesta = await generar_respuesta(msg.texto, mensajes_recientes, resumen_cliente)

            # Si durante la generación se agotaron los créditos, notificar a Silvana
            if agente_detenido() and not _notificacion_402_enviada:
                _notificacion_402_enviada = True
                alerta = (
                    "⚠️ ALERTA CRÍTICA — Créditos de API agotados (error 402)\n\n"
                    f"Ocurrió mientras respondía al cliente {msg.telefono}.\n"
                    "El agente está DETENIDO.\n\n"
                    "Para reactivarlo:\n"
                    "1. Ingresá a platform.anthropic.com\n"
                    "2. Settings → Billing → Add credits\n"
                    "3. Reiniciá el servidor en Railway"
                )
                logger.warning(f"Enviando alerta 402 a Silvana ({TELEFONO_DUENA})")
                await proveedor.enviar_mensaje(TELEFONO_DUENA, alerta)

            identificador_cliente = (
                resumen_cliente.split("\n")[0].strip()
                if resumen_cliente
                else msg.telefono
            )
            tel_limpio = (
                msg.telefono
                .replace("@s.whatsapp.net", "")
                .replace("@c.us", "")
            )

            # Guardar el intercambio en memoria
            await guardar_mensaje(msg.telefono, "user", msg.texto)
            await guardar_mensaje(msg.telefono, "assistant", respuesta)

            # Enviar respuesta al cliente
            await proveedor.enviar_mensaje(msg.telefono, respuesta)
            logger.info(f"Respuesta enviada a {msg.telefono}")

            # ── NOTIFICACIONES A SILVANA (consolidadas en UN solo mensaje) ────
            #
            # Se recopilan todos los fragmentos que deben notificarse y se envía
            # UN único mensaje. Cada tipo tiene su ventana de deduplicación para
            # evitar spam por mensajes consecutivos del mismo cliente.
            #
            # Ventanas: venta=10min | visita=30min | info_faltante=10min
            # Los avisos explícitos (el agente le prometió al cliente que avisaría)
            # siempre se incluyen sin ventana de dedup.
            partes_notif: list[str] = []

            # 1. Aviso explícito (el agente prometió avisar a Silvana)
            if detecto_promesa_aviso_silvana(respuesta):
                partes_notif.append(
                    "📱 AVISO SOLICITADO\n"
                    f"El cliente pidió comunicarse con Silvana.\n"
                    f"Consulta: {msg.texto}\n"
                    f"Respuesta enviada: {respuesta[:300]}{'...' if len(respuesta) > 300 else ''}"
                )
                logger.info("[Notif] Aviso explícito a Silvana detectado")

            # 2. Información faltante (el agente no pudo responder correctamente)
            if detecto_informacion_faltante(respuesta):
                if await puede_notificar_silvana(msg.telefono, "info_faltante", 10):
                    await registrar_notificacion_silvana(msg.telefono, "info_faltante")
                    partes_notif.append(
                        "⚠️ INFORMACIÓN FALTANTE\n"
                        f"Consulta: {msg.texto}\n"
                        f"El agente no encontró información suficiente. "
                        f"Revisá si hace falta agregar datos a /knowledge.\n"
                        f"Respuesta enviada: \"{respuesta[:200]}{'...' if len(respuesta) > 200 else ''}\""
                    )
                    logger.warning("[Notif] Info faltante detectada")
                else:
                    logger.info("[Notif] Info faltante ya notificada recientemente — omitido")

            # 3. Cliente en camino (visita física)
            if detecto_cliente_yendo_en_persona(msg.texto):
                if await puede_notificar_silvana(msg.telefono, "visita", 30):
                    await registrar_notificacion_silvana(msg.telefono, "visita")
                    tiempo = extraer_tiempo_llegada(msg.texto)
                    partes_notif.append(
                        f"🚨 CLIENTE EN CAMINO\n"
                        f"Mensaje: {msg.texto}\n"
                        f"Llega en: {tiempo}"
                    )
                    logger.warning("[Notif] Cliente en camino detectado")
                else:
                    logger.info("[Notif] Alerta de visita ya enviada recientemente — omitida")

            # Detectar señales de venta cerrada en la respuesta
            SENALES_VENTA = [
                "confirmado", "pedido confirmado", "tu pedido está confirmado",
                "listo", "quedó anotado", "lo anoto", "te lo llevo", "te lo llevamos",
                "entrega acordada", "acordamos la entrega", "te lo mando", "te lo mandamos",
                "te lo enviamos", "te lo envío", "lo separamos", "ya lo separo",
                "ya está reservado", "reservado", "tomamos tu pedido",
            ]
            hay_senal_venta = any(s in respuesta.lower() for s in SENALES_VENTA)

            # Actualizar perfil del cliente si hay info nueva o venta confirmada
            if hay_info_nueva_cliente(msg.texto) or hay_senal_venta:
                historial_para_perfil = mensajes_recientes + [
                    {"role": "user", "content": msg.texto},
                    {"role": "assistant", "content": respuesta},
                ]
                nuevo_resumen = await generar_resumen_cliente(
                    msg.telefono, resumen_cliente, historial_para_perfil
                )
                if nuevo_resumen and nuevo_resumen != resumen_cliente:
                    await guardar_resumen_cliente(msg.telefono, nuevo_resumen)

            # 4. Resumen de venta (con deduplicación de 10 minutos)
            if hay_senal_venta:
                if await puede_notificar_silvana(msg.telefono, "venta", 10):
                    await registrar_notificacion_silvana(msg.telefono, "venta")
                    historial_completo = mensajes_recientes + [
                        {"role": "user", "content": msg.texto},
                        {"role": "assistant", "content": respuesta},
                    ]
                    resumen_venta = await generar_resumen_para_silvana(
                        msg.telefono, historial_completo
                    )
                    if resumen_venta:
                        partes_notif.append(f"📦 PEDIDO CONFIRMADO\n{resumen_venta}")
                        logger.info("[Notif] Resumen de venta generado")
                else:
                    logger.info("[Notif] Resumen de venta ya enviado recientemente — omitido")

            # ── ENVÍO CONSOLIDADO ─────────────────────────────────────────────
            # Si hay al menos un fragmento, armar UN solo mensaje y enviarlo.
            if partes_notif:
                encabezado = (
                    f"🔔 *ACTIVIDAD* — {identificador_cliente}\n"
                    f"📞 {tel_limpio}\n"
                    f"{'─' * 32}\n"
                )
                mensaje_silvana = encabezado + "\n\n".join(partes_notif)
                logger.info(
                    f"[Notif] Enviando notificación consolidada a Silvana "
                    f"({len(partes_notif)} sección/es)"
                )
                await proveedor.enviar_mensaje(TELEFONO_DUENA, mensaje_silvana)

            # ── DESCUENTO DE STOCK ────────────────────────────────────────────
            # Si se confirmó una venta, intentar identificar el producto y
            # restar 1 unidad en Google Sheets. Se ejecuta después de notificar a Silvana.
            if hay_senal_venta:
                try:
                    from agent.sheets import extraer_producto_de_venta, descontar_unidad
                    nombre_producto = await extraer_producto_de_venta(msg.texto, respuesta)
                    if nombre_producto:
                        await descontar_unidad(nombre_producto)
                        logger.info(f"[Stock] Unidad descontada: '{nombre_producto}'")
                    else:
                        logger.debug(
                            "[Stock] Venta detectada pero no se identificó el producto — "
                            "revisar manualmente en el sheet"
                        )
                except Exception as e:
                    logger.error(f"[Stock] Error al descontar stock: {e}")

        except Exception as e:
            logger.error(f"Error procesando mensaje de {msg.telefono}: {e}")
