"""
conexion_segura.py — PingMonitor

Helpers del "modo emergencia" que manejan la contraseña de administrador
local SIN pasarla nunca como argumento de linea de comandos.

Por que importa: los argumentos de un proceso son visibles para otros
usuarios/administradores del equipo (Administrador de tareas, WMI
Win32_Process.CommandLine) y, si PowerShell Script Block Logging esta
activo, el texto del comando puede quedar guardado en el Visor de eventos
(evento 4104). La version anterior armaba `net use ... CONTRASEÑA` y
`ConvertTo-SecureString 'CONTRASEÑA'` dentro del comando, exponiendola.

Ahora:
  - Conexion a \\\\host\\c$: API nativa WNetAddConnection2W (mpr.dll) via
    ctypes. La contraseña viaja en memoria, igual que en el resto del
    proyecto (ping_engine.py y credenciales.py ya usan ctypes -> DLL nativa).
  - PowerShell: la contraseña se entrega en una variable de entorno que
    existe SOLO en el proceso hijo. El script la lee, la convierte a
    SecureString y la borra de inmediato. El texto del comando (lo unico
    que queda visible o registrado) solo contiene el NOMBRE de la variable,
    nunca el valor.

Limitacion conocida (documentada a proposito): durante los milisegundos
entre que PowerShell arranca y borra la variable, otro proceso con
privilegios suficientes sobre ese mismo proceso podria leer su bloque de
entorno. Es un riesgo mucho menor que el anterior y aceptable para una
herramienta interna de soporte.
"""

import ctypes
import os
import subprocess
from ctypes import wintypes

# Nombre de la variable de entorno temporal. Solo existe en el proceso hijo.
_VAR_PASSWORD = "PINGMONITOR_EMERGENCY_PW"

_RESOURCETYPE_DISK = 0x00000001
_NO_ERROR = 0


class _NETRESOURCEW(ctypes.Structure):
    _fields_ = [
        ("dwScope", wintypes.DWORD),
        ("dwType", wintypes.DWORD),
        ("dwDisplayType", wintypes.DWORD),
        ("dwUsage", wintypes.DWORD),
        ("lpLocalName", wintypes.LPWSTR),
        ("lpRemoteName", wintypes.LPWSTR),
        ("lpComment", wintypes.LPWSTR),
        ("lpProvider", wintypes.LPWSTR),
    ]


def conectar_recurso_smb(ruta_remota: str, usuario: str, password: str) -> tuple[bool, str]:
    """
    Equivalente a `net use <ruta_remota> /user:<usuario> <password>` pero
    llamando directo a WNetAddConnection2W: ningun proceso nuevo, ningun
    argumento visible.

    Devuelve (exito, mensaje_de_error).
    """
    mpr = ctypes.WinDLL("mpr.dll")
    funcion = mpr.WNetAddConnection2W
    funcion.argtypes = [
        ctypes.POINTER(_NETRESOURCEW),
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
    ]
    funcion.restype = wintypes.DWORD

    recurso = _NETRESOURCEW()
    recurso.dwType = _RESOURCETYPE_DISK
    recurso.lpRemoteName = ruta_remota
    # lpLocalName = None -> conexion sin letra de unidad, igual que el net use original.

    codigo = funcion(ctypes.byref(recurso), password, usuario, 0)
    if codigo == _NO_ERROR:
        return True, ""
    return False, f"Error {codigo}: {ctypes.FormatError(codigo).strip()}"


def _script_con_credencial(usuario: str, script: str) -> str:
    """
    Antepone al script la construccion de $cred a partir de la variable de
    entorno temporal, borrandola en cuanto se usa. El usuario no es secreto;
    se escapan comillas simples por consistencia ('' = comilla literal).
    """
    usuario_escapado = usuario.replace("'", "''")
    return (
        f"$__pw = ConvertTo-SecureString $env:{_VAR_PASSWORD} -AsPlainText -Force; "
        f"Remove-Item Env:{_VAR_PASSWORD}; "
        f"$cred = New-Object System.Management.Automation.PSCredential('{usuario_escapado}', $__pw); "
        f"Remove-Variable __pw; "
        f"{script}; "
        f"Remove-Variable cred"
    )


def _entorno_con_password(password: str) -> dict:
    """Copia del entorno actual + la variable temporal, solo para el hijo."""
    entorno = os.environ.copy()
    entorno[_VAR_PASSWORD] = password
    return entorno


def powershell_interactivo_con_credencial(usuario: str, password: str, script: str) -> None:
    """Abre una ventana de PowerShell (-NoExit) que ejecuta `script` con $cred."""
    subprocess.Popen(
        ["powershell.exe", "-NoExit", "-Command", _script_con_credencial(usuario, script)],
        env=_entorno_con_password(password),
    )


def powershell_con_credencial(usuario: str, password: str, script: str, timeout: int = 30):
    """Ejecuta `script` con $cred y devuelve el CompletedProcess (stdout/stderr capturados)."""
    return subprocess.run(
        ["powershell.exe", "-Command", _script_con_credencial(usuario, script)],
        env=_entorno_con_password(password),
        capture_output=True, text=True, timeout=timeout,
    )
