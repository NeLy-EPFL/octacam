"""Main application window. Port of cpp/src/main_window.{hpp,cpp}."""

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon, QImage, QPixmap, QTransform
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDockWidget,
    QDoubleSpinBox,
    QFileDialog,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGridLayout,
    QLabel,
    QMainWindow,
    QMdiArea,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from octacam.camera import CameraSystem
from octacam.config import CameraConfig, OctacamConfig
from octacam.gui.widgets import DirectoryEdit, DurationInput, GraphicsView
from octacam.serial_link import Command, SerialLink

log = logging.getLogger("octacam")

MS_IN_HOUR = 3_600_000
MS_IN_MINUTE = 60_000
MS_IN_SECOND = 1000

STEPS_PER_REVOLUTION = 4096


class MainWindow(QMainWindow):
    def __init__(
        self,
        camera_system: CameraSystem,
        config: OctacamConfig,
        serial_link: SerialLink,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.camera_system = camera_system
        self.config = config
        self.serial_link = serial_link
        self._camera_config_by_serial: dict[str, CameraConfig] = {
            c.serial_number: c for c in config.cameras
        }

        self.tile = True
        self.pixmap_items: list[QGraphicsPixmapItem] = []
        self.graphics_views: list[GraphicsView] = []
        self.fps_labels: list[QLabel] = []
        self.input_widgets: list[QWidget] = []
        self._record_remaining_time_ms = 0
        self.step_start_with_recording_checkbox: QCheckBox | None = None

        self._setup_ui()

    # ------------------------------------------------------------------ UI

    def _camera_config(self, serial_number: str) -> CameraConfig | None:
        return self._camera_config_by_serial.get(serial_number)

    def _setup_ui(self) -> None:
        cfg = self.config.gui

        self.camera_system.set_software_trigger_frequency(cfg.fps_default)
        self.camera_system.start_software_trigger()
        self.setWindowTitle("octacam")

        self.mdi_area = QMdiArea(self)
        self.setCentralWidget(self.mdi_area)

        for _ in self.camera_system:
            pixmap_item = QGraphicsPixmapItem()
            pixmap_item.setTransformOriginPoint(
                pixmap_item.boundingRect().center()
            )
            self.pixmap_items.append(pixmap_item)

            fps_label = QLabel("0 fps")
            fps_label.setAlignment(Qt.AlignRight)
            self.fps_labels.append(fps_label)
        self._update_frames()

        # Sub-windows are created in reverse so the first camera ends up on
        # top of the z-order (and first in the tiling), like the C++ app.
        for i, camera in reversed(list(enumerate(self.camera_system))):
            camera_config = self._camera_config(camera.serial_number)

            widget = QWidget(self)
            layout = QVBoxLayout(widget)

            tool_bar = QToolBar(widget)
            layout.addWidget(tool_bar)
            spacer = QWidget()
            spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            tool_bar.addWidget(spacer)
            tool_bar.addWidget(self.fps_labels[i])

            view = GraphicsView(widget)
            view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            view.setScene(QGraphicsScene(view))
            self.graphics_views.append(view)

            pixmap_item = self.pixmap_items[i]
            view.scene().addItem(pixmap_item)
            layout.addWidget(view)

            transform = QTransform()
            if camera_config is not None:
                transform.scale(camera_config.scale_x, camera_config.scale_y)
                transform.rotate(camera_config.rotation_deg)
            pixmap_item.setTransform(transform)

            sub_window = self.mdi_area.addSubWindow(
                widget, Qt.WindowMinMaxButtonsHint | Qt.WindowTitleHint
            )
            blank = QPixmap(1, 1)
            blank.fill(Qt.transparent)
            sub_window.setWindowIcon(QIcon(blank))
            sub_window.setWindowTitle(camera.name)

            if camera_config is not None:
                if camera_config.window_x >= 0 and camera_config.window_y >= 0:
                    self.tile = False
                if (
                    camera_config.window_width > 0
                    and camera_config.window_height > 0
                ):
                    self.tile = False

        if self.tile:
            self.mdi_area.tileSubWindows()

        self.step_cw_timer = QTimer(self)
        self.step_cw_timer.setTimerType(Qt.PreciseTimer)
        self.step_cw_timer.setInterval(1)
        self.step_cw_timer.timeout.connect(
            lambda: self.serial_link.write_command(Command(n_steps=1))
        )

        self.step_ccw_timer = QTimer(self)
        self.step_ccw_timer.setTimerType(Qt.PreciseTimer)
        self.step_ccw_timer.setInterval(1)
        self.step_ccw_timer.timeout.connect(
            lambda: self.serial_link.write_command(Command(n_steps=-1))
        )

        display_timer = QTimer(self)
        display_timer.setTimerType(Qt.CoarseTimer)
        display_timer.setInterval(cfg.display_refresh_interval_ms)
        display_timer.timeout.connect(self._update_frames)
        display_timer.start()

        self.record_countdown_timer = QTimer(self)
        self.record_countdown_timer.timeout.connect(self._update_record_countdown)
        self.record_countdown_timer.setInterval(
            cfg.record_countdown_timer_interval_ms
        )

        self.check_record_started_timer = QTimer(self)
        self.check_record_started_timer.timeout.connect(self._check_record_started)
        self.check_record_started_timer.setInterval(
            cfg.check_record_started_timer_interval_ms
        )

        dock = QDockWidget(self)
        dock.setAllowedAreas(Qt.RightDockWidgetArea)
        dock.setMinimumWidth(cfg.dock_min_width)
        dock.setMaximumWidth(cfg.dock_max_width)
        dock.setFeatures(dock.features() & ~QDockWidget.DockWidgetClosable)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)

        dock_content = QWidget(dock)
        dock.setWidget(dock_content)
        dock_layout = QVBoxLayout(dock_content)
        dock_layout.setContentsMargins(0, 0, 0, 0)
        dock_layout.setSpacing(0)

        tabs = QTabWidget(dock_content)
        dock_layout.addWidget(tabs)

        margin = 4
        tabs.addTab(self._build_record_tab(tabs, margin), "Record")
        if self.serial_link.is_open:
            tabs.addTab(self._build_arduino_tab(tabs), "Arduino")
            self._update_step_info()
        tabs.addTab(self._build_view_tab(tabs, margin), "View")

        self.input_widgets = [
            self.duration_input,
            self.fps_edit,
            self.save_dir_edit,
            self.trigger_source_combo,
            self.video_writer_combo,
        ]

    def _build_record_tab(self, tabs: QTabWidget, margin: int) -> QWidget:
        cfg = self.config.gui
        record_tab = QWidget(tabs)
        layout = QGridLayout(record_tab)
        layout.setContentsMargins(margin, margin, margin, margin)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(6)

        row = 0
        duration_label = QLabel("Duration:", record_tab)
        duration_label.setAlignment(Qt.AlignRight)
        layout.addWidget(duration_label, row, 0, 1, 1)
        self.duration_input = DurationInput(
            cfg.duration_default,
            cfg.duration_min,
            cfg.duration_max,
            cfg.duration_unit_default_index,
            record_tab,
        )
        layout.addWidget(self.duration_input, row, 1, 1, 1)
        row += 1

        fps_label = QLabel("FPS:", record_tab)
        fps_label.setAlignment(Qt.AlignRight)
        layout.addWidget(fps_label, row, 0, 1, 1)
        self.fps_edit = QDoubleSpinBox(record_tab)
        self.fps_edit.setRange(cfg.fps_min, cfg.fps_max)
        self.fps_edit.setValue(cfg.fps_default)
        self.fps_edit.setDecimals(2)
        self.fps_edit.setSingleStep(1.0)
        self.fps_edit.valueChanged.connect(self._on_fps_value_changed)
        layout.addWidget(self.fps_edit, row, 1, 1, 1)
        row += 1

        save_dir_button = QPushButton("Save directory:", record_tab)
        layout.addWidget(save_dir_button, row, 0, 1, 1, Qt.AlignRight | Qt.AlignTop)
        self.save_dir_edit = DirectoryEdit(cfg.save_directory_default, record_tab)
        self.save_dir_edit.setFixedHeight(
            self.fontMetrics().height() * cfg.save_dir_edit_height_factor
        )
        layout.addWidget(self.save_dir_edit, row, 1, 1, 1)
        row += 1
        save_dir_button.clicked.connect(self._on_save_dir_button_clicked)

        trigger_source_label = QLabel("Trigger source:", record_tab)
        trigger_source_label.setAlignment(Qt.AlignRight)
        layout.addWidget(trigger_source_label, row, 0, 1, 1)
        self.trigger_source_combo = QComboBox(record_tab)
        self.trigger_source_combo.addItem("software")
        self.trigger_source_combo.addItem("external")
        self.trigger_source_combo.setCurrentIndex(cfg.trigger_source_default_index)
        layout.addWidget(self.trigger_source_combo, row, 1, 1, 1)
        row += 1

        video_writer_label = QLabel("Video writer:", record_tab)
        video_writer_label.setAlignment(Qt.AlignRight)
        layout.addWidget(video_writer_label, row, 0, 1, 1)
        self.video_writer_combo = QComboBox(record_tab)
        self.video_writer_combo.addItem("opencv MJPG avi")
        self.video_writer_combo.addItem("opencv avc1 mp4")
        self.video_writer_combo.setCurrentIndex(cfg.video_writer_default_index)
        layout.addWidget(self.video_writer_combo, row, 1, 1, 1)
        row += 1

        self.record_button = QPushButton("Start recording", record_tab)
        self.record_button.clicked.connect(self._on_record_button_clicked)
        layout.addWidget(self.record_button, row, 0, 1, 2)
        row += 1

        self.status_label = QLabel(record_tab)
        self.status_label.setText("")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label, row, 0, 1, 2)
        row += 1

        layout.setRowStretch(row, 1)
        return record_tab

    def _build_arduino_tab(self, tabs: QTabWidget) -> QWidget:
        arduino_tab = QWidget(tabs)
        layout = QGridLayout(arduino_tab)

        step_title = QLabel("Loop", arduino_tab)
        step_title.setAlignment(Qt.AlignCenter)
        layout.addWidget(step_title, 0, 0, 1, 3)

        self.step_info_label = QLabel(arduino_tab)
        self.step_info_label.setAlignment(Qt.AlignCenter)

        self.step_init_ccw_button = QRadioButton("↺", arduino_tab)
        self.step_init_cw_button = QRadioButton("↻", arduino_tab)
        self.step_init_ccw_button.setChecked(True)

        self.step_count_spinbox = QSpinBox(arduino_tab)
        self.step_count_spinbox.setRange(2, 32767)
        self.step_count_spinbox.setValue(4096)
        self.step_count_spinbox.valueChanged.connect(self._update_step_info)

        self.step_interval_us_spinbox = QSpinBox(arduino_tab)
        self.step_interval_us_spinbox.setRange(800, 65535)
        self.step_interval_us_spinbox.setValue(1465)
        self.step_interval_us_spinbox.setSuffix(" μs")
        self.step_interval_us_spinbox.valueChanged.connect(self._update_step_info)

        self.step_rest_ms_spinbox = QSpinBox(arduino_tab)
        self.step_rest_ms_spinbox.setRange(0, 65535)
        self.step_rest_ms_spinbox.setValue(1000)
        self.step_rest_ms_spinbox.setSuffix(" ms")
        self.step_rest_ms_spinbox.valueChanged.connect(self._update_step_info)

        self.step_repeats_spinbox = QSpinBox(arduino_tab)
        self.step_repeats_spinbox.setRange(1, 255)
        self.step_repeats_spinbox.setValue(3)
        self.step_repeats_spinbox.valueChanged.connect(self._update_step_info)

        self.step_init_wait_s_spinbox = QSpinBox(arduino_tab)
        self.step_init_wait_s_spinbox.setRange(0, 255)
        self.step_init_wait_s_spinbox.setValue(10)
        self.step_init_wait_s_spinbox.setSuffix(" s")
        self.step_init_wait_s_spinbox.valueChanged.connect(self._update_step_info)

        step_execute_button = QPushButton("Execute", arduino_tab)
        step_execute_button.clicked.connect(self._on_step_execute_button_clicked)

        step_init_direction_label = QLabel("Initial direction:", arduino_tab)
        step_init_direction_label.setAlignment(Qt.AlignRight)
        layout.addWidget(step_init_direction_label, 1, 0, 1, 1)
        layout.addWidget(self.step_init_ccw_button, 1, 1, 1, 1)
        layout.addWidget(self.step_init_cw_button, 1, 2, 1, 1)

        for row, (text, widget) in enumerate(
            [
                ("Steps:", self.step_count_spinbox),
                ("Step interval:", self.step_interval_us_spinbox),
                ("Rest duration:", self.step_rest_ms_spinbox),
                ("Repeats:", self.step_repeats_spinbox),
                ("Initial wait:", self.step_init_wait_s_spinbox),
            ],
            start=2,
        ):
            label = QLabel(text, arduino_tab)
            label.setAlignment(Qt.AlignRight)
            layout.addWidget(label, row, 0, 1, 1)
            layout.addWidget(widget, row, 1, 1, 2)

        layout.addWidget(self.step_info_label, 7, 0, 1, 3)
        layout.addWidget(step_execute_button, 8, 0, 1, 3)

        self.step_start_with_recording_checkbox = QCheckBox(
            "Start with recording", arduino_tab
        )
        self.step_start_with_recording_checkbox.setChecked(True)
        layout.addWidget(
            self.step_start_with_recording_checkbox, 9, 0, 1, 3, Qt.AlignCenter
        )

        layout.setRowStretch(10, 1)

        single_step_title = QLabel("Adjust position", arduino_tab)
        single_step_title.setAlignment(Qt.AlignCenter)
        layout.addWidget(single_step_title, 11, 0, 1, 3)

        single_step_ccw_button = QPushButton("↺", arduino_tab)
        single_step_cw_button = QPushButton("↻", arduino_tab)
        self.single_step_interval_edit = QSpinBox(arduino_tab)
        self.single_step_interval_edit.setRange(1, 1000)
        self.single_step_interval_edit.setValue(1)
        self.single_step_interval_edit.setSuffix(" ms")

        single_step_ccw_button.pressed.connect(self._on_single_step_ccw_pressed)
        single_step_ccw_button.released.connect(self._on_single_step_ccw_released)
        single_step_cw_button.pressed.connect(self._on_single_step_cw_pressed)
        single_step_cw_button.released.connect(self._on_single_step_cw_released)

        single_step_interval_label = QLabel("Interval:", arduino_tab)
        single_step_interval_label.setAlignment(Qt.AlignRight)
        layout.addWidget(single_step_interval_label, 12, 0, 1, 1)
        layout.addWidget(self.single_step_interval_edit, 12, 1, 1, 2)

        single_step_direction_label = QLabel("Direction:", arduino_tab)
        single_step_direction_label.setAlignment(Qt.AlignRight)
        layout.addWidget(single_step_direction_label, 13, 0, 1, 1)
        layout.addWidget(single_step_ccw_button, 13, 1, 1, 1)
        layout.addWidget(single_step_cw_button, 13, 2, 1, 1)

        return arduino_tab

    def _build_view_tab(self, tabs: QTabWidget, margin: int) -> QWidget:
        view_tab = QWidget(tabs)
        layout = QGridLayout(view_tab)
        layout.setContentsMargins(margin, margin, margin, margin)
        layout.setSpacing(8)

        layout.addWidget(QLabel("Apply to:", view_tab), 0, 0, 1, 1)
        self.transform_selected_button = QRadioButton("Selected", view_tab)
        layout.addWidget(self.transform_selected_button, 0, 1, 1, 1)
        self.transform_all_button = QRadioButton("All", view_tab)
        layout.addWidget(self.transform_all_button, 0, 2, 1, 1)
        self.transform_all_button.setChecked(True)

        layout.addWidget(QLabel("Rotate:", view_tab), 1, 0, 1, 1)
        rotate_ccw_button = QPushButton("↺", view_tab)
        layout.addWidget(rotate_ccw_button, 1, 1, 1, 1)
        rotate_ccw_button.clicked.connect(
            lambda: self._transform_displays(angle_delta=-90)
        )
        rotate_cw_button = QPushButton("↻", view_tab)
        layout.addWidget(rotate_cw_button, 1, 2, 1, 1)
        rotate_cw_button.clicked.connect(
            lambda: self._transform_displays(angle_delta=90)
        )

        layout.addWidget(QLabel("Flip:", view_tab), 2, 0, 1, 1)
        hflip_button = QPushButton("Horizontal", view_tab)
        layout.addWidget(hflip_button, 2, 1, 1, 1)
        hflip_button.clicked.connect(lambda: self._transform_displays(hflip=True))
        vflip_button = QPushButton("Vertical", view_tab)
        layout.addWidget(vflip_button, 2, 2, 1, 1)
        vflip_button.clicked.connect(lambda: self._transform_displays(vflip=True))

        reset_button = QPushButton("Reset", view_tab)
        layout.addWidget(reset_button, 3, 0, 1, 3)
        reset_button.clicked.connect(lambda: self._transform_displays(reset=True))

        self.display_cross_checkbox = QCheckBox("Display cross", view_tab)
        self.display_cross_checkbox.setChecked(False)
        layout.addWidget(self.display_cross_checkbox, 4, 0, 1, 3, Qt.AlignCenter)
        self.display_cross_checkbox.toggled.connect(self._on_cross_toggled)

        layout.setRowStretch(5, 1)
        return view_tab

    # ------------------------------------------------------------ transforms

    def _on_cross_toggled(self, checked: bool) -> None:
        for view in self.graphics_views:
            view.set_cross_visible(checked)

    @staticmethod
    def _apply_item_transform(item, angle_delta, hflip, vflip, reset) -> None:
        item.setTransformOriginPoint(item.boundingRect().center())
        if reset:
            item.setRotation(0)
            item.setTransform(QTransform())
            return
        item.setRotation(item.rotation() + angle_delta)
        if hflip or vflip:
            center = item.boundingRect().center()
            flip = QTransform()
            flip.translate(center.x(), center.y())
            flip.scale(-1.0 if hflip else 1.0, -1.0 if vflip else 1.0)
            flip.translate(-center.x(), -center.y())
            item.setTransform(flip, True)

    @staticmethod
    def _refit_view(view: GraphicsView) -> None:
        content_bounds = view.scene().itemsBoundingRect()
        view.fitInView(content_bounds, Qt.KeepAspectRatio)
        view.centerOn(content_bounds.center())

    def _transform_displays(
        self,
        angle_delta: int = 0,
        hflip: bool = False,
        vflip: bool = False,
        reset: bool = False,
    ) -> None:
        if self.transform_all_button.isChecked():
            for pixmap_item in self.pixmap_items:
                self._apply_item_transform(
                    pixmap_item, angle_delta, hflip, vflip, reset
                )
                scene = pixmap_item.scene()
                if scene is None or not scene.views():
                    continue
                view = scene.views()[0]
                if isinstance(view, GraphicsView):
                    self._refit_view(view)
            return

        active_sub_window = self.mdi_area.currentSubWindow()
        if active_sub_window is None:
            return
        view = active_sub_window.widget().findChild(GraphicsView)
        if view is None or view.scene() is None:
            return
        for item in view.scene().items():
            self._apply_item_transform(item, angle_delta, hflip, vflip, reset)
        self._refit_view(view)

    # -------------------------------------------------------------- display

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.tile or self.mdi_area is None:
            return
        sub_windows = self.mdi_area.subWindowList()
        for i, camera in enumerate(reversed(list(self.camera_system))):
            camera_config = self._camera_config(camera.serial_number)
            if camera_config is None or i >= len(sub_windows):
                continue
            sub_window = sub_windows[i]
            if camera_config.window_x >= 0 and camera_config.window_y >= 0:
                sub_window.move(
                    round(camera_config.window_x * self.mdi_area.width()),
                    round(camera_config.window_y * self.mdi_area.height()),
                )
            if camera_config.window_width > 0 and camera_config.window_height > 0:
                sub_window.resize(
                    round(camera_config.window_width * self.mdi_area.width()),
                    round(camera_config.window_height * self.mdi_area.height()),
                )

    def _update_frames(self) -> None:
        frames_and_fps = self.camera_system.get_frames_and_fps()
        for pixmap_item, fps_label, (frame, fps) in zip(
            self.pixmap_items, self.fps_labels, frames_and_fps
        ):
            if frame is None:
                continue
            height, width = frame.shape
            image = QImage(
                frame.data,
                width,
                height,
                frame.strides[0],
                QImage.Format_Grayscale8,
            )
            pixmap_item.setPixmap(QPixmap.fromImage(image))
            fps_label.setText(f"{fps:6.2f} fps")

    # ------------------------------------------------------------ recording

    def _check_record_started(self) -> None:
        if self.camera_system.all_cameras_started:
            if (
                self.step_start_with_recording_checkbox is not None
                and self.step_start_with_recording_checkbox.isChecked()
            ):
                self._on_step_execute_button_clicked()
            self.check_record_started_timer.stop()
            self._update_record_countdown()
            self.record_countdown_timer.start()
            self.record_button.setText("Stop recording")

    def _update_record_countdown(self) -> None:
        if self._record_remaining_time_ms >= 0:
            current_ms = self._record_remaining_time_ms
            hours, current_ms = divmod(current_ms, MS_IN_HOUR)
            minutes, current_ms = divmod(current_ms, MS_IN_MINUTE)
            seconds = current_ms // MS_IN_SECOND
            self.status_label.setText(
                f"Remaining time: {hours:2d}:{minutes:02d}:{seconds:02d}"
            )
            self._record_remaining_time_ms -= (
                self.record_countdown_timer.interval()
            )
        else:
            self._stop_record()
            self.status_label.setText("Recording finished")

    def _on_record_button_clicked(self) -> None:
        if self.record_button.text() == "Start recording":
            self._start_record()
        else:
            self._stop_record()

    def _start_record(self) -> None:
        self.record_button.setEnabled(False)
        for widget in self.input_widgets:
            widget.setEnabled(False)

        save_dir = Path(self.save_dir_edit.toPlainText())
        success = False
        if save_dir.exists():
            reply = QMessageBox.question(
                self,
                "Warning",
                f"Directory already exists: {save_dir}\n"
                "Data might be overwritten. Continue?",
                QMessageBox.Yes | QMessageBox.No,
            )
            success = reply == QMessageBox.Yes
        else:
            try:
                save_dir.mkdir(parents=True)
                success = True
            except OSError:
                QMessageBox.critical(
                    self, "Error", f"Could not create directory: {save_dir}"
                )

        if not success:
            for widget in self.input_widgets:
                widget.setEnabled(True)
            self.record_button.setEnabled(True)
            return

        fps = self.fps_edit.value()
        duration_s = self.duration_input.get_duration()
        self._record_remaining_time_ms = round(duration_s * 1000)

        video_writer_info = self.video_writer_combo.currentText()
        fourcc, _, extension = video_writer_info.removeprefix(
            "opencv "
        ).partition(" ")
        if len(fourcc) != 4 or not extension:
            QMessageBox.warning(
                self,
                "Warning",
                "Could not parse video writer format. Recording was not started.",
            )
            for widget in self.input_widgets:
                widget.setEnabled(True)
            self.record_button.setEnabled(True)
            return

        use_software_trigger = (
            self.trigger_source_combo.currentText() == "software"
        )

        self.camera_system.stop_software_trigger()
        if not use_software_trigger:
            self.camera_system.set_trigger_source(False)

        self.camera_system.start_record(save_dir, fps, fourcc, extension)

        self.status_label.setText("Waiting for first trigger...")
        self.record_button.setText("Abort recording")

        if use_software_trigger:
            self.camera_system.start_software_trigger(duration_s)

        self.check_record_started_timer.start()
        self.record_button.setEnabled(True)

    def _stop_record(self) -> None:
        self.record_button.setEnabled(False)

        self.record_countdown_timer.stop()
        self.check_record_started_timer.stop()
        self.camera_system.stop_software_trigger()

        self.camera_system.start_preview()
        self.camera_system.start_software_trigger()
        self.save_dir_edit.increment()

        for widget in self.input_widgets:
            widget.setEnabled(True)

        if self.record_button.text() == "Abort recording":
            self.status_label.setText("Recording aborted")
        else:
            self.status_label.setText("Recording stopped")

        self.record_button.setText("Start recording")
        self.record_button.setEnabled(True)

    def _on_fps_value_changed(self, value: float) -> None:
        if value > 1e-6:
            self.camera_system.set_software_trigger_frequency(value)

    def _on_save_dir_button_clicked(self) -> None:
        start_dir = self.save_dir_edit.toPlainText()
        selected_dir = QFileDialog.getExistingDirectory(
            self,
            "Select save directory",
            start_dir,
            QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks,
        )
        if selected_dir:
            self.save_dir_edit.setPlainText(selected_dir)

    # -------------------------------------------------------------- arduino

    def _on_single_step_ccw_pressed(self) -> None:
        self.step_ccw_timer.setInterval(self.single_step_interval_edit.value())
        self.step_ccw_timer.start()

    def _on_single_step_ccw_released(self) -> None:
        self.step_ccw_timer.stop()
        self.serial_link.write_command(Command(n_steps=0))

    def _on_single_step_cw_pressed(self) -> None:
        self.step_cw_timer.setInterval(self.single_step_interval_edit.value())
        self.step_cw_timer.start()

    def _on_single_step_cw_released(self) -> None:
        self.step_cw_timer.stop()
        self.serial_link.write_command(Command(n_steps=0))

    def _on_step_execute_button_clicked(self) -> None:
        direction = 1 if self.step_init_cw_button.isChecked() else -1
        self.serial_link.write_command(
            Command(
                n_steps=direction * self.step_count_spinbox.value(),
                step_interval_us=self.step_interval_us_spinbox.value(),
                rest_duration_ms=self.step_rest_ms_spinbox.value(),
                n_repeats=self.step_repeats_spinbox.value(),
                init_wait_duration_s=self.step_init_wait_s_spinbox.value(),
            )
        )

    def _update_step_info(self) -> None:
        interval_us = self.step_interval_us_spinbox.value()
        rest_duration_us = self.step_rest_ms_spinbox.value() * 1000
        init_wait_duration_us = self.step_init_wait_s_spinbox.value() * 1_000_000
        n_repeats = self.step_repeats_spinbox.value()
        n_steps = self.step_count_spinbox.value()
        duration_us = interval_us * n_steps
        rpm = 60_000_000 / (STEPS_PER_REVOLUTION * interval_us)
        total_duration_us = (
            (duration_us + rest_duration_us) * n_repeats * 2
            + init_wait_duration_us
            - rest_duration_us
        )
        total_duration_s = total_duration_us / 1_000_000
        self.step_info_label.setText(
            f"Total duration: {total_duration_s:.3f} s, RPM: {rpm:.3f}"
        )
