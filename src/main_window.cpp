#include "main_window.h"

#include <QApplication>
#include <QDockWidget>
#include <QGraphicsPixmapItem>
#include <QGraphicsScene>
#include <QGraphicsView>
#include <QGridLayout>
#include <QIntValidator>
#include <QLabel>
#include <QLineEdit>
#include <QMdiArea>
#include <QMdiSubWindow>
#include <QMessageBox>
#include <QPushButton>
#include <QWidget>
#include <ranges>

GraphicsView::GraphicsView(QWidget *parent) : QGraphicsView(parent) {}

GraphicsView::~GraphicsView() = default;

void GraphicsView::resizeEvent(QResizeEvent *event) {
  QGraphicsView::resizeEvent(event);
  fitInView(scene()->itemsBoundingRect(), Qt::KeepAspectRatio);
}

MainWindow::MainWindow(CameraSystem &camera_system, QWidget *parent)
    : QMainWindow(parent), camera_system(camera_system) {
  setupUi();
}

MainWindow::~MainWindow() = default;

void MainWindow::setupUi() {
  setWindowTitle("huitacam");

  QMdiArea *mdi_area = new QMdiArea(this);
  setCentralWidget(mdi_area);

  for (auto &camera : std::ranges::reverse_view(camera_system)) {
    auto *widget = new QWidget(this);
    auto *layout = new QVBoxLayout(widget);
    auto *view = new GraphicsView(widget);
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

  record_trigger_timer = new QTimer(this);
  record_trigger_timer->setTimerType(Qt::PreciseTimer);
  connect(record_trigger_timer, &QTimer::timeout, this,
          &MainWindow::trigger_once);

  display_trigger_timer = new QTimer(this);
  connect(display_trigger_timer, &QTimer::timeout, this,
          &MainWindow::trigger_once);
  display_trigger_timer->start(33);

  auto *display_timer = new QTimer(this);
  connect(display_timer, &QTimer::timeout, this, &MainWindow::update_frames);
  display_timer->start(33);

  auto *dock = new QDockWidget(this);
  dock->setAllowedAreas(Qt::RightDockWidgetArea);
  dock->setMinimumWidth(200);
  dock->setMaximumWidth(300);
  addDockWidget(Qt::RightDockWidgetArea, dock);

  auto *dock_content = new QWidget(dock);
  dock->setWidget(dock_content);

  auto *dock_layout = new QGridLayout(dock_content);
  dock_content->setLayout(dock_layout);

  dock_layout->addWidget(new QLabel("Duration (s):"), 0, 0);
  auto *duration_input = new QLineEdit(dock_content);
  duration_input->setValidator(new QIntValidator(0, 2147483647, this));
  duration_input->setText("30");
  dock_layout->addWidget(duration_input, 0, 1);

  dock_layout->addWidget(new QLabel("FPS:"), 1, 0);
  auto *fps_input = new QLineEdit(dock_content);
  fps_input->setValidator(new QIntValidator(0, 2147483647, this));
  fps_input->setText("100");
  dock_layout->addWidget(fps_input, 1, 1);

  dock_layout->addWidget(new QLabel("Save directory:"), 2, 0);
  save_dir_edit = new DirectoryEdit(dock_content);
  save_dir_edit->setFixedHeight(fontMetrics().height() * 4);
  dock_layout->addWidget(save_dir_edit, 2, 1);

  auto *record_button = new QPushButton("Start recording", dock);
  connect(record_button, &QPushButton::clicked, this,
          &MainWindow::on_record_button_clicked);
  dock_layout->addWidget(record_button, 3, 0, 1, 2);

  dock_layout->setRowStretch(4, 1);
}

void MainWindow::resizeEvent(QResizeEvent *event) {
  QMainWindow::resizeEvent(event);
  if (auto mdi_area = qobject_cast<QMdiArea *>(centralWidget())) {
    mdi_area->tileSubWindows();
  }
}

void MainWindow::trigger_once() { camera_system.trigger_once(); }

void MainWindow::update_frames() {
  for (auto [pixmap_item, pixmap] :
       std::views::zip(pixmap_items, camera_system.get_pixmaps())) {
    if (pixmap) {
      pixmap_item->setPixmap(*pixmap);
    }
  }
}

void MainWindow::on_record_button_clicked() {
  auto *button = qobject_cast<QPushButton *>(sender());
  if (button) {
    if (button->text() == "Start recording") {
      button->setEnabled(false);
      std::string save_dir = save_dir_edit->toPlainText().toStdString();

      bool success{false};

      if (std::filesystem::exists(save_dir)) {
        QMessageBox::critical(this, "Error",
                              "Could not create directory: " +
                                  QString::fromStdString(save_dir));
      } else if (std::filesystem::create_directories(save_dir)) {
        success = true;
      } else {
        QMessageBox::critical(this, "Error",
                              "Could not create directory: " +
                                  QString::fromStdString(save_dir));
      }

      if (!success) {
        button->setEnabled(true);
        return;
      }

      button->setText("Abort recording");
      display_trigger_timer->stop();
      camera_system.start_record();
      record_trigger_timer->start(33);
      button->setEnabled(true);
    } else {
      button->setEnabled(false);
      button->setText("Start recording");
      record_trigger_timer->stop();
      camera_system.start_preview();
      display_trigger_timer->start(33);
      button->setEnabled(true);
    }
  }
}