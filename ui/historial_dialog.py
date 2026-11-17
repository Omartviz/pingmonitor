"""
ui/historial_dialog.py — PingMonitor

Ventana que se abre con doble clic sobre un host: disponibilidad calculada
y lista de eventos DOWN -> UP con hora exacta y duracion. Los eventos
ocurridos mientras el host estaba silenciado se muestran atenuados y
etiquetados, pero no afectan el porcentaje mostrado arriba.

Sin boton de exportar (decision explicita del diseño: si se necesita
evidencia, se usa screenshot).
"""

from datetime import datetime

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QDialogButtonBox,
)


class HistorialDialog(QDialog):
    def __init__(self, host, historial_store, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Historial — {host.nombre}")
        self.resize(440, 500)
        self.setStyleSheet("background-color: #0d0d0d; color: #e6e6e6;")

        layout = QVBoxLayout(self)

        encabezado = QLabel(f"<b style='font-size:15px'>{host.nombre}</b><br>{host.ip}")
        layout.addWidget(encabezado)

        disponibilidad = historial_store.calcular_disponibilidad(host.ip)
        etiqueta_disp = QLabel(f"Disponibilidad: {disponibilidad:.2f}%")
        etiqueta_disp.setStyleSheet("font-size: 13px; color: #3ddc84; padding: 4px 0;")
        layout.addWidget(etiqueta_disp)

        eventos = historial_store.obtener_historial(host.ip, limite=100)

        lista = QListWidget()
        lista.setStyleSheet("background-color: #1a1a1a; border: none;")

        if not eventos:
            lista.addItem(QListWidgetItem("Sin caidas registradas todavia."))
        else:
            for evento in eventos:
                texto, color = self._formatear_evento(evento)
                item = QListWidgetItem(texto)
                item.setForeground(color)
                lista.addItem(item)

        layout.addWidget(lista)

        botones = QDialogButtonBox(QDialogButtonBox.Close)
        botones.rejected.connect(self.reject)
        botones.button(QDialogButtonBox.Close).clicked.connect(self.accept)
        layout.addWidget(botones)

    @staticmethod
    def _formatear_evento(evento: dict):
        inicio = datetime.fromtimestamp(evento["timestamp_down"]).strftime("%d/%m %H:%M:%S")

        if evento["timestamp_up"]:
            fin = datetime.fromtimestamp(evento["timestamp_up"]).strftime("%H:%M:%S")
            duracion_txt = HistorialDialog._formatear_duracion(evento["duracion_seg"] or 0)
        else:
            fin = "en curso"
            duracion_txt = "—"

        etiqueta = "  (silenciado)" if evento["muted"] else ""
        texto = f"{inicio}  →  {fin}   ({duracion_txt}){etiqueta}"
        color = QColor("#6a6a6a") if evento["muted"] else QColor("#e6e6e6")
        return texto, color

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
