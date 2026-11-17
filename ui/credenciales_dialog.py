"""
ui/credenciales_dialog.py — PingMonitor

Ventana pequeña para capturar las 3 contraseñas de administrador local
(una por cada GrupoCredencial). Se muestra solo cuando hace falta —
la primera vez que se usa el modo emergencia, o si se quieren actualizar
las contraseñas despues.

Las contraseñas no se guardan aqui directamente: este dialogo solo las
recolecta y se las entrega a quien lo abrio (VentanaPrincipal), que las
pasa a AlmacenCredenciales para cifrarlas con DPAPI.
"""

from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QFormLayout,
    QLabel,
    QLineEdit,
    QDialogButtonBox,
)


class CredencialesDialog(QDialog):
    def __init__(self, credenciales_actuales: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Credenciales de emergencia")
        self.resize(360, 220)
        self.setStyleSheet("background-color: #0d0d0d; color: #e6e6e6;")

        layout = QVBoxLayout(self)

        info = QLabel(
            "Contraseña del administrador local para cada grupo.\n"
            "Se guardan cifradas en este equipo (DPAPI) — solo se\n"
            "pueden leer desde esta misma PC."
        )
        info.setStyleSheet("font-size: 12px; color: #999999;")
        layout.addWidget(info)

        formulario = QFormLayout()
        formulario.setSpacing(10)

        estilo_campo = "background-color: #1a1a1a; color: #e6e6e6; padding: 4px; border-radius: 4px;"

        self._campo_1 = QLineEdit(credenciales_actuales.get("1", ""))
        self._campo_1.setEchoMode(QLineEdit.Password)
        self._campo_1.setStyleSheet(estilo_campo)
        formulario.addRow("Grupo 1:", self._campo_1)

        self._campo_2 = QLineEdit(credenciales_actuales.get("2", ""))
        self._campo_2.setEchoMode(QLineEdit.Password)
        self._campo_2.setStyleSheet(estilo_campo)
        formulario.addRow("Grupo 2:", self._campo_2)

        self._campo_3 = QLineEdit(credenciales_actuales.get("3", ""))
        self._campo_3.setEchoMode(QLineEdit.Password)
        self._campo_3.setStyleSheet(estilo_campo)
        formulario.addRow("Grupo 3:", self._campo_3)

        layout.addLayout(formulario)

        botones = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        botones.accepted.connect(self.accept)
        botones.rejected.connect(self.reject)
        layout.addWidget(botones)

    def credenciales(self) -> dict:
        """Solo incluye grupos con contraseña no vacia."""
        resultado = {}
        if self._campo_1.text():
            resultado["1"] = self._campo_1.text()
        if self._campo_2.text():
            resultado["2"] = self._campo_2.text()
        if self._campo_3.text():
            resultado["3"] = self._campo_3.text()
        return resultado
