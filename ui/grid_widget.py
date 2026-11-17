"""
ui/grid_widget.py — PingMonitor

Grid tipo "luces de rack" dibujado a mano con QPainter, organizado en
bloques por categoria: un encabezado + su propia cuadricula de LEDs por
cada tipo de dispositivo (PCs, Impresoras, Telefonos, etc.), en vez de
una sola cuadricula mezclada.

Columnas dinamicas dentro de cada bloque: se recalculan segun el ancho
disponible, nunca hay scroll horizontal. El layout completo (posicion de
encabezados y LEDs) se cachea y solo se recalcula cuando cambia el ancho
de la ventana o los filtros activos — NO en cada ping ni en cada
movimiento del mouse, para que siga siendo fluido con 140+ hosts.
"""

import math
import socket
import subprocess
import time
import webbrowser
from datetime import datetime

from PySide6.QtCore import Qt, QPoint, QRect, QSize, QTimer, Signal
from PySide6.QtGui import QPainter, QColor, QBrush, QPen, QFont, QFontMetrics
from PySide6.QtWidgets import QWidget, QSizePolicy, QToolTip, QMenu, QMessageBox

from ping_engine import EstadoHost, CONFIRMACIONES_PARA_CAIDA


class GridLuces(QWidget):
    LED_DIAMETRO = 26
    LED_ESPACIO = 14
    MARGEN = 16
    ALTO_ENCABEZADO = 30
    ESPACIO_TRAS_ENCABEZADO = 10
    ESPACIO_ENTRE_BLOQUES = 10
    ESPACIO_ENTRE_BLOQUES_PEQUENOS = 16  # espacio horizontal cuando comparten fila
    PADDING_INTERNO_BLOQUE_PEQUENO = 20
    UMBRAL_ANCHO_BLOQUE_PEQUENO = 0.5  # una categoria solo se "empaqueta" si su fila propia ocupa menos de esta fraccion del ancho disponible

    historial_solicitado = Signal(str)      # ip — doble clic
    silenciado_cambiado = Signal(str, bool)  # ip, nuevo_estado_silenciado
    emergencia_c_solicitada = Signal(str)   # ip — "Abrir C$ (modo local)"
    ver_procesos_solicitada = Signal(str)             # ip
    ver_procesos_emergencia_solicitada = Signal(str)  # ip
    reiniciar_solicitada = Signal(str)             # ip
    reiniciar_emergencia_solicitada = Signal(str)  # ip

    _COLORES = {
        EstadoHost.ONLINE: QColor("#3ddc84"),
        EstadoHost.LATENCIA_ALTA: QColor("#ffd23d"),
        EstadoHost.PERDIDA_PAQUETES: QColor("#ff9f3d"),
        EstadoHost.OFFLINE: QColor("#ff3d3d"),
        EstadoHost.SILENCIADO: QColor("#4a4a4a"),
    }
    _ROJO_PARPADEO_APAGADO = QColor("#5a1f1f")
    _COLOR_ENCABEZADO = QColor("#000000")

    def __init__(self, hosts: dict, parent=None):
        super().__init__(parent)
        self.hosts = hosts
        self._orden_ips = list(hosts.keys())
        self._hover_ip = None
        self._parpadeo_visible = True

        self._categorias_disponibles = sorted({self._categoria_de(ip) for ip in self._orden_ips})
        self._categorias_activas = set(self._categorias_disponibles)

        self._ubicaciones_disponibles = sorted({self._ubicacion_de(ip) for ip in self._orden_ips})
        self._ubicaciones_activas = set(self._ubicaciones_disponibles)

        self._layout_cache = None
        self._layout_cache_ancho = None

        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        self._timer_parpadeo = QTimer(self)
        self._timer_parpadeo.timeout.connect(self._tick_parpadeo)
        self._timer_parpadeo.start(500)

    def _categoria_de(self, ip: str) -> str:
        categoria = self.hosts[ip].categoria.strip()
        return categoria if categoria else "Sin categoria"

    def _ubicacion_de(self, ip: str) -> str:
        ubicacion = self.hosts[ip].ubicacion.strip()
        return ubicacion if ubicacion else "Sin ubicacion"

    @property
    def categorias_disponibles(self) -> list:
        return list(self._categorias_disponibles)

    @property
    def ubicaciones_disponibles(self) -> list:
        return list(self._ubicaciones_disponibles)

    def establecer_categorias_activas(self, categorias: set):
        self._categorias_activas = categorias
        self._invalidar_layout()

    def establecer_ubicaciones_activas(self, ubicaciones: set):
        self._ubicaciones_activas = ubicaciones
        self._invalidar_layout()

    def _invalidar_layout(self):
        self._layout_cache = None
        self.updateGeometry()
        self.update()

    def _bloques_visibles(self):
        """Agrupa las IPs que pasan los filtros activos por categoria, en orden alfabetico."""
        grupos = {}
        for ip in self._orden_ips:
            if self._categoria_de(ip) not in self._categorias_activas:
                continue
            if self._ubicacion_de(ip) not in self._ubicaciones_activas:
                continue
            grupos.setdefault(self._categoria_de(ip), []).append(ip)
        return [(categoria, grupos[categoria]) for categoria in sorted(grupos.keys())]

    # -- Layout -------------------------------------------------------

    def _columnas_para_ancho(self, ancho: int) -> int:
        paso = self.LED_DIAMETRO + self.LED_ESPACIO
        disponible = max(0, ancho - self.MARGEN * 2)
        return max(1, disponible // paso)

    def _calcular_layout(self, ancho: int):
        """
        Calcula la posicion de cada encabezado y cada LED para un ancho dado.
        Devuelve (bloques, altura_total), donde cada bloque es:
            {'categoria': str, 'rect_encabezado': QRect, 'posiciones': [(ip, x, y), ...]}

        Los bloques "grandes" (necesitan mas de una fila de LEDs a ancho
        completo) ocupan todo el ancho, como siempre. Los bloques "pequeños"
        consecutivos (caben en una sola fila) se empaquetan uno junto a otro
        compartiendo la misma fila, cada uno con su propio encabezado — pero
        solo entre pequeños consecutivos: un bloque grande de por medio corta
        la agrupacion, para no alterar el orden alfabetico de las categorias.
        """
        columnas_totales = self._columnas_para_ancho(ancho)
        paso = self.LED_DIAMETRO + self.LED_ESPACIO
        ancho_disponible = max(0, ancho - self.MARGEN * 2)

        fuente_encabezado = QFont("Segoe UI", 10, QFont.Bold)
        metricas = QFontMetrics(fuente_encabezado)

        y = self.MARGEN
        bloques = []
        grupos = self._bloques_visibles()

        i = 0
        while i < len(grupos):
            categoria, ips = grupos[i]

            if ips:
                ancho_leds_fila = len(ips) * self.LED_DIAMETRO + (len(ips) - 1) * self.LED_ESPACIO
                ancho_texto = metricas.horizontalAdvance(categoria)
                ancho_bloque_candidato = max(ancho_leds_fila, ancho_texto) + self.PADDING_INTERNO_BLOQUE_PEQUENO
            else:
                ancho_bloque_candidato = 0

            # "Pequeño" (empaquetable con vecinos) solo si cabe en una fila
            # Y ademas esa fila es angosta de verdad — si un bloque cabe en
            # una sola fila pero esa fila ya ocupa la mayor parte del ancho
            # (como IMP con 29 hosts), no tiene caso darle un encabezado "a
            # su medida": nada mas cabria a su lado, y se ve inconsistente
            # comparado con los bloques que si usan el ancho completo.
            es_pequeno = (
                0 < len(ips) <= columnas_totales
                and ancho_bloque_candidato <= ancho_disponible * self.UMBRAL_ANCHO_BLOQUE_PEQUENO
            )

            if not es_pequeno:
                # Bloque grande (o vacio): encabezado a todo el ancho, igual que antes.
                rect_encabezado = QRect(0, y, ancho, self.ALTO_ENCABEZADO)
                y += self.ALTO_ENCABEZADO + self.ESPACIO_TRAS_ENCABEZADO

                posiciones = []
                for idx, ip in enumerate(ips):
                    col = idx % columnas_totales
                    fila = idx // columnas_totales
                    x = self.MARGEN + col * paso + self.LED_DIAMETRO // 2
                    y_led = y + fila * paso + self.LED_DIAMETRO // 2
                    posiciones.append((ip, x, y_led))

                filas_bloque = math.ceil(len(ips) / columnas_totales) if ips else 0
                if filas_bloque > 0:
                    y += (filas_bloque - 1) * paso + self.LED_DIAMETRO
                y += self.ESPACIO_ENTRE_BLOQUES

                bloques.append({
                    "categoria": categoria,
                    "rect_encabezado": rect_encabezado,
                    "posiciones": posiciones,
                })
                i += 1
                continue

            # Recolectar el cluster de bloques PEQUEÑOS consecutivos.
            cluster = []
            while i < len(grupos):
                cat_i, ips_i = grupos[i]
                if not (0 < len(ips_i) <= columnas_totales):
                    break
                ancho_leds = len(ips_i) * self.LED_DIAMETRO + (len(ips_i) - 1) * self.LED_ESPACIO
                ancho_texto = metricas.horizontalAdvance(cat_i)
                ancho_bloque = max(ancho_leds, ancho_texto) + self.PADDING_INTERNO_BLOQUE_PEQUENO
                if ancho_bloque > ancho_disponible * self.UMBRAL_ANCHO_BLOQUE_PEQUENO:
                    break
                cluster.append({"categoria": cat_i, "ips": ips_i, "ancho": ancho_bloque})
                i += 1

            # Empaquetar el cluster en filas (varias filas si no caben todos juntos).
            fila_actual = []
            ancho_fila_actual = 0

            for item in cluster:
                extra = item["ancho"] if not fila_actual else item["ancho"] + self.ESPACIO_ENTRE_BLOQUES_PEQUENOS
                if fila_actual and (ancho_fila_actual + extra) > ancho_disponible:
                    y = self._colocar_fila_pequenos(bloques, fila_actual, y, paso)
                    fila_actual = []
                    ancho_fila_actual = 0
                    extra = item["ancho"]
                fila_actual.append(item)
                ancho_fila_actual += extra

            if fila_actual:
                y = self._colocar_fila_pequenos(bloques, fila_actual, y, paso)

        altura_total = y + self.MARGEN
        return bloques, altura_total

    def _colocar_fila_pequenos(self, bloques: list, fila: list, y: int, paso: int) -> int:
        """Coloca una fila de bloques pequeños uno junto al otro y devuelve el nuevo y."""
        x_cursor = self.MARGEN
        for item in fila:
            rect_encabezado = QRect(x_cursor, y, item["ancho"], self.ALTO_ENCABEZADO)
            posiciones = []
            for idx, ip in enumerate(item["ips"]):
                x_led = x_cursor + idx * paso + self.LED_DIAMETRO // 2
                y_led = y + self.ALTO_ENCABEZADO + self.ESPACIO_TRAS_ENCABEZADO + self.LED_DIAMETRO // 2
                posiciones.append((ip, x_led, y_led))
            bloques.append({
                "categoria": item["categoria"],
                "rect_encabezado": rect_encabezado,
                "posiciones": posiciones,
            })
            x_cursor += item["ancho"] + self.ESPACIO_ENTRE_BLOQUES_PEQUENOS

        return y + self.ALTO_ENCABEZADO + self.ESPACIO_TRAS_ENCABEZADO + self.LED_DIAMETRO + self.ESPACIO_ENTRE_BLOQUES

    def _obtener_layout(self):
        """Cachea el layout mientras el ancho no cambie (se invalida en resize/filtros)."""
        ancho = self.width()
        if self._layout_cache is None or self._layout_cache_ancho != ancho:
            self._layout_cache = self._calcular_layout(ancho)
            self._layout_cache_ancho = ancho
        return self._layout_cache

    def _todas_las_posiciones(self):
        bloques, _ = self._obtener_layout()
        for bloque in bloques:
            yield from bloque["posiciones"]

    def resizeEvent(self, event):
        self._layout_cache = None
        super().resizeEvent(event)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, ancho: int) -> int:
        _, altura_total = self._calcular_layout(ancho)
        return altura_total

    def sizeHint(self):
        return QSize(self.width(), self.heightForWidth(self.width() or 800))

    # -- Interaccion ----------------------------------------------------

    def _ip_en_posicion(self, punto: QPoint):
        radio = self.LED_DIAMETRO // 2
        for ip, x, y in self._todas_las_posiciones():
            if (punto.x() - x) ** 2 + (punto.y() - y) ** 2 <= radio ** 2:
                return ip
        return None

    def mouseMoveEvent(self, event):
        ip = self._ip_en_posicion(event.pos())
        if ip != self._hover_ip:
            self._hover_ip = ip
            self.update()

        if ip:
            QToolTip.showText(event.globalPos(), self._texto_tooltip(self.hosts[ip]), self)
        else:
            QToolTip.hideText()

    def mouseDoubleClickEvent(self, event):
        ip = self._ip_en_posicion(event.pos())
        if ip:
            self.historial_solicitado.emit(ip)

    def contextMenuEvent(self, event):
        ip = self._ip_en_posicion(event.pos())
        if not ip:
            return
        host = self.hosts[ip]
        categoria = host.categoria.strip().upper()
        es_pc = categoria == "PC"
        es_imp_o_tel = categoria in ("IMP", "TEL")

        menu = QMenu(self)
        accion_silenciar = menu.addAction("Reactivar" if host.silenciado else "Silenciar")

        accion_abrir_navegador = None
        if es_imp_o_tel:
            menu.addSeparator()
            accion_abrir_navegador = menu.addAction(f"Abrir en navegador (http://{ip})")

        accion_abrir_c = None
        accion_abrir_c_emergencia = None
        accion_ver_procesos = None
        accion_ver_procesos_emergencia = None
        accion_wol = None
        accion_reiniciar = None
        accion_reiniciar_emergencia = None
        if es_pc:
            alcanzable = host.estado not in (EstadoHost.OFFLINE, EstadoHost.CONFIRMANDO_CAIDA)

            menu.addSeparator()
            accion_abrir_c = menu.addAction(f"Abrir \\\\{ip}\\c$")
            accion_ver_procesos = menu.addAction("Ver procesos")
            accion_wol = menu.addAction("Encender (Wake-on-LAN)")
            # No tiene caso "despertar" algo que ya esta respondiendo.
            accion_wol.setEnabled(not alcanzable)
            accion_reiniciar = menu.addAction("Reiniciar")
            accion_reiniciar.setEnabled(alcanzable)

            menu.addSeparator()
            accion_abrir_c_emergencia = menu.addAction("Abrir C$ (modo local)")
            accion_ver_procesos_emergencia = menu.addAction("Ver procesos (modo local)")
            accion_reiniciar_emergencia = menu.addAction("Reiniciar (modo local)")
            # No se puede reiniciar algo que ya esta caido/inalcanzable.
            accion_reiniciar_emergencia.setEnabled(alcanzable)

        seleccionada = menu.exec(event.globalPos())

        if seleccionada == accion_silenciar:
            host.silenciado = not host.silenciado

            # No se toca fallos_consecutivos ni ultima_caida: el tracking real
            # de conectividad es independiente de si esta silenciado o no.
            # Solo se recalcula el estado VISUAL para reflejar el cambio de
            # inmediato, sin esperar al proximo ciclo de ping.
            if host.silenciado:
                host.estado = EstadoHost.SILENCIADO
            elif host.fallos_consecutivos >= CONFIRMACIONES_PARA_CAIDA:
                host.estado = EstadoHost.OFFLINE
            elif host.fallos_consecutivos > 0:
                host.estado = EstadoHost.CONFIRMANDO_CAIDA
            else:
                host.estado = EstadoHost.ONLINE

            self.silenciado_cambiado.emit(ip, host.silenciado)
            self.update()

        elif accion_abrir_c is not None and seleccionada == accion_abrir_c:
            self._abrir_recurso_administrativo(ip)

        elif accion_abrir_c_emergencia is not None and seleccionada == accion_abrir_c_emergencia:
            self.emergencia_c_solicitada.emit(ip)

        elif accion_ver_procesos is not None and seleccionada == accion_ver_procesos:
            self.ver_procesos_solicitada.emit(ip)

        elif accion_ver_procesos_emergencia is not None and seleccionada == accion_ver_procesos_emergencia:
            self.ver_procesos_emergencia_solicitada.emit(ip)

        elif accion_wol is not None and seleccionada == accion_wol:
            self._enviar_wol(host)

        elif accion_reiniciar is not None and seleccionada == accion_reiniciar:
            self.reiniciar_solicitada.emit(ip)

        elif accion_reiniciar_emergencia is not None and seleccionada == accion_reiniciar_emergencia:
            self.reiniciar_emergencia_solicitada.emit(ip)

        elif accion_abrir_navegador is not None and seleccionada == accion_abrir_navegador:
            self._abrir_en_navegador(ip)

    def _abrir_en_navegador(self, ip: str):
        try:
            webbrowser.open(f"http://{ip}")
        except Exception as e:
            print(f"No se pudo abrir http://{ip} en el navegador: {e}")

    def _abrir_recurso_administrativo(self, ip: str):
        """Abre \\\\ip\\c$ en el Explorador, usando la sesion de dominio actual (pass-through)."""
        try:
            subprocess.Popen(["explorer.exe", f"\\\\{ip}\\c$"])
        except Exception as e:
            print(f"No se pudo abrir \\\\{ip}\\c$: {e}")

    def _enviar_wol(self, host):
        """Arma y envia el 'magic packet' de Wake-on-LAN por broadcast UDP."""
        mac = host.mac.strip()
        if not mac:
            QMessageBox.warning(
                self,
                "Wake-on-LAN no disponible",
                f"{host.nombre} ({host.ip}) no tiene MAC configurada en inventario.csv.",
            )
            return

        try:
            mac_limpio = mac.replace(":", "").replace("-", "")
            mac_bytes = bytes.fromhex(mac_limpio)
            if len(mac_bytes) != 6:
                raise ValueError("una MAC valida tiene 6 bytes")
        except (ValueError, TypeError) as e:
            QMessageBox.warning(
                self,
                "MAC invalida",
                f"La MAC de {host.nombre} ({mac}) no tiene un formato valido: {e}",
            )
            return

        # Broadcast dirigido a la subred de esta IP, asumiendo /24
        # (el diseño original asume una red plana /24).
        octetos = host.ip.split(".")
        broadcast = ".".join(octetos[:3] + ["255"])

        paquete = b"\xff" * 6 + mac_bytes * 16

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.sendto(paquete, (broadcast, 9))
            print(f"Wake-on-LAN enviado a {host.nombre} ({mac}) via {broadcast}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"No se pudo enviar el paquete Wake-on-LAN: {e}")

    # -- Tooltip ----------------------------------------------------------

    def _texto_tooltip(self, host) -> str:
        latencia = f"{host.latencia_ms} ms" if host.latencia_ms is not None else "—"

        if host.ultima_caida:
            ultima_caida = datetime.fromtimestamp(host.ultima_caida).strftime("%d/%m %H:%M:%S")
        else:
            ultima_caida = "Sin caidas registradas"

        if host.silenciado:
            tiempo_activo = "Silenciado"
        elif host.estado == EstadoHost.OFFLINE:
            tiempo_activo = "Caido ahora"
        else:
            tiempo_activo = self._formatear_duracion(time.time() - host.conectado_desde)

        return (
            f"<b>{host.nombre}</b><br>"
            f"{host.ip}<br>"
            f"Latencia: {latencia}<br>"
            f"Ultima caida: {ultima_caida}<br>"
            f"Tiempo activo: {tiempo_activo}"
        )

    @staticmethod
    def _formatear_duracion(segundos: float) -> str:
        segundos = int(max(0, segundos))
        horas, resto = divmod(segundos, 3600)
        minutos, segs = divmod(resto, 60)
        if horas:
            return f"{horas}h {minutos}m"
        if minutos:
            return f"{minutos}m {segs}s"
        return f"{segs}s"

    # -- Parpadeo -----------------------------------------------------------

    def _tick_parpadeo(self):
        hay_confirmando = any(
            h.estado == EstadoHost.CONFIRMANDO_CAIDA for h in self.hosts.values()
        )
        if hay_confirmando:
            self._parpadeo_visible = not self._parpadeo_visible
            self.update()

    # -- Dibujo -----------------------------------------------------------

    def actualizar(self):
        """
        Llamar despues de que el motor de ping actualice el dict hosts.
        Solo repinta — el layout (posiciones) no cambia por un ping, asi
        que no hace falta invalidar el cache aqui.
        """
        self.update()

    def _color_para_estado(self, estado: EstadoHost) -> QColor:
        if estado == EstadoHost.CONFIRMANDO_CAIDA:
            return self._COLORES[EstadoHost.OFFLINE] if self._parpadeo_visible else self._ROJO_PARPADEO_APAGADO
        return self._COLORES.get(estado, self._COLORES[EstadoHost.ONLINE])

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor("#0d0d0d"))

        bloques, _ = self._obtener_layout()
        fuente_encabezado = QFont("Segoe UI", 10, QFont.Bold)
        fuente_led = QFont("Segoe UI", 8)
        radio = self.LED_DIAMETRO // 2

        for bloque in bloques:
            painter.setFont(fuente_encabezado)
            painter.fillRect(bloque["rect_encabezado"], self._COLOR_ENCABEZADO)
            painter.setPen(QColor("#ffffff"))
            painter.drawText(
                bloque["rect_encabezado"].adjusted(self.MARGEN, 0, 0, 0),
                Qt.AlignVCenter | Qt.AlignLeft,
                bloque["categoria"],
            )

            painter.setFont(fuente_led)
            for ip, x, y in bloque["posiciones"]:
                host = self.hosts.get(ip)
                if not host:
                    continue

                if ip == self._hover_ip:
                    painter.setPen(QPen(QColor("#ffffff"), 2))
                else:
                    painter.setPen(Qt.NoPen)

                painter.setBrush(QBrush(self._color_para_estado(host.estado)))
                painter.drawEllipse(QPoint(x, y), radio, radio)

                painter.setPen(QColor("#0d0d0d"))
                painter.drawText(
                    QRect(x - radio, y - radio, radio * 2, radio * 2),
                    Qt.AlignCenter,
                    ip.split(".")[-1],
                )
