"""
main.py — PingMonitor

Ventana principal + capa de persistencia conectada:
  - historial.db (SQLite): eventos de caida/recuperacion reales, con
    flag muted para los ocurridos mientras el host estaba silenciado.
  - inventario.csv: se reescribe cada vez que se silencia/reactiva un
    host, para que el estado sobreviva a cerrar y volver a abrir la app.

IMPORTANTE sobre hilos: las señales de PingWorker (que corre en su propio
QThread) se conectan a metodos de VentanaPrincipal en vez de funciones
sueltas. VentanaPrincipal es un QObject que vive en el hilo principal, asi
que Qt encola automaticamente esas llamadas ahi (QueuedConnection) — todo
el acceso a SQLite termina ocurriendo en un solo hilo sin tener que
sincronizar nada a mano.

Tambien incluye: filtro por categorias, panel de top de desconexiones
(24h/7d/30d), alerta sonora con cooldown anti-tormenta, alertas por
Telegram y un "modo emergencia" con cuenta local (ver conexion_segura.py).
"""

import csv
import signal
import subprocess
import sys
import threading
import time
import winsound
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QCheckBox,
    QPushButton,
    QSizePolicy,
    QMessageBox,
    QDialog,
)

from ping_engine import Host, PingWorker, EstadoHost, CONFIRMACIONES_PARA_CAIDA
from historial_store import HistorialStore
from credenciales import AlmacenCredenciales
from conexion_segura import (
    conectar_recurso_smb,
    powershell_con_credencial,
    powershell_interactivo_con_credencial,
)
from telegram_alertas import crear_config_si_falta, cargar_config as cargar_config_telegram, enviar_mensaje as enviar_telegram
from ui.grid_widget import GridLuces
from ui.historial_dialog import HistorialDialog
from ui.credenciales_dialog import CredencialesDialog

def _carpeta_base() -> Path:
    """
    Carpeta donde viven inventario.csv y historial.db.

    Con Path(__file__).parent normal esto apunta a la carpeta del script.
    Empaquetado con PyInstaller, hay que usar sys.executable en su lugar.
    PyInstaller 6.x organiza los archivos internos en una subcarpeta
    "_internal" junto al .exe; si sys.executable resuelve ahi adentro,
    se sube un nivel para quedar junto al .exe real, no dentro de _internal.
    """
    if getattr(sys, "frozen", False):
        carpeta = Path(sys.executable).resolve().parent
        if carpeta.name == "_internal":
            carpeta = carpeta.parent
        return carpeta
    return Path(__file__).parent


RUTA_INVENTARIO = _carpeta_base() / "inventario.csv"
RUTA_HISTORIAL_DB = _carpeta_base() / "historial.db"
RUTA_CREDENCIALES = _carpeta_base() / "credenciales.dat"
RUTA_TELEGRAM_CONFIG = _carpeta_base() / "telegram_config.json"

_UMBRAL_TELEGRAM = 8          # mas de esta cantidad de caidas...
_VENTANA_TELEGRAM_SEG = 60    # ...dentro de esta ventana movil, dispara la alerta

_CAMPOS_CSV = [
    "IP", "Nombre", "Categoria", "Switch", "Puerto", "Ubicacion", "Notas", "Silenciado",
    "Hostname", "GrupoCredencial", "MAC",
]

_VENTANAS_TOP = {
    "24h": 24 * 3600,
    "7 dias": 7 * 24 * 3600,
    "30 dias": 30 * 24 * 3600,
}

_COOLDOWN_ALERTA_SEG = 8  # tiempo minimo entre alertas sonoras, evita tormenta si cae un switch completo


def cargar_inventario(ruta: Path) -> dict[str, Host]:
    resultado = {}
    with open(ruta, newline="", encoding="utf-8") as f:
        lector = csv.DictReader(f)
        for fila in lector:
            ip = fila["IP"].strip()
            resultado[ip] = Host(
                ip=ip,
                nombre=fila.get("Nombre", "").strip(),
                categoria=fila.get("Categoria", "").strip(),
                switch=fila.get("Switch", "").strip(),
                puerto=fila.get("Puerto", "").strip(),
                ubicacion=fila.get("Ubicacion", "").strip(),
                notas=fila.get("Notas", "").strip(),
                silenciado=fila.get("Silenciado", "0").strip() == "1",
                hostname=fila.get("Hostname", "").strip(),
                grupo_credencial=fila.get("GrupoCredencial", "").strip(),
                mac=fila.get("MAC", "").strip(),
            )
    return resultado


def guardar_inventario(ruta: Path, hosts: dict[str, Host]):
    with open(ruta, "w", newline="", encoding="utf-8") as f:
        escritor = csv.writer(f)
        escritor.writerow(_CAMPOS_CSV)
        for host in hosts.values():
            escritor.writerow([
                host.ip, host.nombre, host.categoria, host.switch,
                host.puerto, host.ubicacion, host.notas,
                "1" if host.silenciado else "0",
                host.hostname, host.grupo_credencial, host.mac,
            ])


def reanudar_eventos_abiertos(hosts: dict[str, Host], historial_store: HistorialStore):
    """
    Si un host quedo caido cuando se cerro la app la vez anterior, su evento
    en SQLite se quedo sin timestamp_up (abierto). Sin esto, al reabrir la
    app el motor lo trataria como una caida nueva tras 3 fallos, dejando el
    evento viejo huerfano para siempre en vez de cerrarlo cuando se recupere.

    Aqui se "adelanta" el estado del host para que continue ese mismo evento:
    si en efecto sigue caido, no se crea un evento duplicado; si ya se
    recupero mientras la app estaba cerrada, se cierra correctamente con
    la duracion real (incluyendo el tiempo que la app estuvo apagada).
    """
    for host in hosts.values():
        evento_abierto = historial_store.obtener_evento_abierto(host.ip)
        if evento_abierto:
            host.ultima_caida = evento_abierto["timestamp_down"]
            host.fallos_consecutivos = CONFIRMACIONES_PARA_CAIDA
            host.estado = EstadoHost.SILENCIADO if host.silenciado else EstadoHost.OFFLINE


class VentanaPrincipal(QMainWindow):
    def __init__(self, hosts: dict[str, Host], historial_store: HistorialStore):
        super().__init__()
        self.hosts = hosts
        self.historial_store = historial_store
        self.almacen_credenciales = AlmacenCredenciales(RUTA_CREDENCIALES)
        self.telegram_config = cargar_config_telegram(RUTA_TELEGRAM_CONFIG)
        self._alerta_telegram_activa = False
        self._ultima_alerta_sonora = 0.0
        self._ultimos_desconectados = deque()  # sin limite: se pidio historial "infinito" aqui
        # Se precarga con el historial real (no arranca vacia cada vez que
        # se abre la app) — ya vienen ordenadas de mas reciente a mas vieja.
        for ip, timestamp in self.historial_store.ultimas_desconexiones(15):
            self._ultimos_desconectados.append((self._nombre_de(ip), timestamp))

        self.setWindowTitle("PingMonitor")
        self.resize(1100, 650)
        self.setStyleSheet("background-color: #0d0d0d; color: #e6e6e6;")

        contenedor = QWidget()
        layout = QVBoxLayout(contenedor)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(8)

        self.barra_stats = QLabel()
        self.barra_stats.setStyleSheet(
            "font-size: 13px; padding: 8px; background-color: #1a1a1a; border-radius: 6px;"
        )
        layout.addWidget(self.barra_stats)

        fila_contenido = QHBoxLayout()
        fila_contenido.setSpacing(10)

        columna_izquierda = QVBoxLayout()
        columna_izquierda.setSpacing(8)

        self.grid = GridLuces(hosts)
        self.grid.historial_solicitado.connect(self._mostrar_historial)
        self.grid.silenciado_cambiado.connect(self._on_silenciado_cambiado)
        self.grid.emergencia_c_solicitada.connect(self._on_emergencia_c)
        self.grid.ver_procesos_solicitada.connect(self._on_ver_procesos)
        self.grid.ver_procesos_emergencia_solicitada.connect(self._on_ver_procesos_emergencia)
        self.grid.reiniciar_solicitada.connect(self._on_reiniciar)
        self.grid.reiniciar_emergencia_solicitada.connect(self._on_reiniciar_emergencia)

        columna_izquierda.addWidget(self._crear_panel_zonas())
        columna_izquierda.addWidget(self._crear_panel_categorias())

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("border: none;")
        scroll.setWidget(self.grid)
        columna_izquierda.addWidget(scroll)

        fila_contenido.addLayout(columna_izquierda, 1)

        columna_derecha = QVBoxLayout()
        columna_derecha.setSpacing(10)

        self._ventana_top_actual = "24h"
        columna_derecha.addWidget(self._crear_panel_top_desconexiones())
        columna_derecha.addWidget(self._crear_panel_ultimos_desconectados(), 1)

        fila_contenido.addLayout(columna_derecha)

        layout.addLayout(fila_contenido)

        self.barra_stats_inferior = QLabel()
        self.barra_stats_inferior.setStyleSheet(
            "font-size: 13px; padding: 8px; background-color: #1a1a1a; border-radius: 6px;"
        )
        layout.addWidget(self.barra_stats_inferior)

        self.setCentralWidget(contenedor)
        self._actualizar_barra_stats()
        self._actualizar_top_desconexiones()
        self._actualizar_ultimos_desconectados()

        self._timer_top = QTimer(self)
        self._timer_top.timeout.connect(self._actualizar_top_desconexiones)
        self._timer_top.start(10_000)  # refresco propio, no en cada ping (SQL de mas)

    def _crear_panel_zonas(self) -> QWidget:
        return self._crear_fila_filtro(
            etiqueta_texto="Zonas:",
            valores=self.grid.ubicaciones_disponibles,
            dict_checkboxes_attr="_checkboxes_zona",
            on_cambio=self._on_filtro_zona_cambiado,
        )

    def _crear_panel_categorias(self) -> QWidget:
        return self._crear_fila_filtro(
            etiqueta_texto="Tipo:",
            valores=self.grid.categorias_disponibles,
            dict_checkboxes_attr="_checkboxes_categoria",
            on_cambio=self._on_filtro_categoria_cambiado,
        )

    def _crear_fila_filtro(self, etiqueta_texto: str, valores: list,
                            dict_checkboxes_attr: str, on_cambio) -> QWidget:
        panel = QWidget()
        fila = QHBoxLayout(panel)
        fila.setContentsMargins(0, 0, 0, 0)
        fila.setSpacing(10)

        etiqueta = QLabel(etiqueta_texto)
        etiqueta.setStyleSheet("color: #999999; font-size: 12px;")
        fila.addWidget(etiqueta)

        checkboxes = {}
        for valor in valores:
            casilla = QCheckBox(valor)
            casilla.setChecked(True)
            casilla.setStyleSheet("color: #e6e6e6; font-size: 12px;")
            casilla.stateChanged.connect(on_cambio)
            fila.addWidget(casilla)
            checkboxes[valor] = casilla
        setattr(self, dict_checkboxes_attr, checkboxes)

        boton_todos = QPushButton("Todos")
        boton_todos.setStyleSheet(
            "background-color: #1a1a1a; color: #e6e6e6; padding: 3px 10px; border-radius: 4px;"
        )
        boton_todos.clicked.connect(lambda: self._marcar_todas(checkboxes, True))
        fila.addWidget(boton_todos)

        boton_ninguno = QPushButton("Ninguno")
        boton_ninguno.setStyleSheet(
            "background-color: #1a1a1a; color: #e6e6e6; padding: 3px 10px; border-radius: 4px;"
        )
        boton_ninguno.clicked.connect(lambda: self._marcar_todas(checkboxes, False))
        fila.addWidget(boton_ninguno)

        fila.addStretch()
        return panel

    def _marcar_todas(self, checkboxes: dict, marcadas: bool):
        for casilla in checkboxes.values():
            casilla.setChecked(marcadas)
        # setChecked ya dispara el callback de cambio por cada una

    def _on_filtro_categoria_cambiado(self):
        activas = {
            categoria for categoria, casilla in self._checkboxes_categoria.items()
            if casilla.isChecked()
        }
        self.grid.establecer_categorias_activas(activas)
        self._actualizar_barra_stats()

    def _on_filtro_zona_cambiado(self):
        activas = {
            zona for zona, casilla in self._checkboxes_zona.items()
            if casilla.isChecked()
        }
        self.grid.establecer_ubicaciones_activas(activas)
        self._actualizar_barra_stats()

    # -- Panel de Top de desconexiones --

    def _crear_panel_top_desconexiones(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(200)
        panel.setStyleSheet("background-color: #1a1a1a; border-radius: 6px;")

        layout_panel = QVBoxLayout(panel)
        layout_panel.setContentsMargins(12, 10, 12, 10)
        layout_panel.setSpacing(8)

        titulo = QLabel("Top desconexiones")
        titulo.setWordWrap(True)
        titulo.setStyleSheet("font-size: 13px; font-weight: bold; color: #e6e6e6;")
        layout_panel.addWidget(titulo)

        fila_botones = QHBoxLayout()
        fila_botones.setSpacing(4)
        self._botones_ventana_top = {}
        for etiqueta in _VENTANAS_TOP:
            boton = QPushButton(etiqueta)
            boton.setCheckable(True)
            boton.clicked.connect(lambda checked, e=etiqueta: self._cambiar_ventana_top(e))
            fila_botones.addWidget(boton)
            self._botones_ventana_top[etiqueta] = boton
        layout_panel.addLayout(fila_botones)
        self._refrescar_estilos_botones_top()

        self._etiqueta_top = QLabel("Cargando...")
        self._etiqueta_top.setStyleSheet("font-size: 12px; color: #e6e6e6;")
        self._etiqueta_top.setWordWrap(True)
        layout_panel.addWidget(self._etiqueta_top)

        layout_panel.addStretch()
        return panel

    def _crear_panel_ultimos_desconectados(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(200)
        panel.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        panel.setStyleSheet("background-color: #1a1a1a; border-radius: 6px;")

        layout_panel = QVBoxLayout(panel)
        layout_panel.setContentsMargins(12, 10, 12, 10)
        layout_panel.setSpacing(8)

        titulo = QLabel("Últimos desconectados")
        titulo.setWordWrap(True)
        titulo.setStyleSheet("font-size: 13px; font-weight: bold; color: #e6e6e6;")
        layout_panel.addWidget(titulo)

        self._etiqueta_ultimos = QLabel("Sin caidas recientes.")
        self._etiqueta_ultimos.setStyleSheet("font-size: 12px; color: #e6e6e6;")
        self._etiqueta_ultimos.setWordWrap(True)
        self._etiqueta_ultimos.setAlignment(Qt.AlignTop)

        # Sin limite de cantidad -> con el tiempo esta lista puede crecer
        # mucho. El scroll se queda como red de seguridad para cuando la
        # lista sea mas larga que el espacio disponible, pero el panel en
        # si se expande para llenar el alto real de la ventana en vez de
        # quedar topado a una altura fija.
        scroll_ultimos = QScrollArea()
        scroll_ultimos.setWidgetResizable(True)
        scroll_ultimos.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        scroll_ultimos.setStyleSheet("border: none; background: transparent;")
        scroll_ultimos.setWidget(self._etiqueta_ultimos)
        layout_panel.addWidget(scroll_ultimos)

        return panel

    def _actualizar_ultimos_desconectados(self):
        if not self._ultimos_desconectados:
            self._etiqueta_ultimos.setText("Sin caidas recientes.")
            return

        lineas = [
            f"{datetime.fromtimestamp(timestamp).strftime('%H:%M:%S')} — {nombre}"
            for nombre, timestamp in self._ultimos_desconectados
        ]
        self._etiqueta_ultimos.setText("\n".join(lineas))

    def _refrescar_estilos_botones_top(self):
        for etiqueta, boton in self._botones_ventana_top.items():
            activo = etiqueta == self._ventana_top_actual
            boton.setChecked(activo)
            if activo:
                boton.setStyleSheet(
                    "background-color: #3ddc84; color: #0d0d0d; padding: 3px 6px; "
                    "border-radius: 4px; font-size: 11px;"
                )
            else:
                boton.setStyleSheet(
                    "background-color: #2a2a2a; color: #999999; padding: 3px 6px; "
                    "border-radius: 4px; font-size: 11px;"
                )

    def _cambiar_ventana_top(self, etiqueta: str):
        self._ventana_top_actual = etiqueta
        self._refrescar_estilos_botones_top()
        self._actualizar_top_desconexiones()

    def _actualizar_top_desconexiones(self):
        ventana_seg = _VENTANAS_TOP[self._ventana_top_actual]
        resultados = self.historial_store.top_desconexiones(ventana_seg, limite=10)

        if not resultados:
            self._etiqueta_top.setText("Sin caidas registradas\nen esta ventana.")
            return

        lineas = []
        for i, (ip, total) in enumerate(resultados, start=1):
            lineas.append(f"{i}. {self._nombre_de(ip)} — {total}")
        self._etiqueta_top.setText("\n".join(lineas))

    # -- Slots conectados al PingWorker (via QueuedConnection automatica) --

    def actualizar_host(self, ip: str, estado):
        self.grid.actualizar()
        self._actualizar_barra_stats()

    def on_caida_confirmada(self, ip: str, timestamp: float, muted: bool):
        self.historial_store.registrar_caida(ip, timestamp, muted)
        if not muted:
            self._ultimos_desconectados.appendleft((self._nombre_de(ip), timestamp))
            self._actualizar_ultimos_desconectados()
            self._reproducir_alerta_si_corresponde()
            self._verificar_tormenta_telegram()

    def _verificar_tormenta_telegram(self):
        if not self.telegram_config:
            return

        ahora = time.time()
        recientes = [
            nombre for nombre, ts in self._ultimos_desconectados
            if ahora - ts <= _VENTANA_TELEGRAM_SEG
        ]
        cantidad = len(recientes)

        if cantidad > _UMBRAL_TELEGRAM and not self._alerta_telegram_activa:
            # Se dispara una sola vez al cruzar el umbral — no otra vez hasta
            # que la cantidad baje de 8 y vuelva a subir (tormenta nueva de verdad).
            self._alerta_telegram_activa = True
            lista = ", ".join(recientes[:8])
            mensaje = (
                f"⚠️ {cantidad} caidas en el ultimo minuto — PingMonitor\n"
                f"Posible falla de switch o zona.\nUltimas: {lista}"
            )
            threading.Thread(
                target=enviar_telegram,
                args=(self.telegram_config["token"], self.telegram_config["chat_id"], mensaje),
                daemon=True,
            ).start()
        elif cantidad <= _UMBRAL_TELEGRAM:
            self._alerta_telegram_activa = False

    def _nombre_de(self, ip: str) -> str:
        """Nunca devuelve vacio: si el campo Nombre viene en blanco, cae a la IP."""
        if ip in self.hosts:
            nombre = self.hosts[ip].nombre.strip()
            if nombre:
                return nombre
        return ip

    def on_recuperacion_confirmada(self, ip: str, timestamp: float, duracion: float, muted: bool):
        self.historial_store.registrar_recuperacion(ip, timestamp, duracion)

    def _reproducir_alerta_si_corresponde(self):
        ahora = time.time()
        if ahora - self._ultima_alerta_sonora < _COOLDOWN_ALERTA_SEG:
            return
        self._ultima_alerta_sonora = ahora
        try:
            winsound.Beep(1200, 220)
        except Exception as e:
            print(f"No se pudo reproducir la alerta sonora: {e}")

    # -- Slots de interaccion del grid --

    def _mostrar_historial(self, ip: str):
        host = self.hosts[ip]
        dialogo = HistorialDialog(host, self.historial_store, self)
        dialogo.exec()

    def _on_silenciado_cambiado(self, ip: str, silenciado: bool):
        host = self.hosts[ip]
        print(f"{host.nombre} ({ip}) -> {'silenciado' if silenciado else 'reactivado'}")
        guardar_inventario(RUTA_INVENTARIO, self.hosts)
        self._actualizar_barra_stats()

    def _obtener_password_emergencia(self, host: Host) -> Optional[str]:
        """
        Devuelve la contraseña del GrupoCredencial de este host, pidiendola
        (y guardandola cifrada) si es la primera vez que se usa. Devuelve
        None si el host no tiene los datos necesarios o el usuario cancela.
        """
        if not host.hostname or not host.grupo_credencial:
            QMessageBox.warning(
                self,
                "Modo local no disponible",
                f"{host.nombre} ({host.ip}) no tiene Hostname o GrupoCredencial "
                "configurado en inventario.csv — sin eso no se puede armar "
                "la cuenta local para autenticar.",
            )
            return None

        password = self.almacen_credenciales.obtener_password(host.grupo_credencial)
        if password is not None:
            return password

        credenciales_previas = self.almacen_credenciales.cargar() if self.almacen_credenciales.existe() else {}
        dialogo = CredencialesDialog(credenciales_previas, self)
        if dialogo.exec() != QDialog.Accepted:
            return None
        nuevas = dialogo.credenciales()
        if not nuevas:
            return None
        self.almacen_credenciales.guardar(nuevas)

        password = nuevas.get(host.grupo_credencial)
        if password is None:
            QMessageBox.warning(
                self,
                "Falta contraseña",
                f"No se capturo contraseña para el Grupo {host.grupo_credencial} "
                f"(el que le corresponde a {host.nombre}).",
            )
        return password

    def _on_emergencia_c(self, ip: str):
        host = self.hosts.get(ip)
        if not host:
            return

        password = self._obtener_password_emergencia(host)
        if password is None:
            return

        usuario = f"{host.hostname}\\administrador"
        # API nativa en vez de `net use`: la contraseña no aparece en
        # ninguna linea de comandos (ver conexion_segura.py).
        try:
            exito, detalle = conectar_recurso_smb(f"\\\\{host.hostname}\\c$", usuario, password)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"No se pudo conectar al recurso: {e}")
            return

        if not exito:
            QMessageBox.critical(
                self,
                "Autenticacion fallida",
                f"No se pudo autenticar contra {host.hostname} con el Grupo "
                f"{host.grupo_credencial}.\n\n{detalle}",
            )
            return

        subprocess.Popen(["explorer.exe", f"\\\\{ip}\\c$"])

    def _on_ver_procesos(self, ip: str):
        host = self.hosts.get(ip)
        if not host:
            return

        comando = (
            f"Invoke-Command -ComputerName {ip} -ScriptBlock {{ Get-Process }} "
            f"| Sort-Object WS -Descending | Format-Table -AutoSize"
        )
        try:
            subprocess.Popen(["powershell.exe", "-NoExit", "-Command", comando])
        except Exception as e:
            QMessageBox.critical(self, "Error", f"No se pudo abrir PowerShell: {e}")

    def _on_ver_procesos_emergencia(self, ip: str):
        host = self.hosts.get(ip)
        if not host:
            return

        password = self._obtener_password_emergencia(host)
        if password is None:
            return

        usuario = f"{host.hostname}\\administrador"
        # La contraseña viaja en una variable de entorno temporal del proceso
        # hijo, no en el texto del comando (ver conexion_segura.py).
        script = (
            f"Invoke-Command -ComputerName {ip} -Credential $cred -Authentication Negotiate "
            f"-ScriptBlock {{ Get-Process }} | Sort-Object WS -Descending | Format-Table -AutoSize"
        )
        try:
            powershell_interactivo_con_credencial(usuario, password, script)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"No se pudo abrir PowerShell: {e}")

    def _confirmar_reinicio(self, host: Host, modo_emergencia: bool) -> bool:
        titulo = "Confirmar reinicio (modo local)" if modo_emergencia else "Confirmar reinicio"
        respuesta = QMessageBox.question(
            self,
            titulo,
            f"¿Estás seguro que deseas reiniciar {host.nombre} ({host.ip})?\n\n"
            "Esto interrumpe cualquier trabajo en curso en ese equipo.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,  # default en "No" para que un Enter accidental no confirme
        )
        return respuesta == QMessageBox.Yes

    def _ejecutar_reinicio(self, host: Host, comando: str,
                           usuario: Optional[str] = None, password: Optional[str] = None):
        try:
            if password is not None:
                # Modo emergencia: $cred se construye dentro del hijo a partir
                # de una variable de entorno temporal (ver conexion_segura.py).
                resultado = powershell_con_credencial(usuario, password, comando)
            else:
                resultado = subprocess.run(
                    ["powershell.exe", "-Command", comando],
                    capture_output=True, text=True, timeout=30,
                )
        except Exception as e:
            QMessageBox.critical(self, "Error", f"No se pudo enviar el reinicio: {e}")
            return

        error = (resultado.stderr or "").strip()
        if resultado.returncode != 0 or error:
            QMessageBox.critical(
                self,
                "Reinicio fallido",
                f"No se pudo reiniciar {host.nombre} ({host.ip}).\n\n{error or resultado.stdout.strip()}",
            )
            return

        QMessageBox.information(
            self, "Reinicio enviado", f"Se envió la orden de reinicio a {host.nombre} ({host.ip})."
        )

    def _on_reiniciar(self, ip: str):
        host = self.hosts.get(ip)
        if not host:
            return
        if not self._confirmar_reinicio(host, modo_emergencia=False):
            return

        comando = f"Restart-Computer -ComputerName {ip} -Force -Protocol WSMan"
        self._ejecutar_reinicio(host, comando)

    def _on_reiniciar_emergencia(self, ip: str):
        host = self.hosts.get(ip)
        if not host:
            return

        password = self._obtener_password_emergencia(host)
        if password is None:
            return

        if not self._confirmar_reinicio(host, modo_emergencia=True):
            return

        usuario = f"{host.hostname}\\administrador"
        comando = f"Restart-Computer -ComputerName {ip} -Credential $cred -Force -Protocol WSMan"
        self._ejecutar_reinicio(host, comando, usuario=usuario, password=password)

    # -- Barra de estadisticas --

    def _en_filtro_actual(self, host) -> bool:
        return (
            self.grid._categoria_de(host.ip) in self.grid._categorias_activas
            and self.grid._ubicacion_de(host.ip) in self.grid._ubicaciones_activas
        )

    def _actualizar_barra_stats(self):
        if getattr(self, "_checkboxes_categoria", None):
            activos = [h for h in self.hosts.values() if not h.silenciado and self._en_filtro_actual(h)]
            total_en_filtro = [h for h in self.hosts.values() if self._en_filtro_actual(h)]
        else:
            activos = [h for h in self.hosts.values() if not h.silenciado]
            total_en_filtro = list(self.hosts.values())
        online = sum(1 for h in activos if h.estado == EstadoHost.ONLINE)
        latencia_alta = sum(1 for h in activos if h.estado == EstadoHost.LATENCIA_ALTA)
        perdida = sum(1 for h in activos if h.estado == EstadoHost.PERDIDA_PAQUETES)
        offline = sum(1 for h in activos if h.estado == EstadoHost.OFFLINE)
        confirmando = sum(1 for h in activos if h.estado == EstadoHost.CONFIRMANDO_CAIDA)
        silenciados = len(total_en_filtro) - len(activos)

        latencias = [h.latencia_ms for h in activos if h.latencia_ms is not None]
        promedio = f"{sum(latencias) / len(latencias):.0f} ms" if latencias else "—"

        base = len(activos) or 1
        disponibilidad = 100 * sum(
            1 for h in activos
            if h.estado not in (EstadoHost.OFFLINE, EstadoHost.CONFIRMANDO_CAIDA)
        ) / base

        con_latencia = [h for h in activos if h.latencia_ms is not None]
        peor = max(con_latencia, key=lambda h: h.latencia_ms, default=None)
        peor_texto = f"{peor.nombre} ({peor.latencia_ms} ms)" if peor else "—"

        self.barra_stats.setText(
            f"🟢 Online: {online}   🟡 Latencia alta: {latencia_alta}   "
            f"🟠 Perdida: {perdida}   🔴 Offline: {offline + confirmando}   "
            f"⚫ Silenciados: {silenciados}"
        )
        self.barra_stats_inferior.setText(
            f"Latencia promedio: {promedio}    Disponibilidad: {disponibilidad:.1f}%    "
            f"Pico de latencia: {peor_texto}"
        )


if __name__ == "__main__":
    app = QApplication(sys.argv)

    crear_config_si_falta(RUTA_TELEGRAM_CONFIG)

    if not RUTA_INVENTARIO.exists():
        QMessageBox.critical(
            None,
            "Falta inventario.csv",
            f"No se encontro el inventario en:\n{RUTA_INVENTARIO}\n\n"
            "Copia inventario.example.csv como inventario.csv y edítalo "
            "con los equipos de tu red.",
        )
        sys.exit(1)

    hosts = cargar_inventario(RUTA_INVENTARIO)
    print(f"Inventario cargado: {len(hosts)} hosts")

    historial_store = HistorialStore(RUTA_HISTORIAL_DB)
    reanudar_eventos_abiertos(hosts, historial_store)

    ventana = VentanaPrincipal(hosts, historial_store)
    ventana.show()

    worker = PingWorker(hosts, intervalo_seg=3.0, timeout_ms=800)
    worker.host_actualizado.connect(ventana.actualizar_host)
    worker.caida_confirmada.connect(ventana.on_caida_confirmada)
    worker.recuperacion_confirmada.connect(ventana.on_recuperacion_confirmada)
    worker.start()

    def manejar_ctrl_c(*_):
        print("\nDeteniendo (Ctrl+C detectado)...")
        app.quit()

    def al_cerrar():
        worker.detener()
        worker.wait(3000)
        historial_store.cerrar()

    signal.signal(signal.SIGINT, manejar_ctrl_c)
    app.aboutToQuit.connect(al_cerrar)

    # Necesario en PySide6: el event loop de Qt no deja pasar Ctrl+C hasta
    # que Python tiene oportunidad de ejecutar bytecode. Este QTimer fuerza
    # eso cada 200 ms.
    _keepalive = QTimer()
    _keepalive.timeout.connect(lambda: None)
    _keepalive.start(200)

    sys.exit(app.exec())
