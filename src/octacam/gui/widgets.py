"""Custom widgets. Port of the widget classes in cpp/src/main_window.{hpp,cpp}."""

import re

from PySide6.QtCore import QDir, QLineF, QPoint, Qt
from PySide6.QtGui import QColor, QDoubleValidator, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QGraphicsView,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QWidget,
)


class DurationInput(QWidget):
    """A duration line edit with an s/min/h unit selector."""

    def __init__(
        self,
        duration_default: float,
        duration_min: float,
        duration_max: float,
        duration_unit_default_index: int,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setContentsMargins(0, 0, 0, 0)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._duration_edit = QLineEdit(self)
        self._duration_edit.setValidator(
            QDoubleValidator(duration_min, duration_max, 2, self)
        )
        self._duration_edit.setText(str(duration_default))
        layout.addWidget(self._duration_edit)

        self._unit_combo = QComboBox(self)
        self._unit_combo.addItem("s")
        self._unit_combo.addItem("min")
        self._unit_combo.addItem("h")
        self._unit_combo.setCurrentIndex(duration_unit_default_index)
        layout.addWidget(self._unit_combo)

    def get_duration(self) -> float:
        """Duration in seconds (the C++ version returned nanoseconds)."""
        try:
            duration = float(self._duration_edit.text())
        except ValueError:
            return 0.0
        unit = self._unit_combo.currentText()
        return duration * {"s": 1.0, "min": 60.0, "h": 3600.0}.get(unit, 1.0)


class DirectoryEdit(QPlainTextEdit):
    """A path editor that normalizes input and can auto-increment a 3-digit
    suffix (001-bhv -> 002-bhv) after each recording."""

    def __init__(self, save_directory_default: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setPlainText(save_directory_default)

    def setPlainText(self, text: str) -> None:
        input_path = text.strip()
        if input_path.startswith("~"):
            input_path = QDir.homePath() + input_path[1:]
        abs_path = QDir(input_path).absolutePath().replace("\\", "/")
        super().setPlainText(abs_path)

    def increment(self) -> None:
        text = self.toPlainText()
        matches = list(re.finditer(r"\d{3}", text))
        if not matches:
            return
        last = matches[-1]
        incremented = f"{int(last.group()) + 1:03d}"
        self.setPlainText(text[: last.start()] + incremented + text[last.end() :])

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.setPlainText(self.toPlainText())
            event.accept()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:
        self.setPlainText(self.toPlainText())
        super().focusOutEvent(event)


class GraphicsView(QGraphicsView):
    """Camera view: keeps the frame fitted on resize and can overlay a
    lime crosshair centered on the viewport."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._cross_visible = False

    def set_cross_visible(self, visible: bool) -> None:
        if self._cross_visible == visible:
            return
        self._cross_visible = visible
        self.viewport().update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.scene() is None:
            return
        self.fitInView(self.scene().itemsBoundingRect(), Qt.KeepAspectRatio)

    def drawForeground(self, painter, rect) -> None:
        super().drawForeground(painter, rect)
        if not self._cross_visible:
            return

        viewport_rect = self.viewport().rect()
        if viewport_rect.isEmpty():
            return
        center = viewport_rect.center()

        scene_left = self.mapToScene(QPoint(viewport_rect.left(), center.y()))
        scene_right = self.mapToScene(QPoint(viewport_rect.right(), center.y()))
        scene_top = self.mapToScene(QPoint(center.x(), viewport_rect.top()))
        scene_bottom = self.mapToScene(QPoint(center.x(), viewport_rect.bottom()))

        pen = QPen(QColor("lime"))
        pen.setWidthF(0.0)
        painter.setPen(pen)
        painter.drawLine(QLineF(scene_left, scene_right))
        painter.drawLine(QLineF(scene_top, scene_bottom))
