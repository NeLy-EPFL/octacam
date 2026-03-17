#include "main_window.hpp"

#include <QChar>
#include <QComboBox>
#include <QDir>
#include <QDockWidget>
#include <QDoubleSpinBox>
#include <QDoubleValidator>
#include <QFileDialog>
#include <QFrame>
#include <QGraphicsPixmapItem>
#include <QGraphicsScene>
#include <QGraphicsTextItem>
#include <QGraphicsView>
#include <QGridLayout>
#include <QHBoxLayout>
#include <QIcon>
#include <QIntValidator>
#include <QLabel>
#include <QLineEdit>
#include <QMdiArea>
#include <QMdiSubWindow>
#include <QMessageBox>
#include <QPixmap>
#include <QPushButton>
#include <QRadioButton>
#include <QResizeEvent>
#include <QSizePolicy>
#include <QTimer>
#include <QToolBar>
#include <QTransform>
#include <QVBoxLayout>

#include <cmath>
#include <cstdlib>
#include <ranges>
#include <stdexcept>

#include <spdlog/sinks/stdout_color_sinks.h>
#include <spdlog/spdlog.h>

namespace {
constexpr long long MS_IN_HOUR = 3'600'000LL;
constexpr long long MS_IN_MINUTE = 60'000LL;
constexpr long long MS_IN_SECOND = 1000LL;
} // namespace

GraphicsView::GraphicsView(QWidget *parent) : QGraphicsView(parent) {}

GraphicsView::~GraphicsView() = default;

void GraphicsView::resizeEvent(QResizeEvent *event) {
  QGraphicsView::resizeEvent(event);
  fitInView(scene()->itemsBoundingRect(), Qt::KeepAspectRatio);
}

MainWindow::MainWindow(CameraSystem &camera_system, OctacamConfig config,
                       SerialPort &serial_port, QWidget *parent)
    : QMainWindow(parent), camera_system(camera_system), config(config),
      serial_port(serial_port) {
  setup_ui();
}

MainWindow::~MainWindow() = default;

inline std::chrono::nanoseconds fps_to_ns(double fps) {
  return std::chrono::nanoseconds(
      static_cast<long long>(std::round(1.0e9 / fps)));
}

void MainWindow::setup_ui() {
  auto &cfg = config.gui_config;

  camera_system.set_software_trigger_frequency(cfg.fps_default);
  camera_system.start_software_trigger();
  setWindowTitle("octacam");

  mdi_area = new QMdiArea(this);
  setCentralWidget(mdi_area);

  for (auto &camera : camera_system) {
    auto *pixmap_item = new QGraphicsPixmapItem();
    pixmap_items.push_back(pixmap_item);
    pixmap_item->setTransformOriginPoint(pixmap_item->boundingRect().center());

    auto *fps_label = new QLabel("0 fps");
    fps_label->setAlignment(Qt::AlignRight);
    fps_labels.push_back(fps_label);
  }
  update_frames();

  int i = pixmap_items.size() - 1;

  for (auto &camera : std::ranges::reverse_view(camera_system)) {
    auto serial_number = camera.get_serial_number();

    CameraConfig camera_config;
    auto it = std::ranges::find_if(
        config.camera_configs,
        [&serial_number](const CameraConfig &camera_config) {
          return camera_config.serial_number == serial_number;
        });
    if (it != config.camera_configs.end()) {
      camera_config = *it;
    }

    auto *widget = new QWidget(this);
    auto *layout = new QVBoxLayout(widget);

    auto *tool_bar = new QToolBar(widget);
    layout->addWidget(tool_bar);

    auto *spacer = new QWidget();
    spacer->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Preferred);
    tool_bar->addWidget(spacer);

    tool_bar->addWidget(fps_labels[i]);

    auto *view = new GraphicsView(widget);
    view->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    view->setVerticalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    view->setScene(new QGraphicsScene(view));

    auto *pixmap_item = pixmap_items[i];
    view->scene()->addItem(pixmap_item);
    layout->addWidget(view);

    QTransform transform;
    transform.scale(camera_config.scale_x, camera_config.scale_y);
    transform.rotate(camera_config.rotation_deg);
    pixmap_item->setTransform(transform);

    auto sub_window = mdi_area->addSubWindow(
        widget, Qt::WindowMinMaxButtonsHint | Qt::WindowTitleHint);
    QPixmap pixmap{1, 1};
    pixmap.fill(Qt::transparent);
    sub_window->setWindowIcon(QIcon{pixmap});
    auto title = QString::fromStdString(camera.get_name());
    sub_window->setWindowTitle(title);

    if (camera_config.window_x >= 0 && camera_config.window_y >= 0) {
      tile = false;
    }
    if (camera_config.window_width > 0 && camera_config.window_height > 0) {
      tile = false;
    }
    --i;
  }

  if (tile) {
    mdi_area->tileSubWindows();
  }

  step_plus_timer = new QTimer(this);
  step_plus_timer->setTimerType(Qt::PreciseTimer);
  step_plus_timer->setInterval(20);
  connect(step_plus_timer, &QTimer::timeout, this, &MainWindow::step_plus);

  step_minus_timer = new QTimer(this);
  step_minus_timer->setTimerType(Qt::PreciseTimer);
  step_minus_timer->setInterval(20);
  connect(step_minus_timer, &QTimer::timeout, this, &MainWindow::step_minus);

  auto display_timer = new QTimer(this);
  display_timer->setTimerType(Qt::CoarseTimer);
  display_timer->setInterval(cfg.display_refresh_interval_ms);
  connect(display_timer, &QTimer::timeout, this, &MainWindow::update_frames);
  display_timer->start();

  record_countdown_timer = new QTimer(this);
  connect(record_countdown_timer, &QTimer::timeout, this,
          &MainWindow::update_record_countdown);
  record_countdown_timer->setInterval(cfg.record_countdown_timer_interval_ms);

  check_record_started_timer = new QTimer(this);
  connect(check_record_started_timer, &QTimer::timeout, this,
          &MainWindow::check_record_started);
  check_record_started_timer->setInterval(
      cfg.check_record_started_timer_interval_ms);

  auto *dock = new QDockWidget(this);
  dock->setAllowedAreas(Qt::RightDockWidgetArea);
  dock->setMinimumWidth(cfg.dock_min_width);
  dock->setMaximumWidth(cfg.dock_max_width);
  dock->setFeatures(dock->features() & ~QDockWidget::DockWidgetClosable);
  addDockWidget(Qt::RightDockWidgetArea, dock);

  auto *dock_content = new QWidget(dock);
  dock->setWidget(dock_content);

  auto *dock_layout = new QGridLayout(dock_content);
  dock_content->setLayout(dock_layout);
  int row = 0;

  dock_layout->addWidget(new QLabel("Duration"), row, 0);
  duration_input = new DurationInput(
      cfg.duration_default, cfg.duration_min, cfg.duration_max,
      cfg.duration_unit_default_index, dock_content);
  dock_layout->addWidget(duration_input, row++, 1);

  dock_layout->addWidget(new QLabel("FPS:"), row, 0);
  fps_edit = new QDoubleSpinBox(dock_content);
  fps_edit->setRange(cfg.fps_min, cfg.fps_max);
  fps_edit->setValue(cfg.fps_default);
  fps_edit->setDecimals(2);
  fps_edit->setSingleStep(1.0);
  connect(fps_edit, &QDoubleSpinBox::valueChanged, this,
          &MainWindow::on_fps_value_changed);
  dock_layout->addWidget(fps_edit, row++, 1);

  dock_layout->addWidget(new QLabel("Save directory:"), row, 0);
  save_dir_edit = new DirectoryEdit(cfg.save_directory_default, dock_content);
  save_dir_edit->setFixedHeight(fontMetrics().height() *
                                cfg.save_dir_edit_height_factor);
  dock_layout->addWidget(save_dir_edit, row++, 1);

  dock_layout->addWidget(new QLabel("Trigger source:"), row, 0);
  trigger_source_combo = new QComboBox(dock_content);
  trigger_source_combo->addItem("software");
  trigger_source_combo->addItem("external");
  trigger_source_combo->setCurrentIndex(cfg.trigger_source_default_index);

  dock_layout->addWidget(trigger_source_combo, row++, 1);

  dock_layout->addWidget(new QLabel("Video writer:"), row, 0);
  video_writer_combo = new QComboBox(dock_content);
  video_writer_combo->addItem("opencv MJPG avi");
  video_writer_combo->addItem("opencv avc1 mp4");
  video_writer_combo->setCurrentIndex(cfg.video_writer_default_index);
  dock_layout->addWidget(video_writer_combo, row++, 1);

  record_button = new QPushButton("Start recording", dock);
  connect(record_button, &QPushButton::clicked, this,
          &MainWindow::on_record_button_clicked);
  dock_layout->addWidget(record_button, row++, 0, 1, 2);

  status_label = new QLabel(dock_content);
  status_label->setText("");
  status_label->setAlignment(Qt::AlignCenter);
  dock_layout->addWidget(status_label, row++, 0, 1, 2);

  auto step_widget = new QWidget(dock);
  step_widget->setContentsMargins(0, 0, 0, 0);
  step_widget->setLayout(new QHBoxLayout(step_widget));
  step_widget->layout()->setContentsMargins(0, 0, 0, 0);

  auto step_minus_button = new QPushButton("-", dock);
  auto step_plus_button = new QPushButton("+", dock);
  step_interval_edit = new QSpinBox(step_widget);
  step_interval_edit->setRange(1, 1000);
  step_interval_edit->setValue(1);

  connect(step_minus_button, &QPushButton::pressed, this,
          &MainWindow::on_step_minus_button_pressed);
  connect(step_minus_button, &QPushButton::released, this,
          &MainWindow::on_step_minus_button_released);
  connect(step_plus_button, &QPushButton::pressed, this,
          &MainWindow::on_step_plus_button_pressed);
  connect(step_plus_button, &QPushButton::released, this,
          &MainWindow::on_step_plus_button_released);

  step_widget->layout()->addWidget(step_interval_edit);
  step_widget->layout()->addWidget(step_minus_button);
  step_widget->layout()->addWidget(step_plus_button);

  dock_layout->addWidget(new QLabel("Step at interval (ms):"), row, 0);
  dock_layout->addWidget(step_widget, row++, 1);

  auto step_degrees_widget = new QWidget(dock);
  step_degrees_widget->setContentsMargins(0, 0, 0, 0);
  step_degrees_widget->setLayout(new QHBoxLayout(step_degrees_widget));
  step_degrees_widget->layout()->setContentsMargins(0, 0, 0, 0);
  step_degrees_edit = new QDoubleSpinBox(step_degrees_widget);
  step_degrees_edit->setValue(30);

  auto step_degrees_minus_button = new QPushButton("-", step_degrees_widget);
  auto step_degrees_plus_button = new QPushButton("+", step_degrees_widget);
  step_degrees_widget->layout()->addWidget(step_degrees_edit);
  step_degrees_widget->layout()->addWidget(step_degrees_minus_button);
  step_degrees_widget->layout()->addWidget(step_degrees_plus_button);

  connect(step_degrees_minus_button, &QPushButton::clicked, this,
          &MainWindow::on_step_degrees_minus_button_clicked);

  connect(step_degrees_plus_button, &QPushButton::clicked, this,
          &MainWindow::on_step_degrees_plus_button_clicked);

  dock_layout->addWidget(new QLabel("Step by degrees:"), row, 0);
  dock_layout->addWidget(step_degrees_widget, row++, 1);

  dock_layout->setRowStretch(row++, 1);

  auto *h_line = new QFrame(dock_content);
  h_line->setFrameShape(QFrame::HLine);
  h_line->setFrameShadow(QFrame::Sunken);
  dock_layout->addWidget(h_line, row++, 0, 1, 2);

  auto rotate_widget = new QWidget(dock);
  rotate_widget->setContentsMargins(0, 0, 0, 0);
  rotate_widget->setLayout(new QHBoxLayout(rotate_widget));
  rotate_widget->layout()->setContentsMargins(0, 0, 0, 0);
  dock_layout->addWidget(rotate_widget, row, 0, 2, 2);

  rotate_widget->layout()->addWidget(new QLabel("Rotate:"));

  auto rotate_control_widget = new QWidget(rotate_widget);
  rotate_control_widget->setContentsMargins(0, 0, 0, 0);
  rotate_control_widget->setLayout(new QVBoxLayout(rotate_control_widget));
  rotate_control_widget->layout()->setContentsMargins(0, 0, 0, 0);
  rotate_widget->layout()->addWidget(rotate_control_widget);

  auto rotate_buttons_widget = new QWidget(rotate_control_widget);
  rotate_buttons_widget->setContentsMargins(0, 0, 0, 0);
  rotate_buttons_widget->setLayout(new QHBoxLayout(rotate_buttons_widget));
  rotate_buttons_widget->layout()->setContentsMargins(0, 0, 0, 0);
  rotate_control_widget->layout()->addWidget(rotate_buttons_widget);

  auto rotate_ccw_button = new QPushButton("↺", dock);
  rotate_buttons_widget->layout()->addWidget(rotate_ccw_button);
  connect(rotate_ccw_button, &QPushButton::clicked, this,
          &MainWindow::rotate_displays);

  auto rotate_cw_button = new QPushButton("↻", dock);
  rotate_buttons_widget->layout()->addWidget(rotate_cw_button);
  connect(rotate_cw_button, &QPushButton::clicked, this,
          &MainWindow::rotate_displays);

  auto reset_rotation_button = new QPushButton("Reset", dock);
  rotate_buttons_widget->layout()->addWidget(reset_rotation_button);
  connect(reset_rotation_button, &QPushButton::clicked, this,
          &MainWindow::rotate_displays);

  auto rotate_which_widget = new QWidget(rotate_control_widget);
  rotate_which_widget->setContentsMargins(0, 0, 0, 0);
  rotate_which_widget->setLayout(new QHBoxLayout(rotate_which_widget));
  rotate_which_widget->layout()->setContentsMargins(0, 0, 0, 0);
  rotate_control_widget->layout()->addWidget(rotate_which_widget);

  rotate_selected_button = new QRadioButton("Selected", rotate_which_widget);
  rotate_which_widget->layout()->addWidget(rotate_selected_button);

  rotate_all_button = new QRadioButton("All", rotate_which_widget);
  rotate_which_widget->layout()->addWidget(rotate_all_button);

  rotate_all_button->setChecked(true);

  input_widgets.push_back(duration_input);
  input_widgets.push_back(fps_edit);
  input_widgets.push_back(save_dir_edit);
  input_widgets.push_back(trigger_source_combo);
  input_widgets.push_back(video_writer_combo);
}

void MainWindow::rotate_displays() {
  auto *button_sender = qobject_cast<QPushButton *>(sender());
  if (!button_sender) {
    return;
  }

  const QString button_text = button_sender->text();
  int angle_delta = 0;
  bool reset_rotation = false;

  if (button_text == "↺") {
    angle_delta = -90;
  } else if (button_text == "↻") {
    angle_delta = 90;
  } else if (button_text == "Reset") {
    reset_rotation = true;
  } else {
    return;
  }

  if (rotate_all_button->isChecked()) {
    for (auto *pixmap_item : pixmap_items) {
      if (!pixmap_item)
        continue;
      if (reset_rotation) {
        pixmap_item->setRotation(0);
      } else {
        pixmap_item->setRotation(pixmap_item->rotation() + angle_delta);
      }
      if (auto *view = qobject_cast<GraphicsView *>(
              pixmap_item->scene()->views().first())) {
        view->fitInView(pixmap_item->sceneBoundingRect(), Qt::KeepAspectRatio);
      }
    }
    return;
  }

  auto active_sub_window = mdi_area->currentSubWindow();
  if (!active_sub_window) {
    return;
  }

  auto *view = active_sub_window->widget()->findChild<GraphicsView *>();
  if (!view || !view->scene()) {
    return;
  }

  for (auto *item : view->scene()->items()) {
    if (!item)
      continue;
    if (reset_rotation) {
      item->setRotation(0);
    } else {
      item->setRotation(item->rotation() + angle_delta);
    }
    view->fitInView(item->sceneBoundingRect(), Qt::KeepAspectRatio);
  }
}

void MainWindow::resizeEvent(QResizeEvent *event) {
  QMainWindow::resizeEvent(event);
  if (!tile && mdi_area) {
    int i = 0;
    for (auto &camera : std::ranges::reverse_view(camera_system)) {
      auto serial_number = camera.get_serial_number();
      CameraConfig camera_config;
      auto it = std::ranges::find_if(
          config.camera_configs,
          [&serial_number](const CameraConfig &camera_config) {
            return camera_config.serial_number == serial_number;
          });
      if (it != config.camera_configs.end()) {
        camera_config = *it;
      }

      auto *sub_window = mdi_area->subWindowList().at(i++);
      if (camera_config.window_x >= 0 && camera_config.window_y >= 0) {
        sub_window->move(static_cast<int>(std::round(camera_config.window_x *
                                                     mdi_area->width())),
                         static_cast<int>(std::round(camera_config.window_y *
                                                     mdi_area->height())));
      }
      if (camera_config.window_width > 0 && camera_config.window_height > 0) {
        sub_window->resize(
            static_cast<int>(
                std::round(camera_config.window_width * mdi_area->width())),
            static_cast<int>(
                std::round(camera_config.window_height * mdi_area->height())));
      }
    }
  }
}

void MainWindow::update_frames() {
  auto mats_and_fps = camera_system.get_mats_and_fps();
  for (size_t i = 0; i < mats_and_fps.size(); ++i) {
    auto *pixmap_item = pixmap_items[i];
    auto *fps_label = fps_labels[i];

    if (pixmap_item && i < mats_and_fps.size()) {
      auto [mat, fps] = mats_and_fps[i];
      if (!mat) {
        continue;
      }
      cv::Mat frame = mat->clone();
      QImage image(frame.data, frame.cols, frame.rows,
                   static_cast<int>(frame.step[0]), QImage::Format_Grayscale8);
      QPixmap pixmap = QPixmap::fromImage(image);
      pixmap_item->setPixmap(pixmap);
      fps_label->setText(QString("%1 fps").arg(fps, 6, 'f', 2));
      delete mat;
    }
  }
}

void MainWindow::check_record_started() {
  if (camera_system.all_cameras_started()) {
    check_record_started_timer->stop();
    record_countdown_timer->start();
    update_record_countdown();
    record_countdown_timer->start();
    record_button->setText("Stop recording");
  }
}

void MainWindow::update_record_countdown() {
  if (record_remaining_time_.count() >= 0) {
    long long current_ms = record_remaining_time_.count();
    auto hours = current_ms / MS_IN_HOUR;
    current_ms %= MS_IN_HOUR;
    auto minutes = current_ms / MS_IN_MINUTE;
    current_ms %= MS_IN_MINUTE;
    auto seconds = current_ms / MS_IN_SECOND;

    status_label->setText(QString("Remaining time: %1:%2:%3")
                              .arg(hours, 2, 10, QChar(' '))
                              .arg(minutes, 2, 10, QChar('0'))
                              .arg(seconds, 2, 10, QChar('0')));
    record_remaining_time_ -=
        std::chrono::milliseconds(record_countdown_timer->interval());
  } else {
    stop_record();
    status_label->setText("Recording finished");
  }
}

void MainWindow::on_record_button_clicked() {
  auto *record_button = qobject_cast<QPushButton *>(sender());
  if (record_button) {
    if (record_button->text() == "Start recording") {
      start_record();
    } else {
      stop_record();
    }
  }
}

void MainWindow::on_step_minus_button_pressed() {
  step_minus_timer->setInterval(step_interval_edit->value());
  step_minus_timer->start();
}

void MainWindow::on_step_minus_button_released() { step_minus_timer->stop(); }

void MainWindow::on_step_plus_button_pressed() {
  step_plus_timer->setInterval(step_interval_edit->value());
  step_plus_timer->start();
}

void MainWindow::on_step_plus_button_released() { step_plus_timer->stop(); }

void MainWindow::step_plus() {
  Command cmd{0, 1};
  serial_port.writeAll(&cmd, sizeof(cmd));
}

void MainWindow::step_minus() {
  Command cmd{0, -1};
  serial_port.writeAll(&cmd, sizeof(cmd));
}

void MainWindow::on_step_degrees_minus_button_clicked() {
  Command cmd{1, 4096, 800};
  serial_port.writeAll(&cmd, sizeof(cmd));
}

void MainWindow::on_step_degrees_plus_button_clicked() {
  Command cmd{1, -4096, 800};
  serial_port.writeAll(&cmd, sizeof(cmd));
}

void MainWindow::on_fps_value_changed(double value) {
  if (value > 1e-6) {
    camera_system.set_software_trigger_frequency(value);
  }
}

void MainWindow::start_record() {
  record_button->setEnabled(false);

  for (auto *widget : input_widgets) {
    widget->setEnabled(false);
  }

  const std::string save_dir = save_dir_edit->toPlainText().toStdString();

  bool success{false};

  if (std::filesystem::exists(save_dir)) {
    auto msg = QString("Directory already exists: %1\nData might be "
                       "overwritten. Continue?")
                   .arg(QString::fromStdString(save_dir));
    auto reply = QMessageBox::question(this, "Warning", msg,
                                       QMessageBox::Yes | QMessageBox::No);
    if (reply == QMessageBox::Yes) {
      success = true;
    }
  } else if (std::filesystem::create_directories(save_dir)) {
    success = true;
  } else {
    auto msg = QString("Could not create directory: %1")
                   .arg(QString::fromStdString(save_dir));
    QMessageBox::critical(this, "Error", msg);
  }

  if (!success) {
    for (auto *widget : input_widgets) {
      widget->setEnabled(true);
    }
    record_button->setEnabled(true);
    return;
  }

  double fps_val = fps_edit->value();
  std::chrono::nanoseconds duration_ns = duration_input->get_duration();

  record_remaining_time_ =
      std::chrono::duration_cast<std::chrono::milliseconds>(duration_ns);
  const auto video_writer_info =
      video_writer_combo->currentText().toStdString();

  std::string fourcc_str;
  std::string extension_str;

  if (video_writer_info.rfind("opencv ", 0) == 0) {
    std::string details = video_writer_info.substr(7);
    size_t space_pos = details.find(' ');
    if (space_pos != std::string::npos) {
      fourcc_str = details.substr(0, space_pos);
      extension_str = details.substr(space_pos + 1);
    }
  }

  const bool use_software_trigger =
      trigger_source_combo->currentText() == "software";

  camera_system.stop_software_trigger();

  if (!use_software_trigger) {
    camera_system.set_trigger_source(false);
  }

  if (!fourcc_str.empty() && fourcc_str.length() == 4 &&
      !extension_str.empty()) {
    camera_system.start_record(save_dir, fps_val, fourcc_str, extension_str);
  } else {
    QMessageBox::warning(this, "Warning",
                         "Could not parse video writer format. Recording may "
                         "not use specified FOURCC/extension.");
  }

  status_label->setText("Waiting for first trigger...");
  record_button->setText("Abort recording");

  if (use_software_trigger) {
    camera_system.start_software_trigger(duration_ns);
  }

  check_record_started_timer->start();
  record_button->setEnabled(true);
}

void MainWindow::stop_record() {
  record_button->setEnabled(false);

  record_countdown_timer->stop();
  check_record_started_timer->stop();
  camera_system.stop_software_trigger();

  camera_system.start_preview();
  camera_system.start_software_trigger();
  save_dir_edit->increment();

  for (auto *widget : input_widgets) {
    widget->setEnabled(true);
  }

  if (record_button->text() == "Abort recording") {
    status_label->setText("Recording aborted");
  } else {
    status_label->setText("Recording stopped");
  }

  record_button->setText("Start recording");
  record_button->setEnabled(true);
}
