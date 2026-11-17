"""
historial_store.py — PingMonitor

Persistencia en SQLite del historial de caidas/recuperaciones.

Los eventos ocurridos mientras un host estaba silenciado se guardan con
muted=1: no cuentan para el % de disponibilidad ni para el Top de
desconexiones, pero la informacion no se pierde — siguen visibles en el
historial individual de esa IP.

Todo el acceso a este modulo debe hacerse desde el hilo principal (GUI).
Las señales de PingWorker se conectan a metodos de VentanaPrincipal (un
QObject que vive en el hilo principal), asi que Qt encola automaticamente
la ejecucion ahi — evita tener que sincronizar manualmente el acceso a
SQLite desde el hilo de escaneo.
"""

import sqlite3
import time
from pathlib import Path


class HistorialStore:
    def __init__(self, ruta_db: Path):
        self.ruta_db = ruta_db
        self._conn = sqlite3.connect(str(ruta_db))
        self._crear_tabla()

    def _crear_tabla(self):
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS eventos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip TEXT NOT NULL,
                timestamp_down REAL NOT NULL,
                timestamp_up REAL,
                duracion_seg REAL,
                muted INTEGER NOT NULL DEFAULT 0
            )
        """)
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_eventos_ip ON eventos(ip)")
        self._conn.commit()

    def registrar_caida(self, ip: str, timestamp: float, muted: bool):
        self._conn.execute(
            "INSERT INTO eventos (ip, timestamp_down, muted) VALUES (?, ?, ?)",
            (ip, timestamp, int(muted)),
        )
        self._conn.commit()

    def registrar_recuperacion(self, ip: str, timestamp: float, duracion: float):
        """Cierra el evento abierto mas reciente de esa IP (timestamp_up IS NULL)."""
        self._conn.execute(
            """
            UPDATE eventos
            SET timestamp_up = ?, duracion_seg = ?
            WHERE id = (
                SELECT id FROM eventos
                WHERE ip = ? AND timestamp_up IS NULL
                ORDER BY timestamp_down DESC LIMIT 1
            )
            """,
            (timestamp, duracion, ip),
        )
        self._conn.commit()

    def obtener_evento_abierto(self, ip: str):
        """Devuelve el evento sin cerrar mas reciente de esa IP (timestamp_up IS NULL), o None."""
        cursor = self._conn.execute(
            """
            SELECT id, timestamp_down FROM eventos
            WHERE ip = ? AND timestamp_up IS NULL
            ORDER BY timestamp_down DESC LIMIT 1
            """,
            (ip,),
        )
        fila = cursor.fetchone()
        return {"id": fila[0], "timestamp_down": fila[1]} if fila else None

    def obtener_historial(self, ip: str, limite: int = 100):
        cursor = self._conn.execute(
            """
            SELECT timestamp_down, timestamp_up, duracion_seg, muted
            FROM eventos WHERE ip = ?
            ORDER BY timestamp_down DESC LIMIT ?
            """,
            (ip, limite),
        )
        columnas = ("timestamp_down", "timestamp_up", "duracion_seg", "muted")
        return [dict(zip(columnas, fila)) for fila in cursor.fetchall()]

    def calcular_disponibilidad(self, ip: str) -> float:
        """
        % de tiempo disponible desde el primer evento registrado hasta ahora,
        excluyendo eventos marcados como muted. Si no hay eventos, 100%.
        """
        eventos = self.obtener_historial(ip, limite=1000)
        if not eventos:
            return 100.0

        ahora = time.time()
        primer_evento = min(e["timestamp_down"] for e in eventos)
        tiempo_total = max(1.0, ahora - primer_evento)

        tiempo_caido = sum(
            (e["timestamp_up"] or ahora) - e["timestamp_down"]
            for e in eventos if not e["muted"]
        )
        return max(0.0, min(100.0, 100 * (1 - tiempo_caido / tiempo_total)))

    def ultimas_desconexiones(self, limite: int = 15):
        """Las caidas NO silenciadas mas recientes, sin importar la IP (orden cronologico)."""
        cursor = self._conn.execute(
            """
            SELECT ip, timestamp_down
            FROM eventos
            WHERE muted = 0
            ORDER BY timestamp_down DESC
            LIMIT ?
            """,
            (limite,),
        )
        return cursor.fetchall()

    def top_desconexiones(self, ventana_seg: float, limite: int = 5):
        """Cuenta caidas NO silenciadas por IP dentro de la ventana de tiempo dada."""
        desde = time.time() - ventana_seg
        cursor = self._conn.execute(
            """
            SELECT ip, COUNT(*) as total
            FROM eventos
            WHERE timestamp_down >= ? AND muted = 0
            GROUP BY ip
            ORDER BY total DESC
            LIMIT ?
            """,
            (desde, limite),
        )
        return cursor.fetchall()

    def cerrar(self):
        self._conn.close()
