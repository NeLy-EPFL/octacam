#include "main_window.h"

#include <QApplication>
#include <QDockWidget>
#include <QGraphicsPixmapItem>
#include <QGraphicsScene>
#include <QGraphicsView>
#include <QMdiArea>
#include <QMdiSubWindow>
#include <QPushButton>
#include <QVBoxLayout>
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

  for (auto &camera : camera_system) {
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
  display_trigger_timer->start(1000 / 30);

  auto *display_timer = new QTimer(this);
  connect(display_timer, &QTimer::timeout, this, &MainWindow::update_frames);
  display_timer->start(1000 / 30);

  auto *right_dock = new QDockWidget(this);
  right_dock->setAllowedAreas(Qt::RightDockWidgetArea);
  auto *dock_content = new QWidget(right_dock);
  auto *dock_layout = new QVBoxLayout(dock_content);
  dock_content->setLayout(dock_layout);
  right_dock->setWidget(dock_content);
  addDockWidget(Qt::RightDockWidgetArea, right_dock);
  right_dock->setMinimumWidth(400);

  auto *record_button = new QPushButton("Start recording", right_dock);
  connect(record_button, &QPushButton::clicked, this,
          &MainWindow::on_record_button_clicked);
  dock_layout->addWidget(record_button);
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
      button->setText("Abort recording");
      display_trigger_timer->stop();
      camera_system.start_record();
      record_trigger_timer->start(1000 / 30);
    } else {
      camera_system.abort_record();
      button->setText("Start recording");
    }
  }
}