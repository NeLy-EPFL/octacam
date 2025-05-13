#include "main_window.hpp"

#include <QChar>
#include <QComboBox>
#include <QDir>
#include <QDockWidget>
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
#include <QTimer>
#include <QVBoxLayout>
#include <cstdlib>
#include <ranges>
#include <stdexcept>

namespace {
constexpr std::chrono::nanoseconds DEFAULT_PREVIEW_INTERVAL_NS(33'000'000);
constexpr int DISPLAY_TIMER_INTERVAL_MS = 33;
constexpr int RECORD_COUNTDOWN_TIMER_INTERVAL_MS = 1000;
constexpr int CHECK_RECORD_STARTED_TIMER_INTERVAL_MS = 100;
constexpr int DOCK_MIN_WIDTH = 200;
constexpr int DOCK_MAX_WIDTH = 300;
constexpr int SAVE_DIR_EDIT_HEIGHT_FACTOR = 4;

constexpr int FPS_MIN = 0;
constexpr int FPS_MAX = 1000;
constexpr int DURATION_MIN_S = 0;
constexpr int DURATION_MAX_S = 359999;

constexpr long long MS_IN_HOUR = 3'600'000LL;
constexpr long long MS_IN_MINUTE = 60'000LL;
constexpr long long MS_IN_SECOND = 1000LL;

constexpr std::chrono::nanoseconds NS_IN_SECOND(1'000'000'000);
} // namespace

GraphicsView::GraphicsView(QWidget *parent) : QGraphicsView(parent) {}

GraphicsView::~GraphicsView() = default;

void GraphicsView::resizeEvent(QResizeEvent *event) {
  QGraphicsView::resizeEvent(event);
  fitInView(scene()->itemsBoundingRect(), Qt::KeepAspectRatio);
}

MainWindow::MainWindow(CameraSystem &camera_system, QWidget *parent)
    : QMainWindow(parent), camera_system(camera_system) {
  setup_ui();
}

MainWindow::~MainWindow() = default;

void MainWindow::setup_ui() {
  camera_system.start_software_trigger(DEFAULT_PREVIEW_INTERVAL_NS);
  setWindowTitle("huitacam");

  mdi_area = new QMdiArea(this);
  setCentralWidget(mdi_area);

  for (auto &camera : std::ranges::reverse_view(camera_system)) {
    auto *widget = new QWidget(this);
    auto *layout = new QVBoxLayout(widget);
    auto *view = new GraphicsView(widget);
    view->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    view->setVerticalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    view->setScene(new QGraphicsScene(view));
    auto *pixmap_item = new QGraphicsPixmapItem();
    pixmap_items.push_back(pixmap_item);
    view->scene()->addItem(pixmap_item);
    layout->addWidget(view);
    auto sub_window = mdi_area->addSubWindow(
        widget, Qt::WindowMinMaxButtonsHint | Qt::WindowTitleHint);
    QPixmap pixmap{1, 1};
    pixmap.fill(Qt::transparent);
    sub_window->setWindowIcon(QIcon{pixmap});
    auto title = QString::fromStdString(camera.get_serial_number());
    window_titles_.push_back(title);
    sub_window->setWindowTitle(title);
  }

  update_frames();

  auto display_timer = new QTimer(this);
  display_timer->setTimerType(Qt::CoarseTimer);
  display_timer->setInterval(DISPLAY_TIMER_INTERVAL_MS);
  connect(display_timer, &QTimer::timeout, this, &MainWindow::update_frames);
  display_timer->start();

  record_countdown_timer = new QTimer(this);
  connect(record_countdown_timer, &QTimer::timeout, this,
          &MainWindow::update_record_countdown);
  record_countdown_timer->setInterval(RECORD_COUNTDOWN_TIMER_INTERVAL_MS);

  check_record_started_timer = new QTimer(this);
  connect(check_record_started_timer, &QTimer::timeout, this,
          &MainWindow::check_record_started);
  check_record_started_timer->setInterval(
      CHECK_RECORD_STARTED_TIMER_INTERVAL_MS);

  auto *dock = new QDockWidget(this);
  dock->setAllowedAreas(Qt::RightDockWidgetArea);
  dock->setMinimumWidth(DOCK_MIN_WIDTH);
  dock->setMaximumWidth(DOCK_MAX_WIDTH);
  dock->setFeatures(dock->features() & ~QDockWidget::DockWidgetClosable);
  addDockWidget(Qt::RightDockWidgetArea, dock);

  auto *dock_content = new QWidget(dock);
  dock->setWidget(dock_content);

  auto *dock_layout = new QGridLayout(dock_content);
  dock_content->setLayout(dock_layout);
  int row = 0;

  dock_layout->addWidget(new QLabel("Duration (s):"), row, 0);
  duration_edit = new QLineEdit(dock_content);
  duration_edit->setValidator(
      new QIntValidator(DURATION_MIN_S, DURATION_MAX_S, this));
  duration_edit->setText("30");
  dock_layout->addWidget(duration_edit, row++, 1);

  dock_layout->addWidget(new QLabel("FPS:"), row, 0);
  fps_edit = new QLineEdit(dock_content);
  fps_edit->setValidator(new QIntValidator(FPS_MIN, FPS_MAX, this));
  fps_edit->setText("100");
  dock_layout->addWidget(fps_edit, row++, 1);

  dock_layout->addWidget(new QLabel("Save directory:"), row, 0);
  save_dir_edit = new DirectoryEdit(dock_content);
  save_dir_edit->setFixedHeight(fontMetrics().height() *
                                SAVE_DIR_EDIT_HEIGHT_FACTOR);
  dock_layout->addWidget(save_dir_edit, row++, 1);

  dock_layout->addWidget(new QLabel("Trigger source:"), row, 0);
  trigger_source_combo = new QComboBox(dock_content);
  trigger_source_combo->addItem("software");
  trigger_source_combo->addItem("external");
  dock_layout->addWidget(trigger_source_combo, row++, 1);

  dock_layout->addWidget(new QLabel("Video writer:"), row, 0);
  video_writer_combo = new QComboBox(dock_content);
  video_writer_combo->addItem("opencv MJPG avi");
  video_writer_combo->addItem("opencv avc1 mp4");
  dock_layout->addWidget(video_writer_combo, row++, 1);

  record_button = new QPushButton("Start recording", dock);
  connect(record_button, &QPushButton::clicked, this,
          &MainWindow::on_record_button_clicked);
  dock_layout->addWidget(record_button, row++, 0, 1, 2);

  status_label = new QLabel(dock_content);
  status_label->setText("");
  status_label->setAlignment(Qt::AlignCenter);
  dock_layout->addWidget(status_label, row++, 0, 1, 2);

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

  input_widgets.push_back(duration_edit);
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
        pixmap_item->setTransformOriginPoint(
            pixmap_item->boundingRect().center());
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
      item->setTransformOriginPoint(item->boundingRect().center());
      item->setRotation(item->rotation() + angle_delta);
    }
    view->fitInView(item->sceneBoundingRect(), Qt::KeepAspectRatio);
  }
}

void MainWindow::resizeEvent(QResizeEvent *event) {
  QMainWindow::resizeEvent(event);
  if (mdi_area) {
    mdi_area->tileSubWindows();
  }
}

void MainWindow::update_frames() {
  for (auto [pixmap_item, pixmap_fps_opt, sub_window, title] :
       std::views::zip(pixmap_items, camera_system.get_pixmaps_and_fps(),
                       mdi_area->subWindowList(), window_titles_)) {
    if (pixmap_item && pixmap_fps_opt) {
      auto [pixmap, fps] = *pixmap_fps_opt;
      pixmap_item->setPixmap(pixmap);

      auto new_title = title + QString(" | %1 fps").arg(fps, 6, 'f', 2);
      sub_window->setWindowTitle(new_title);
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
    record_button->setEnabled(true);
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

  int fps_val = 0;
  int duration_s_val = 0;
  try {
    fps_val = std::stoi(fps_edit->text().toStdString());
    duration_s_val = std::stoi(duration_edit->text().toStdString());
  } catch (const std::invalid_argument &ia) {
    QMessageBox::critical(this, "Error",
                          QString("Invalid number format: %1").arg(ia.what()));
    for (auto *widget : input_widgets) {
      widget->setEnabled(true);
    }
    record_button->setEnabled(true);
    return;
  } catch (const std::out_of_range &oor) {
    QMessageBox::critical(this, "Error",
                          QString("Number out of range: %1").arg(oor.what()));
    for (auto *widget : input_widgets) {
      widget->setEnabled(true);
    }
    record_button->setEnabled(true);
    return;
  }

  record_remaining_time_ = std::chrono::seconds(duration_s_val);
  const auto interval = NS_IN_SECOND / fps_val;
  const auto duration_ns = NS_IN_SECOND * duration_s_val;
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

  if (!fourcc_str.empty() && fourcc_str.length() == 4 &&
      !extension_str.empty()) {
    camera_system.start_record(save_dir, fps_val, fourcc_str, extension_str);
  } else {
    QMessageBox::warning(this, "Warning",
                         "Could not parse video writer format. Recording may "
                         "not use specified FOURCC/extension.");
  }

  if (use_software_trigger) {
    camera_system.start_software_trigger(interval, duration_ns);
  }

  check_record_started_timer->start();
}

void MainWindow::stop_record() {
  record_button->setEnabled(false);

  record_countdown_timer->stop();
  check_record_started_timer->stop();
  camera_system.stop_software_trigger();

  camera_system.start_preview();
  camera_system.start_software_trigger(DEFAULT_PREVIEW_INTERVAL_NS);
  save_dir_edit->increment();

  for (auto *widget : input_widgets) {
    widget->setEnabled(true);
  }

  record_button->setText("Start recording");
  record_button->setEnabled(true);

  status_label->setText("Recording stopped");
}