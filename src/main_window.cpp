#include "main_window.hpp"

#include <cstdlib>
#include <ranges>

#include <QApplication>
#include <QDockWidget>
#include <QFrame>
#include <QGraphicsPixmapItem>
#include <QGraphicsScene>
#include <QGraphicsView>
#include <QGridLayout>
#include <QIntValidator>
#include <QLabel>
#include <QMdiSubWindow>
#include <QMessageBox>
#include <QWidget>

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
  camera_system.start_software_trigger(std::chrono::nanoseconds(33000000));
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
    sub_window->setWindowTitle(QString(camera.get_serial_number().c_str()));
  }

  update_frames();

  auto display_timer = new QTimer(this);
  display_timer->setTimerType(Qt::CoarseTimer);
  display_timer->setInterval(33);
  connect(display_timer, &QTimer::timeout, this, &MainWindow::update_frames);
  display_timer->start();

  record_countdown_timer = new QTimer(this);
  connect(record_countdown_timer, &QTimer::timeout, this,
          &MainWindow::update_record_countdown);
  record_countdown_timer->setInterval(1000);

  check_record_started_timer = new QTimer(this);
  connect(check_record_started_timer, &QTimer::timeout, this,
          &MainWindow::check_record_started);
  check_record_started_timer->setInterval(100);

  auto *dock = new QDockWidget(this);
  dock->setAllowedAreas(Qt::RightDockWidgetArea);
  dock->setMinimumWidth(200);
  dock->setMaximumWidth(300);
  addDockWidget(Qt::RightDockWidgetArea, dock);

  auto *dock_content = new QWidget(dock);
  dock->setWidget(dock_content);

  auto *dock_layout = new QGridLayout(dock_content);
  dock_content->setLayout(dock_layout);
  int row = 0;

  dock_layout->addWidget(new QLabel("Duration (s):"), row, 0);
  duration_edit = new QLineEdit(dock_content);
  duration_edit->setValidator(new QIntValidator(0, 359999, this));
  duration_edit->setText("30");
  dock_layout->addWidget(duration_edit, row++, 1);

  dock_layout->addWidget(new QLabel("FPS:"), row, 0);
  fps_edit = new QLineEdit(dock_content);
  fps_edit->setValidator(new QIntValidator(0, 1000, this));
  fps_edit->setText("100");
  dock_layout->addWidget(fps_edit, row++, 1);

  dock_layout->addWidget(new QLabel("Save directory:"), row, 0);
  save_dir_edit = new DirectoryEdit(dock_content);
  save_dir_edit->setFixedHeight(fontMetrics().height() * 4);
  dock_layout->addWidget(save_dir_edit, row++, 1);

  dock_layout->addWidget(new QLabel("Trigger source:"), row, 0);
  trigger_source_combo = new QComboBox(dock_content);
  trigger_source_combo->addItem("software");
  trigger_source_combo->addItem("external");
  dock_layout->addWidget(trigger_source_combo, row++, 1);

  dock_layout->addWidget(new QLabel("Video writer:"), row, 0);
  video_writer_combo = new QComboBox(dock_content);
  video_writer_combo->addItem("opencv MJPG avi");
  video_writer_combo->addItem("opencv AVC1 mp4");
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
  auto *rotate_button = qobject_cast<QPushButton *>(sender());
  if (!rotate_button) {
    return;
  }

  if (rotate_all_button->isChecked()) {
    for (auto pixmap_item : pixmap_items) {
      if (rotate_button->text() == "↺") {
        pixmap_item->setTransformOriginPoint(
            pixmap_item->boundingRect().center());
        pixmap_item->setRotation(pixmap_item->rotation() - 90);
      } else if (rotate_button->text() == "↻") {
        pixmap_item->setTransformOriginPoint(
            pixmap_item->boundingRect().center());
        pixmap_item->setRotation(pixmap_item->rotation() + 90);
      } else if (rotate_button->text() == "Reset") {
        pixmap_item->setRotation(0);
      }
      auto view =
          qobject_cast<GraphicsView *>(pixmap_item->scene()->views().first());
      if (view) {
        view->fitInView(pixmap_item->sceneBoundingRect(), Qt::KeepAspectRatio);
      }
    }
    return;
  }

  auto active_sub_window = mdi_area->currentSubWindow();
  if (!active_sub_window) {
    return;
  }

  QGraphicsView *view =
      active_sub_window->widget()->findChild<QGraphicsView *>();
  if (!view) {
    return;
  }
  QGraphicsScene *scene = view->scene();
  if (!scene) {
    return;
  }

  for (auto item : scene->items()) {
    if (!item) {
      continue;
    }
    if (rotate_button->text() == "↺") {
      item->setTransformOriginPoint(item->boundingRect().center());
      item->setRotation(item->rotation() - 90);
    } else if (rotate_button->text() == "↻") {
      item->setTransformOriginPoint(item->boundingRect().center());
      item->setRotation(item->rotation() + 90);
    } else if (rotate_button->text() == "Reset") {
      item->setRotation(0);
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
  for (auto [pixmap_item, pixmap] :
       std::views::zip(pixmap_items, camera_system.get_pixmaps())) {
    if (pixmap) {
      pixmap_item->setPixmap(*pixmap);
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
  if (record_remaing_time_ms >= 0) {
    std::div_t result = std::div(record_remaing_time_ms, 3'600'000);
    auto hours = result.quot;
    result = std::div(result.rem, 60'000);
    auto minutes = result.quot;
    auto seconds = result.rem / 1000;

    status_label->setText(QString("Remaing time: %1:%2:%3")
                              .arg(hours, 2, 10, QChar(' '))
                              .arg(minutes, 2, 10, QChar('0'))
                              .arg(seconds, 2, 10, QChar('0')));
    record_remaing_time_ms -= record_countdown_timer->interval();
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

  for (auto widget : input_widgets) {
    widget->setEnabled(false);
  }

  std::string save_dir = save_dir_edit->toPlainText().toStdString();

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
    for (auto widget : input_widgets) {
      widget->setEnabled(true);
    }
    return;
  }

  auto fps = std::stoi(fps_edit->text().toStdString());
  auto duration_s = std::stoi(duration_edit->text().toStdString());
  record_remaing_time_ms = duration_s * 1000;
  auto interval = std::chrono::nanoseconds(1000000000) / fps;
  auto duration = std::chrono::nanoseconds(1000000000) * duration_s;
  auto video_writer_info = video_writer_combo->currentText().toStdString();

  std::string fourcc = "";
  std::string extension = "";

  if (video_writer_info.starts_with("opencv")) {
    fourcc = video_writer_info.substr(7, 4);
    extension = video_writer_info.substr(12, 3);
  } else {
    // Handle other video writer types if needed
  }

  bool use_software_trigger = trigger_source_combo->currentText() == "software";

  camera_system.stop_software_trigger();

  if (!fourcc.empty()) {
    camera_system.start_record(save_dir, fps, fourcc, extension);
  }

  if (use_software_trigger) {
    camera_system.start_software_trigger(interval, duration);
  }

  check_record_started_timer->start();
}

void MainWindow::stop_record() {
  record_button->setEnabled(false);

  record_countdown_timer->stop();
  camera_system.stop_software_trigger();

  camera_system.start_preview();
  camera_system.start_software_trigger(std::chrono::nanoseconds(33000000));
  save_dir_edit->increment();

  for (auto widget : input_widgets) {
    widget->setEnabled(true);
  }

  record_button->setText("Start recording");
  record_button->setEnabled(true);

  status_label->setText("Recording stopped");
}