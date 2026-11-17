"""
telegram_alertas.py — PingMonitor

Envia una alerta a un chat de Telegram cuando se detecta una "tormenta"
de desconexiones (mas de N caidas reales en una ventana corta de tiempo)
— util para enterarte aunque no estes viendo la pantalla en ese momento.

Configuracion en telegram_config.json, texto plano editable junto al
resto de los archivos del proyecto:
    {
        "token": "123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "chat_id": "987654321",
        "habilitado": true
    }

No es tan sensible como las contraseñas de administrador local — el peor
caso de una fuga es que alguien mande mensajes por ese bot, no acceso a
ninguna PC — asi que no hace falta cifrarlo con DPAPI como a las
credenciales; texto plano editable es consistente con como ya tratamos
inventario.csv.

Solo usa urllib (libreria estandar), sin dependencias nuevas.
"""

import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional


def crear_config_si_falta(ruta: Path):
    """Deja una plantilla deshabilitada la primera vez, para que sea facil de llenar."""
    if ruta.exists():
        return
    plantilla = {"token": "", "chat_id": "", "habilitado": False}
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(plantilla, f, indent=2, ensure_ascii=False)


def cargar_config(ruta: Path) -> Optional[dict]:
    """Devuelve la config solo si esta habilitada y completa; None en cualquier otro caso."""
    if not ruta.exists():
        return None
    try:
        with open(ruta, encoding="utf-8") as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    if not config.get("habilitado", False):
        return None
    if not config.get("token") or not config.get("chat_id"):
        return None
    return config


def enviar_mensaje(token: str, chat_id: str, texto: str, timeout: int = 10) -> bool:
    """Devuelve True si Telegram confirmo la entrega. Pensado para correr en un hilo aparte."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    datos = urllib.parse.urlencode({"chat_id": chat_id, "text": texto}).encode("utf-8")

    try:
        with urllib.request.urlopen(url, data=datos, timeout=timeout) as respuesta:
            return respuesta.status == 200
    except Exception as e:
        print(f"No se pudo enviar la alerta de Telegram: {e}")
        return False
