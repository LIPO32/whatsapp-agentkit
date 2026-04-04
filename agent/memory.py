# agent/memory.py — Memoria de conversaciones con SQLite
# Generado por AgentKit

"""
Sistema de memoria del agente. Guarda el historial de conversaciones
por número de teléfono usando SQLite (local) o PostgreSQL (producción).
"""

import os
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Text, DateTime, select, Integer
from dotenv import load_dotenv

load_dotenv()

# Configuración de base de datos
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./agentkit.db")

# Si es PostgreSQL en producción, ajustar el esquema de URL
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Mensaje(Base):
    """Modelo de mensaje en la base de datos."""
    __tablename__ = "mensajes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    role: Mapped[str] = mapped_column(String(20))   # "user" o "assistant"
    content: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ResumenCliente(Base):
    """Perfil compacto del cliente para memoria persistente entre sesiones."""
    __tablename__ = "resumenes_clientes"

    telefono: Mapped[str] = mapped_column(String(50), primary_key=True)
    resumen: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class NotificacionSilvana(Base):
    """
    Registro de notificaciones enviadas a Silvana por cliente y tipo.
    Permite evitar duplicados dentro de una ventana de tiempo configurable.
    Tipos: "venta" (10 min) | "visita" (30 min) | "info_faltante" (10 min)
    """
    __tablename__ = "notificaciones_silvana"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono_cliente: Mapped[str] = mapped_column(String(50), index=True)
    tipo: Mapped[str] = mapped_column(String(30))
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


async def inicializar_db():
    """Crea las tablas si no existen."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def guardar_mensaje(telefono: str, role: str, content: str):
    """Guarda un mensaje en el historial de conversación."""
    async with async_session() as session:
        mensaje = Mensaje(
            telefono=telefono,
            role=role,
            content=content,
            timestamp=datetime.utcnow()
        )
        session.add(mensaje)
        await session.commit()


async def obtener_historial(telefono: str, limite: int = 20) -> list[dict]:
    """
    Recupera los últimos N mensajes de una conversación.

    Args:
        telefono: Número de teléfono del cliente
        limite: Máximo de mensajes a recuperar (default: 20)

    Returns:
        Lista de diccionarios con role y content
    """
    async with async_session() as session:
        query = (
            select(Mensaje)
            .where(Mensaje.telefono == telefono)
            .order_by(Mensaje.timestamp.desc())
            .limit(limite)
        )
        result = await session.execute(query)
        mensajes = result.scalars().all()

        # Invertir para orden cronológico (los más recientes están primero en la query)
        mensajes.reverse()

        return [
            {"role": msg.role, "content": msg.content}
            for msg in mensajes
        ]


async def limpiar_historial(telefono: str):
    """Borra todo el historial de una conversación."""
    async with async_session() as session:
        query = select(Mensaje).where(Mensaje.telefono == telefono)
        result = await session.execute(query)
        mensajes = result.scalars().all()
        for msg in mensajes:
            await session.delete(msg)
        await session.commit()


async def obtener_resumen_cliente(telefono: str) -> str | None:
    """Recupera el perfil compacto del cliente, o None si no existe."""
    async with async_session() as session:
        resultado = await session.get(ResumenCliente, telefono)
        return resultado.resumen if resultado else None


async def guardar_resumen_cliente(telefono: str, resumen: str):
    """Guarda o actualiza el perfil compacto del cliente."""
    async with async_session() as session:
        existente = await session.get(ResumenCliente, telefono)
        if existente:
            existente.resumen = resumen
            existente.updated_at = datetime.utcnow()
        else:
            session.add(ResumenCliente(
                telefono=telefono,
                resumen=resumen,
                updated_at=datetime.utcnow(),
            ))
        await session.commit()


async def obtener_ultimos_clientes(limite: int = 10) -> list[dict]:
    """Retorna los últimos N clientes distintos ordenados por actividad reciente."""
    from sqlalchemy import func, desc as sql_desc
    async with async_session() as session:
        subq = (
            select(
                Mensaje.telefono,
                func.max(Mensaje.timestamp).label("ultima_actividad"),
            )
            .group_by(Mensaje.telefono)
            .order_by(sql_desc(func.max(Mensaje.timestamp)))
            .limit(limite)
            .subquery()
        )
        result = await session.execute(select(subq))
        rows = result.all()
        return [
            {"telefono": row.telefono, "ultima_actividad": row.ultima_actividad}
            for row in rows
        ]


async def obtener_todos_resumenes() -> list[dict]:
    """Retorna todos los perfiles de clientes guardados, ordenados por actualización."""
    async with async_session() as session:
        result = await session.execute(
            select(ResumenCliente).order_by(ResumenCliente.updated_at.desc())
        )
        rows = result.scalars().all()
        return [
            {"telefono": r.telefono, "resumen": r.resumen, "updated_at": r.updated_at}
            for r in rows
        ]


async def puede_notificar_silvana(telefono: str, tipo: str, ventana_minutos: int) -> bool:
    """
    Retorna True si se PUEDE enviar la notificación (no hay registro reciente del mismo tipo).
    Retorna False si ya se envió la misma notificación para este cliente en la ventana dada.

    Args:
        telefono: Número de teléfono del cliente que generó la notificación
        tipo: Tipo de notificación ("venta", "visita", "info_faltante")
        ventana_minutos: Cuántos minutos hacia atrás considerar para el bloqueo
    """
    desde = datetime.utcnow() - timedelta(minutes=ventana_minutos)
    async with async_session() as session:
        result = await session.execute(
            select(NotificacionSilvana)
            .where(
                NotificacionSilvana.telefono_cliente == telefono,
                NotificacionSilvana.tipo == tipo,
                NotificacionSilvana.timestamp >= desde,
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is None  # None = no hay reciente = puede notificar


async def registrar_notificacion_silvana(telefono: str, tipo: str):
    """Registra que se envió (o se va a enviar) una notificación de este tipo para este cliente."""
    async with async_session() as session:
        session.add(NotificacionSilvana(
            telefono_cliente=telefono,
            tipo=tipo,
            timestamp=datetime.utcnow(),
        ))
        await session.commit()


async def buscar_telefono_por_query(query: str) -> str | None:
    """
    Encuentra un número de teléfono buscando por número parcial o nombre en el resumen.
    Retorna el telefono exacto tal como está en la base de datos, o None.
    """
    query_limpio = query.strip().replace(" ", "").replace("-", "").replace("+", "")

    # Si parece un número, buscar por sufijo de teléfono en la tabla de mensajes
    if query_limpio.isdigit() and len(query_limpio) >= 6:
        async with async_session() as session:
            result = await session.execute(
                select(Mensaje.telefono)
                .where(Mensaje.telefono.contains(query_limpio))
                .limit(1)
            )
            return result.scalar_one_or_none()

    # Si es texto, buscar por nombre en los resumenes
    async with async_session() as session:
        result = await session.execute(select(ResumenCliente))
        rows = result.scalars().all()
        query_lower = query.lower()
        for r in rows:
            if query_lower in r.resumen.lower():
                return r.telefono

    return None


async def obtener_contexto_cliente(telefono: str) -> dict:
    """
    Retorna el perfil compacto del cliente más los últimos 4 mensajes.
    Reemplaza al historial completo para reducir el costo en tokens por request.

    Returns:
        {
            "resumen": str | None,       # Perfil persistente del cliente
            "mensajes_recientes": list   # Últimos 4 mensajes de la conversación
        }
    """
    resumen = await obtener_resumen_cliente(telefono)
    mensajes_recientes = await obtener_historial(telefono, limite=4)
    return {
        "resumen": resumen,
        "mensajes_recientes": mensajes_recientes,
    }
