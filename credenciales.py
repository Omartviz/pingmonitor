"""
credenciales.py — PingMonitor

Guarda las contraseñas de administrador local (3 grupos de credenciales)
cifradas con DPAPI de Windows — CryptProtectData/CryptUnprotectData via
ctypes, la misma tecnologia que usa Windows para contraseñas de Wi-Fi
guardadas o el Administrador de credenciales. Cero dependencias externas,
consistente con el resto del proyecto (ping_engine.py ya usa el mismo
patron de ctypes -> DLL nativa).

Modo CRYPTPROTECT_LOCAL_MACHINE ("de equipo", no "de usuario"): el archivo
cifrado se puede desencriptar desde CUALQUIER cuenta de Windows que inicie
sesion en esta PC (la tuya o la de tu compañero), pero es ilegible si se
copia a otra maquina — la llave vive en el equipo, no en la cuenta.

Esto es exclusivamente para el "modo emergencia": cuando el pass-through
de dominio (Kerberos) falla y hace falta autenticar con la cuenta local
Administrador de una PC especifica.
"""

import ctypes
import ctypes.wintypes
import json
from pathlib import Path
from typing import Optional

_CRYPTPROTECT_LOCAL_MACHINE = 0x4


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _crear_blob(data: bytes):
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = _DATA_BLOB()
    blob.cbData = len(data)
    blob.pbData = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))
    return blob, buffer  # el buffer debe seguir vivo mientras se use el blob


def _proteger(data: bytes) -> bytes:
    entrada, _buffer = _crear_blob(data)
    salida = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(entrada), None, None, None, None,
        _CRYPTPROTECT_LOCAL_MACHINE, ctypes.byref(salida),
    )
    if not ok:
        raise OSError("CryptProtectData fallo — no se pudo cifrar")
    try:
        return ctypes.string_at(salida.pbData, salida.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(salida.pbData)


def _desproteger(data: bytes) -> bytes:
    entrada, _buffer = _crear_blob(data)
    salida = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(entrada), None, None, None, None,
        _CRYPTPROTECT_LOCAL_MACHINE, ctypes.byref(salida),
    )
    if not ok:
        raise OSError("CryptUnprotectData fallo — no se pudo descifrar")
    try:
        return ctypes.string_at(salida.pbData, salida.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(salida.pbData)


class AlmacenCredenciales:
    """Persiste {'1': 'pass1', '2': 'pass2', '3': 'pass3'} cifrado en disco."""

    def __init__(self, ruta: Path):
        self.ruta = ruta

    def existe(self) -> bool:
        return self.ruta.exists()

    def guardar(self, credenciales: dict):
        data = json.dumps(credenciales).encode("utf-8")
        cifrado = _proteger(data)
        self.ruta.write_bytes(cifrado)

    def cargar(self) -> dict:
        if not self.ruta.exists():
            return {}
        cifrado = self.ruta.read_bytes()
        data = _desproteger(cifrado)
        return json.loads(data.decode("utf-8"))

    def obtener_password(self, grupo: str) -> Optional[str]:
        if not grupo:
            return None
        return self.cargar().get(str(grupo).strip())
