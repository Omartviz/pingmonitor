"""
ping_engine.py — PingMonitor

Motor de ping ICMP nativo de Windows via ctypes (iphlpapi.dll / IcmpSendEcho).
No requiere ejecutar la app como administrador, a diferencia de librerias
basadas en sockets raw (ping3, icmplib).

Logica de confirmacion (evita falsos positivos por un solo paquete perdido):
    1er ping fallido            -> estado CONFIRMANDO_CAIDA (parpadeo, no se registra)
    2 fallos consecutivos mas   -> estado OFFLINE (caida real, se registra y dispara evento)

Deteccion de perdida de paquetes:
    Se mantiene una ventana movil de los ultimos 10 resultados por host.
    Si >= 20% de esos pings fallaron (pero el host sigue respondiendo ahora),
    se marca como PERDIDA_PAQUETES en vez de ONLINE/LATENCIA_ALTA.
"""

import ctypes
import socket
import struct
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from PySide6.QtCore import QThread, Signal, QMutex, QMutexLocker


# ---------------------------------------------------------------------------
# Configuracion / umbrales (ajustables)
# ---------------------------------------------------------------------------

UMBRAL_LATENCIA_ALTA_MS = 100
UMBRAL_PERDIDA_PAQUETES = 0.20          # 20% de fallos en la ventana reciente
TAMANO_VENTANA_PERDIDA = 10             # ultimos N resultados considerados
CONFIRMACIONES_PARA_CAIDA = 3           # 1er fallo + 2 confirmaciones = 3 en total


class EstadoHost(Enum):
    ONLINE = "online"
    LATENCIA_ALTA = "latencia_alta"
    PERDIDA_PAQUETES = "perdida_paquetes"
    CONFIRMANDO_CAIDA = "confirmando_caida"   # parpadeo, aun no es una caida oficial
    OFFLINE = "offline"                        # caida confirmada (3 fallos seguidos)
    SILENCIADO = "silenciado"


# ---------------------------------------------------------------------------
# Wrapper ICMP nativo (ctypes -> iphlpapi.dll)
# ---------------------------------------------------------------------------

class _IPOptionInformation(ctypes.Structure):
    _fields_ = [
        ("Ttl", ctypes.c_ubyte),
        ("Tos", ctypes.c_ubyte),
        ("Flags", ctypes.c_ubyte),
        ("OptionsSize", ctypes.c_ubyte),
        ("OptionsData", ctypes.c_char_p),
    ]


class _IcmpEchoReply(ctypes.Structure):
    _fields_ = [
        ("Address", ctypes.c_ulong),
        ("Status", ctypes.c_ulong),
        ("RoundTripTime", ctypes.c_ulong),
        ("DataSize", ctypes.c_ushort),
        ("Reserved", ctypes.c_ushort),
        ("Data", ctypes.c_void_p),
        ("Options", _IPOptionInformation),
    ]


_REQUEST_DATA = b"PingMonitor"
_REPLY_SIZE = ctypes.sizeof(_IcmpEchoReply) + len(_REQUEST_DATA) + 8
_IP_STATUS_SUCCESS = 0
_INVALID_HANDLE_VALUE = 0xFFFFFFFFFFFFFFFF

# IMPORTANTE: se declaran argtypes/restype explicitamente.
# Sin esto, ctypes asume enteros de 32 bits por default y trunca el HANDLE
# de 64 bits que devuelve IcmpCreateFile en Windows moderno, lo que corrompe
# el puntero y provoca "access violation" al usarlo en IcmpSendEcho.
from ctypes import wintypes  # noqa: E402

_iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)

_iphlpapi.IcmpCreateFile.restype = wintypes.HANDLE
_iphlpapi.IcmpCreateFile.argtypes = []

_iphlpapi.IcmpCloseHandle.restype = wintypes.BOOL
_iphlpapi.IcmpCloseHandle.argtypes = [wintypes.HANDLE]

_iphlpapi.IcmpSendEcho.restype = wintypes.DWORD
_iphlpapi.IcmpSendEcho.argtypes = [
    wintypes.HANDLE,   # IcmpHandle
    wintypes.DWORD,    # DestinationAddress (IPAddr)
    wintypes.LPVOID,   # RequestData
    wintypes.WORD,     # RequestSize
    wintypes.LPVOID,   # RequestOptions (NULL en nuestro caso)
    wintypes.LPVOID,   # ReplyBuffer
    wintypes.DWORD,    # ReplySize
    wintypes.DWORD,    # Timeout
]


def _ip_a_entero(ip: str) -> int:
    """Convierte '192.0.2.10' al entero que espera IcmpSendEcho (orden de red)."""
    return struct.unpack("<L", socket.inet_aton(ip))[0]


class IcmpPinger:
    """
    Wrapper reutilizable sobre un unico handle ICMP.
    Abrir/cerrar el handle por cada ping es mas lento; aqui se abre una vez
    y se reutiliza durante toda la vida del hilo de escaneo.
    """

    def __init__(self):
        self._handle = _iphlpapi.IcmpCreateFile()
        if not self._handle or self._handle in (0, _INVALID_HANDLE_VALUE):
            raise OSError(
                "No se pudo crear el handle ICMP (IcmpCreateFile). "
                "Esta libreria solo funciona en Windows."
            )

    def ping(self, ip: str, timeout_ms: int = 800) -> tuple[bool, Optional[int]]:
        """Envia un unico ICMP Echo Request. Devuelve (exito, latencia_ms)."""
        destino = _ip_a_entero(ip)
        buffer_respuesta = ctypes.create_string_buffer(_REPLY_SIZE)

        resultado = _iphlpapi.IcmpSendEcho(
            self._handle,
            destino,
            _REQUEST_DATA,
            len(_REQUEST_DATA),
            None,
            buffer_respuesta,
            _REPLY_SIZE,
            timeout_ms,
        )

        if resultado == 0:
            return False, None

        respuesta = ctypes.cast(buffer_respuesta, ctypes.POINTER(_IcmpEchoReply)).contents
        if respuesta.Status == _IP_STATUS_SUCCESS:
            return True, int(respuesta.RoundTripTime)
        return False, None

    def cerrar(self):
        if getattr(self, "_handle", None):
            _iphlpapi.IcmpCloseHandle(self._handle)
            self._handle = None

    def __del__(self):
        try:
            self.cerrar()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Modelo de host
# ---------------------------------------------------------------------------

@dataclass
class Host:
    ip: str
    nombre: str
    categoria: str = ""
    switch: str = ""
    puerto: str = ""
    ubicacion: str = ""
    notas: str = ""
    silenciado: bool = False
    hostname: str = ""          # necesario para autenticacion con cuenta local (NTLM)
    grupo_credencial: str = ""  # "1"/"2"/"3" -> a que juego de contraseñas pertenece
    mac: str = ""                # para Wake-on-LAN

    # Estado en vivo (no viene del CSV, lo gestiona el motor)
    estado: EstadoHost = field(default=EstadoHost.ONLINE, init=False)
    latencia_ms: Optional[int] = field(default=None, init=False)
    fallos_consecutivos: int = field(default=0, init=False)
    ultima_caida: Optional[float] = field(default=None, init=False)     # epoch
    conectado_desde: float = field(default_factory=time.time, init=False)
    historial_resultados: deque = field(
        default_factory=lambda: deque(maxlen=TAMANO_VENTANA_PERDIDA), init=False
    )

    def __post_init__(self):
        if self.silenciado:
            self.estado = EstadoHost.SILENCIADO


# ---------------------------------------------------------------------------
# Hilo de escaneo continuo
# ---------------------------------------------------------------------------

class PingWorker(QThread):
    """
    Corre en un hilo separado para no bloquear la interfaz de PySide6.
    Escanea todos los hosts en cada ciclo y emite señales cuando:
      - cambia el estado visual de un host (para repintar su LED)
      - se confirma una caida real (3 fallos seguidos)
      - se confirma una recuperacion real (vuelve a responder tras estar OFFLINE)
    """

    host_actualizado = Signal(str, object)                      # ip, EstadoHost
    caida_confirmada = Signal(str, float, bool)                  # ip, timestamp_epoch, silenciado
    recuperacion_confirmada = Signal(str, float, float, bool)    # ip, timestamp_epoch, duracion_seg, silenciado

    def __init__(self, hosts: dict[str, Host], intervalo_seg: float = 3.0,
                 timeout_ms: int = 800, parent=None):
        super().__init__(parent)
        self.hosts = hosts
        self.intervalo_seg = intervalo_seg
        self.timeout_ms = timeout_ms
        self._corriendo = True
        self._mutex = QMutex()

    def detener(self):
        with QMutexLocker(self._mutex):
            self._corriendo = False

    def _sigue_corriendo(self) -> bool:
        with QMutexLocker(self._mutex):
            return self._corriendo

    def run(self):
        pinger = IcmpPinger()
        try:
            while self._sigue_corriendo():
                inicio_ciclo = time.time()

                for host in list(self.hosts.values()):
                    if not self._sigue_corriendo():
                        break

                    exito, latencia = pinger.ping(host.ip, self.timeout_ms)
                    self._procesar_resultado(host, exito, latencia)

                # Duerme solo lo que falte del intervalo, sin acumular retraso
                transcurrido = time.time() - inicio_ciclo
                espera_ms = max(0, int((self.intervalo_seg - transcurrido) * 1000))
                self.msleep(espera_ms)
        finally:
            pinger.cerrar()

    def _porcentaje_perdida(self, host: Host) -> float:
        if not host.historial_resultados:
            return 0.0
        fallos = sum(1 for r in host.historial_resultados if not r)
        return fallos / len(host.historial_resultados)

    def _procesar_resultado(self, host: Host, exito: bool, latencia_ms: Optional[int]):
        host.historial_resultados.append(exito)

        if exito:
            host.latencia_ms = latencia_ms

            if host.fallos_consecutivos > 0:
                # Se calcula ANTES de resetear, para saber si la caida ya
                # habia sido confirmada (independiente de si esta silenciado)
                estaba_caido_confirmado = host.fallos_consecutivos >= CONFIRMACIONES_PARA_CAIDA
                host.fallos_consecutivos = 0

                if estaba_caido_confirmado:
                    ahora = time.time()
                    duracion = ahora - host.ultima_caida if host.ultima_caida else 0.0
                    host.conectado_desde = ahora
                    self.recuperacion_confirmada.emit(host.ip, ahora, duracion, host.silenciado)

            perdida = self._porcentaje_perdida(host)
            if perdida >= UMBRAL_PERDIDA_PAQUETES:
                estado_real = EstadoHost.PERDIDA_PAQUETES
            elif latencia_ms is not None and latencia_ms > UMBRAL_LATENCIA_ALTA_MS:
                estado_real = EstadoHost.LATENCIA_ALTA
            else:
                estado_real = EstadoHost.ONLINE

        else:
            host.fallos_consecutivos += 1
            host.latencia_ms = None

            if host.fallos_consecutivos >= CONFIRMACIONES_PARA_CAIDA:
                if host.fallos_consecutivos == CONFIRMACIONES_PARA_CAIDA:
                    # Se dispara solo una vez, justo al cruzar el umbral
                    host.ultima_caida = time.time()
                    self.caida_confirmada.emit(host.ip, host.ultima_caida, host.silenciado)
                estado_real = EstadoHost.OFFLINE
            else:
                estado_real = EstadoHost.CONFIRMANDO_CAIDA

        # El estado VISUAL se fuerza a SILENCIADO si aplica (sin parpadeo,
        # sin importar el resultado real), pero el registro en SQLite ya
        # se disparo arriba con el flag muted correcto.
        host.estado = EstadoHost.SILENCIADO if host.silenciado else estado_real
        self.host_actualizado.emit(host.ip, host.estado)
