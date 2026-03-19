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
#include <QGroupBox>
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
#include <QTabWidget>
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
  if (!scene()) {
    return;
  }
  fitInView(scene()->itemsBoundingRect(), Qt::KeepAspectRatio);
}

MainWindow::MainWindow(CameraSystem &camera_system, OctacamConfig config,
                       SerialPort &serial_port, QWidget *parent)
    : QMainWindow(parent), camera_system(camera_system), config(config),
      serial_port(serial_port) {
  for (const auto &camera_config : this->config.camera_configs) {
    camera_config_by_serial_.emplace(camera_config.serial_number,
                                     camera_config);
  }
  setup_ui();
}

MainWindow::~MainWindow() = default;

inline std::chrono::nanoseconds fps_to_ns(double fps) {
  return std::chrono::nanoseconds(
      static_cast<long long>(std::round(1.0e9 / fps)));
}

void MainWindow::setup_ui() {
  auto &cfg = config.gui_config;
  const auto find_camera_config =
      [this](const std::string &serial_number) -> const CameraConfig * {
    auto it = camera_config_by_serial_.find(serial_number);
    if (it == camera_config_by_serial_.end()) {
      return nullptr;
    }
    return &it->second;
  };

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

    const CameraConfig *camera_config = find_camera_config(serial_number);

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
    if (camera_config) {
      transform.scale(camera_config->scale_x, camera_config->scale_y);
      transform.rotate(camera_config->rotation_deg);
    }
    pixmap_item->setTransform(transform);

    auto sub_window = mdi_area->addSubWindow(
        widget, Qt::WindowMinMaxButtonsHint | Qt::WindowTitleHint);
    QPixmap pixmap{1, 1};
    pixmap.fill(Qt::transparent);
    sub_window->setWindowIcon(QIcon{pixmap});
    auto title = QString::fromStdString(camera.get_name());
    sub_window->setWindowTitle(title);

    if (camera_config && camera_config->window_x >= 0 &&
        camera_config->window_y >= 0) {
      tile = false;
    }
    if (camera_config && camera_config->window_width > 0 &&
        camera_config->window_height > 0) {
      tile = false;
    }
    --i;
  }

  if (tile) {
    mdi_area->tileSubWindows();
  }

  step_cw_timer = new QTimer(this);
  step_cw_timer->setTimerType(Qt::PreciseTimer);
  step_cw_timer->setInterval(1);
  connect(step_cw_timer, &QTimer::timeout, this,
          [this]() { serial_port.write_all(Command{1}); });

  step_ccw_timer = new QTimer(this);
  step_ccw_timer->setTimerType(Qt::PreciseTimer);
  step_ccw_timer->setInterval(1);
  connect(step_ccw_timer, &QTimer::timeout, this,
          [this]() { serial_port.write_all(Command{-1}); });

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

  auto *dock_layout = new QVBoxLayout(dock_content);
  dock_layout->setContentsMargins(0, 0, 0, 0);
  dock_layout->setSpacing(0);
  dock_content->setLayout(dock_layout);

  auto *tabs = new QTabWidget(dock_content);
  dock_layout->addWidget(tabs);

  int margin = 4;

  auto *record_tab = new QWidget(tabs);
  auto *record_layout = new QGridLayout(record_tab);
  record_layout->setContentsMargins(margin, margin, margin, margin);
  record_layout->setHorizontalSpacing(8);
  record_layout->setVerticalSpacing(6);
  record_tab->setLayout(record_layout);

  int row = 0;

  auto *duration_label = new QLabel("Duration:", record_tab);
  duration_label->setAlignment(Qt::AlignRight);
  record_layout->addWidget(duration_label, row, 0, 1, 1);
  duration_input = new DurationInput(
      cfg.duration_default, cfg.duration_min, cfg.duration_max,
      cfg.duration_unit_default_index, record_tab);
  record_layout->addWidget(duration_input, row++, 1, 1, 1);

  auto *fps_label = new QLabel("FPS:", record_tab);
  fps_label->setAlignment(Qt::AlignRight);
  record_layout->addWidget(fps_label, row, 0, 1, 1);
  fps_edit = new QDoubleSpinBox(record_tab);
  fps_edit->setRange(cfg.fps_min, cfg.fps_max);
  fps_edit->setValue(cfg.fps_default);
  fps_edit->setDecimals(2);
  fps_edit->setSingleStep(1.0);
  connect(fps_edit, &QDoubleSpinBox::valueChanged, this,
          &MainWindow::on_fps_value_changed);
  record_layout->addWidget(fps_edit, row++, 1, 1, 1);

  auto *save_dir_button = new QPushButton("Save directory:", record_tab);
  record_layout->addWidget(save_dir_button, row, 0, 1, 1,
                           Qt::AlignRight | Qt::AlignTop);
  save_dir_edit = new DirectoryEdit(cfg.save_directory_default, record_tab);
  save_dir_edit->setFixedHeight(fontMetrics().height() *
                                cfg.save_dir_edit_height_factor);
  record_layout->addWidget(save_dir_edit, row++, 1, 1, 1);
  connect(save_dir_button, &QPushButton::clicked, this, [this]() {
    const QString start_dir =
        save_dir_edit ? save_dir_edit->toPlainText() : QString{};
    const QString selected_dir = QFileDialog::getExistingDirectory(
        this, "Select save directory", start_dir,
        QFileDialog::ShowDirsOnly | QFileDialog::DontResolveSymlinks);
    if (!selected_dir.isEmpty() && save_dir_edit) {
      save_dir_edit->setPlainText(selected_dir);
    }
  });

  auto *trigger_source_label = new QLabel("Trigger source:", record_tab);
  trigger_source_label->setAlignment(Qt::AlignRight);
  record_layout->addWidget(trigger_source_label, row, 0, 1, 1);
  trigger_source_combo = new QComboBox(record_tab);
  trigger_source_combo->addItem("software");
  trigger_source_combo->addItem("external");
  trigger_source_combo->setCurrentIndex(cfg.trigger_source_default_index);
  record_layout->addWidget(trigger_source_combo, row++, 1, 1, 1);

  auto *video_writer_label = new QLabel("Video writer:", record_tab);
  video_writer_label->setAlignment(Qt::AlignRight);
  record_layout->addWidget(video_writer_label, row, 0, 1, 1);
  video_writer_combo = new QComboBox(record_tab);
  video_writer_combo->addItem("opencv MJPG avi");
  video_writer_combo->addItem("opencv avc1 mp4");
  video_writer_combo->setCurrentIndex(cfg.video_writer_default_index);
  record_layout->addWidget(video_writer_combo, row++, 1, 1, 1);

  record_button = new QPushButton("Start recording", record_tab);
  connect(record_button, &QPushButton::clicked, this,
          &MainWindow::on_record_button_clicked);
  record_layout->addWidget(record_button, row++, 0, 1, 2);

  status_label = new QLabel(record_tab);
  status_label->setText("");
  status_label->setAlignment(Qt::AlignCenter);
  status_label->setWordWrap(true);
  record_layout->addWidget(status_label, row++, 0, 1, 2);

  record_layout->setRowStretch(row, 1);

  tabs->addTab(record_tab, "Record");

  if (serial_port.is_open()) {
    auto *arduino_tab = new QWidget(tabs);
    auto *arduino_layout = new QGridLayout(arduino_tab);
    // arduino_layout->setContentsMargins(margin, margin, margin, margin);
    // arduino_layout->setSpacing(8);
    arduino_tab->setLayout(arduino_layout);

    auto *step_title = new QLabel("Loop", arduino_tab);
    step_title->setAlignment(Qt::AlignCenter);
    arduino_layout->addWidget(step_title, 0, 0, 1, 3);

    step_info_label = new QLabel(arduino_tab);
    step_info_label->setAlignment(Qt::AlignCenter);

    step_init_ccw_button = new QRadioButton("↺", arduino_tab);
    step_init_cw_button = new QRadioButton("↻", arduino_tab);
    step_init_ccw_button->setChecked(true);

    step_count_spinbox = new QSpinBox(arduino_tab);
    step_count_spinbox->setRange(2, 32767);
    step_count_spinbox->setValue(4096);
    connect(step_count_spinbox, &QSpinBox::valueChanged, this,
            &MainWindow::update_step_info);

    step_interval_us_spinbox = new QSpinBox(arduino_tab);
    step_interval_us_spinbox->setRange(800, 65535);
    step_interval_us_spinbox->setValue(1465);
    step_interval_us_spinbox->setSuffix(" μs");
    connect(step_interval_us_spinbox, &QSpinBox::valueChanged, this,
            &MainWindow::update_step_info);

    step_rest_ms_spinbox = new QSpinBox(arduino_tab);
    step_rest_ms_spinbox->setRange(0, 65535);
    step_rest_ms_spinbox->setValue(1000);
    step_rest_ms_spinbox->setSuffix(" ms");
    connect(step_rest_ms_spinbox, &QSpinBox::valueChanged, this,
            &MainWindow::update_step_info);

    step_repeats_spinbox = new QSpinBox(arduino_tab);
    step_repeats_spinbox->setRange(1, 255);
    step_repeats_spinbox->setValue(3);
    connect(step_repeats_spinbox, &QSpinBox::valueChanged, this,
            &MainWindow::update_step_info);

    step_init_wait_s_spinbox = new QSpinBox(arduino_tab);
    step_init_wait_s_spinbox->setRange(0, 255);
    step_init_wait_s_spinbox->setValue(10);
    step_init_wait_s_spinbox->setSuffix(" s");
    connect(step_init_wait_s_spinbox, &QSpinBox::valueChanged, this,
            &MainWindow::update_step_info);

    auto *step_execute_button = new QPushButton("Execute", arduino_tab);
    connect(step_execute_button, &QPushButton::clicked, this,
            &MainWindow::on_step_execute_button_clicked);

    auto *step_init_direction_label =
        new QLabel("Initial direction:", arduino_tab);
    step_init_direction_label->setAlignment(Qt::AlignRight);
    arduino_layout->addWidget(step_init_direction_label, 1, 0, 1, 1);
    arduino_layout->addWidget(step_init_ccw_button, 1, 1, 1, 1);
    arduino_layout->addWidget(step_init_cw_button, 1, 2, 1, 1);

    auto *step_count_label = new QLabel("Steps:", arduino_tab);
    step_count_label->setAlignment(Qt::AlignRight);
    arduino_layout->addWidget(step_count_label, 2, 0, 1, 1);
    arduino_layout->addWidget(step_count_spinbox, 2, 1, 1, 2);

    auto *step_interval_label = new QLabel("Step interval:", arduino_tab);
    step_interval_label->setAlignment(Qt::AlignRight);
    arduino_layout->addWidget(step_interval_label, 3, 0, 1, 1);
    arduino_layout->addWidget(step_interval_us_spinbox, 3, 1, 1, 2);

    auto *step_rest_label = new QLabel("Rest duration:", arduino_tab);
    step_rest_label->setAlignment(Qt::AlignRight);
    arduino_layout->addWidget(step_rest_label, 4, 0, 1, 1);
    arduino_layout->addWidget(step_rest_ms_spinbox, 4, 1, 1, 2);

    auto *step_repeats_label = new QLabel("Repeats:", arduino_tab);
    step_repeats_label->setAlignment(Qt::AlignRight);
    arduino_layout->addWidget(step_repeats_label, 5, 0, 1, 1);
    arduino_layout->addWidget(step_repeats_spinbox, 5, 1, 1, 2);

    auto *step_init_wait_label = new QLabel("Initial wait:", arduino_tab);
    step_init_wait_label->setAlignment(Qt::AlignRight);
    arduino_layout->addWidget(step_init_wait_label, 6, 0, 1, 1);
    arduino_layout->addWidget(step_init_wait_s_spinbox, 6, 1, 1, 2);

    arduino_layout->addWidget(step_info_label, 7, 0, 1, 3);
    arduino_layout->addWidget(step_execute_button, 8, 0, 1, 3);

    step_start_with_recording_checkbox =
        new QCheckBox("Start with recording", arduino_tab);
    step_start_with_recording_checkbox->setChecked(true);
    arduino_layout->addWidget(step_start_with_recording_checkbox, 9, 0, 1, 3,
                              Qt::AlignCenter);

    arduino_layout->setRowStretch(10, 1);

    auto *single_step_title = new QLabel("Adjust position", arduino_tab);
    single_step_title->setAlignment(Qt::AlignCenter);
    arduino_layout->addWidget(single_step_title, 11, 0, 1, 3);

    auto *single_step_ccw_button = new QPushButton("↺", arduino_tab);
    auto *single_step_cw_button = new QPushButton("↻", arduino_tab);
    single_step_interval_edit = new QSpinBox(arduino_tab);
    single_step_interval_edit->setRange(1, 1000);
    single_step_interval_edit->setValue(1);
    single_step_interval_edit->setSuffix(" ms");

    connect(single_step_ccw_button, &QPushButton::pressed, this,
            &MainWindow::on_single_step_ccw_button_pressed);
    connect(single_step_ccw_button, &QPushButton::released, this,
            &MainWindow::on_single_step_ccw_button_released);
    connect(single_step_cw_button, &QPushButton::pressed, this,
            &MainWindow::on_single_step_cw_button_pressed);
    connect(single_step_cw_button, &QPushButton::released, this,
            &MainWindow::on_single_step_cw_button_released);

    auto *single_step_interval_label = new QLabel("Interval:", arduino_tab);
    single_step_interval_label->setAlignment(Qt::AlignRight);
    arduino_layout->addWidget(single_step_interval_label, 12, 0, 1, 1);
    arduino_layout->addWidget(single_step_interval_edit, 12, 1, 1, 2);

    auto *single_step_direction_label = new QLabel("Direction:", arduino_tab);
    single_step_direction_label->setAlignment(Qt::AlignRight);
    arduino_layout->addWidget(single_step_direction_label, 13, 0, 1, 1);
    arduino_layout->addWidget(single_step_ccw_button, 13, 1, 1, 1);
    arduino_layout->addWidget(single_step_cw_button, 13, 2, 1, 1);

    tabs->addTab(arduino_tab, "Arduino");

    update_step_info();
  }

  auto *view_tab = new QWidget(tabs);
  auto *view_layout = new QGridLayout(view_tab);
  view_layout->setContentsMargins(margin, margin, margin, margin);
  view_layout->setSpacing(8);
  view_tab->setLayout(view_layout);

  view_layout->addWidget(new QLabel("Apply to:", view_tab), 0, 0, 1, 1);

  transform_selected_button = new QRadioButton("Selected", view_tab);
  view_layout->addWidget(transform_selected_button, 0, 1, 1, 1);

  transform_all_button = new QRadioButton("All", view_tab);
  view_layout->addWidget(transform_all_button, 0, 2, 1, 1);

  transform_all_button->setChecked(true);

  view_layout->addWidget(new QLabel("Rotate:", view_tab), 1, 0, 1, 1);

  auto *rotate_ccw_button = new QPushButton("↺", view_tab);
  view_layout->addWidget(rotate_ccw_button, 1, 1, 1, 1);
  connect(rotate_ccw_button, &QPushButton::clicked, this,
          &MainWindow::rotate_displays);

  auto *rotate_cw_button = new QPushButton("↻", view_tab);
  view_layout->addWidget(rotate_cw_button, 1, 2, 1, 1);
  connect(rotate_cw_button, &QPushButton::clicked, this,
          &MainWindow::rotate_displays);

  view_layout->addWidget(new QLabel("Flip:", view_tab), 2, 0, 1, 1);

  auto *hflip_button = new QPushButton("Horizontal", view_tab);
  view_layout->addWidget(hflip_button, 2, 1, 1, 1);
  connect(hflip_button, &QPushButton::clicked, this,
          &MainWindow::rotate_displays);

  auto *vflip_button = new QPushButton("Vertical", view_tab);
  view_layout->addWidget(vflip_button, 2, 2, 1, 1);
  connect(vflip_button, &QPushButton::clicked, this,
          &MainWindow::rotate_displays);

  auto *reset_transformation = new QPushButton("Reset", view_tab);
  view_layout->addWidget(reset_transformation, 3, 0, 1, 3);
  connect(reset_transformation, &QPushButton::clicked, this,
          &MainWindow::rotate_displays);

  view_layout->setRowStretch(4, 1);

  tabs->addTab(view_tab, "View");

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
  bool hflip = false;
  bool vflip = false;

  if (button_text == "↺") {
    angle_delta = -90;
  } else if (button_text == "↻") {
    angle_delta = 90;
  } else if (button_text == "Horizontal") {
    hflip = true;
  } else if (button_text == "Vertical") {
    vflip = true;
  } else if (button_text == "Reset") {
    reset_rotation = true;
  } else {
    return;
  }

  if (transform_all_button->isChecked()) {
    for (auto *pixmap_item : pixmap_items) {
      if (!pixmap_item)
        continue;

      pixmap_item->setTransformOriginPoint(
          pixmap_item->boundingRect().center());

      if (reset_rotation) {
        pixmap_item->setRotation(0);
        pixmap_item->setTransform(QTransform());
      } else {
        pixmap_item->setRotation(pixmap_item->rotation() + angle_delta);
        if (hflip || vflip) {
          auto center = pixmap_item->boundingRect().center();
          QTransform flip;
          flip.translate(center.x(), center.y());
          flip.scale(hflip ? -1.0 : 1.0, vflip ? -1.0 : 1.0);
          flip.translate(-center.x(), -center.y());
          pixmap_item->setTransform(flip, true);
        }
      }

      auto *scene = pixmap_item->scene();
      if (!scene || scene->views().isEmpty()) {
        continue;
      }

      if (auto *view = qobject_cast<GraphicsView *>(scene->views().first())) {
        const QRectF content_bounds = scene->itemsBoundingRect();
        view->fitInView(content_bounds, Qt::KeepAspectRatio);
        view->centerOn(content_bounds.center());
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

    item->setTransformOriginPoint(item->boundingRect().center());

    if (reset_rotation) {
      item->setRotation(0);
      item->setTransform(QTransform());
    } else {
      item->setRotation(item->rotation() + angle_delta);
      if (hflip || vflip) {
        auto center = item->boundingRect().center();
        QTransform flip;
        flip.translate(center.x(), center.y());
        flip.scale(hflip ? -1.0 : 1.0, vflip ? -1.0 : 1.0);
        flip.translate(-center.x(), -center.y());
        item->setTransform(flip, true);
      }
    }
  }

  const QRectF content_bounds = view->scene()->itemsBoundingRect();
  view->fitInView(content_bounds, Qt::KeepAspectRatio);
  view->centerOn(content_bounds.center());
}

void MainWindow::resizeEvent(QResizeEvent *event) {
  QMainWindow::resizeEvent(event);
  if (!tile && mdi_area) {
    const auto find_camera_config =
        [this](const std::string &serial_number) -> const CameraConfig * {
      auto it = camera_config_by_serial_.find(serial_number);
      if (it == camera_config_by_serial_.end()) {
        return nullptr;
      }
      return &it->second;
    };

    int i = 0;
    for (auto &camera : std::ranges::reverse_view(camera_system)) {
      auto serial_number = camera.get_serial_number();
      const CameraConfig *camera_config = find_camera_config(serial_number);

      auto *sub_window = mdi_area->subWindowList().at(i++);
      if (camera_config && camera_config->window_x >= 0 &&
          camera_config->window_y >= 0) {
        sub_window->move(static_cast<int>(std::round(camera_config->window_x *
                                                     mdi_area->width())),
                         static_cast<int>(std::round(camera_config->window_y *
                                                     mdi_area->height())));
      }
      if (camera_config && camera_config->window_width > 0 &&
          camera_config->window_height > 0) {
        sub_window->resize(
            static_cast<int>(
                std::round(camera_config->window_width * mdi_area->width())),
            static_cast<int>(
                std::round(camera_config->window_height * mdi_area->height())));
      }
    }
  }
}

void MainWindow::update_frames() {
  auto frames_and_fps = camera_system.get_mats_and_fps();
  for (size_t i = 0; i < frames_and_fps.size(); ++i) {
    auto *pixmap_item = pixmap_items[i];
    auto *fps_label = fps_labels[i];

    if (pixmap_item) {
      auto &[mat, fps] = frames_and_fps[i];
      if (!mat) {
        continue;
      }
      QImage image(mat->data, mat->cols, mat->rows,
                   static_cast<int>(mat->step[0]), QImage::Format_Grayscale8);
      QPixmap pixmap = QPixmap::fromImage(image);
      pixmap_item->setPixmap(pixmap);
      fps_label->setText(QString("%1 fps").arg(fps, 6, 'f', 2));
    }
  }
}

void MainWindow::check_record_started() {
  if (camera_system.all_cameras_started()) {
    if (step_start_with_recording_checkbox &&
        step_start_with_recording_checkbox->isChecked()) {
      on_step_execute_button_clicked();
    }
    check_record_started_timer->stop();
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
  if (record_button->text() == "Start recording") {
    start_record();
  } else {
    stop_record();
  }
}

void MainWindow::on_single_step_ccw_button_pressed() {
  step_ccw_timer->setInterval(single_step_interval_edit->value());
  step_ccw_timer->start();
}

void MainWindow::on_single_step_ccw_button_released() {
  step_ccw_timer->stop();
  serial_port.write_all(Command{0});
}

void MainWindow::on_single_step_cw_button_pressed() {
  step_cw_timer->setInterval(single_step_interval_edit->value());
  step_cw_timer->start();
}

void MainWindow::on_single_step_cw_button_released() {
  step_cw_timer->stop();
  serial_port.write_all(Command{0});
}

void MainWindow::on_step_execute_button_clicked() {
  int direction =
      step_init_cw_button && step_init_cw_button->isChecked() ? 1 : -1;
  int16_t steps = static_cast<int16_t>(direction * step_count_spinbox->value());
  uint16_t interval = static_cast<uint16_t>(step_interval_us_spinbox->value());
  uint16_t rest_duration = static_cast<uint16_t>(step_rest_ms_spinbox->value());
  uint8_t repeats = static_cast<uint8_t>(step_repeats_spinbox->value());
  uint8_t init_wait_duration =
      static_cast<uint8_t>(step_init_wait_s_spinbox->value());
  serial_port.write_all(
      Command{steps, interval, rest_duration, repeats, init_wait_duration});
}

void MainWindow::update_step_info() {
  uint64_t interval_us =
      static_cast<uint64_t>(step_interval_us_spinbox->value());
  uint64_t rest_duration_us =
      static_cast<uint64_t>(step_rest_ms_spinbox->value()) * 1000;
  uint64_t init_wait_duration_us =
      static_cast<uint64_t>(step_init_wait_s_spinbox->value()) * 1000000;
  uint64_t n_repeats = static_cast<uint64_t>(step_repeats_spinbox->value());
  uint64_t n_steps = static_cast<uint64_t>(step_count_spinbox->value());
  uint64_t duration_us = interval_us * n_steps;
  uint64_t n_steps_per_rev = 4096;
  long double rpm =
      60'000'000.0L / static_cast<long double>(n_steps_per_rev * interval_us);
  uint64_t total_duration_us =
      (duration_us + rest_duration_us) * n_repeats * 2 + init_wait_duration_us -
      rest_duration_us;
  long double total_duration_s =
      static_cast<long double>(total_duration_us) / 1'000'000.0L;
  step_info_label->setText(QString("Total duration: %1 s, RPM: %2")
                               .arg(total_duration_s, 0, 'f', 3)
                               .arg(rpm, 0, 'f', 3));
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

  if (fourcc_str.size() != 4 || extension_str.empty()) {
    QMessageBox::warning(this, "Warning",
                         "Could not parse video writer format. Recording was "
                         "not started.");
    for (auto *widget : input_widgets) {
      widget->setEnabled(true);
    }
    record_button->setEnabled(true);
    return;
  }

  const bool use_software_trigger =
      trigger_source_combo->currentText() == "software";

  camera_system.stop_software_trigger();

  if (!use_software_trigger) {
    camera_system.set_trigger_source(false);
  }

  camera_system.start_record(save_dir, fps_val, fourcc_str, extension_str);

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
