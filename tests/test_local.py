# tests/test_local.py — Simulador de chat en terminal
# Generado por AgentKit

"""
Probá tu agente Silvana Natura sin necesitar WhatsApp.
Simula una conversación completa en la terminal.
"""

import asyncio
import sys
import os

# Agregar el directorio raíz al path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.brain import generar_respuesta
from agent.memory import inicializar_db, guardar_mensaje, obtener_historial, limpiar_historial

TELEFONO_TEST = "test-local-001"


async def main():
    """Loop principal del chat de prueba."""
    await inicializar_db()

    print()
    print("=" * 55)
    print("   Silvana Natura — Test Local")
    print("=" * 55)
    print()
    print("  Escribí mensajes como si fueras una cliente.")
    print("  Comandos especiales:")
    print("    'limpiar'  — borra el historial")
    print("    'salir'    — termina el test")
    print()
    print("-" * 55)
    print()

    while True:
        try:
            mensaje = input("Vos: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\nTest finalizado.")
            break

        if not mensaje:
            continue

        if mensaje.lower() == "salir":
            print("\nTest finalizado.")
            break

        if mensaje.lower() == "limpiar":
            await limpiar_historial(TELEFONO_TEST)
            print("[Historial borrado]\n")
            continue

        # Obtener historial ANTES de guardar (brain.py agrega el mensaje actual)
        historial = await obtener_historial(TELEFONO_TEST)

        # Generar respuesta
        print("\nSilvana Natura: ", end="", flush=True)
        respuesta = await generar_respuesta(mensaje, historial)
        print(respuesta)
        print()

        # Si hay resumen para la dueña, mostrarlo destacado
        if "---RESUMEN PARA SILVANA---" in respuesta:
            print("📋 [MENSAJE QUE LLEGARÍA A SILVANA (la dueña)]")
            print("-" * 45)
            inicio = respuesta.find("---RESUMEN PARA SILVANA---")
            fin = respuesta.find("---FIN RESUMEN---")
            if fin != -1:
                print(respuesta[inicio:fin + len("---FIN RESUMEN---")])
            print("-" * 45)
            print()

        # Guardar mensajes
        await guardar_mensaje(TELEFONO_TEST, "user", mensaje)
        await guardar_mensaje(TELEFONO_TEST, "assistant", respuesta)


if __name__ == "__main__":
    asyncio.run(main())
