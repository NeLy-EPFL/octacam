#include "main_window.h"

#include <QApplication>
#include <QGraphicsPixmapItem>
#include <QGraphicsScene>
#include <QGraphicsView>
#include <QMdiArea>
#include <QMdiSubWindow>
#include <QTimer>
#include <QVBoxLayout>
#include <QWidget>

MainWindow::MainWindow(CameraSystem &camera_system, QWidget *parent)
    : QMainWindow(parent), camera_system(camera_system) {
  setupUi();
}

MainWindow::~MainWindow() {}

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
    pixmap_item->setPixmap(camera.get_pixmap());
    pixmap_items.push_back(pixmap_item);
    view->scene()->addItem(pixmap_item);
    layout->addWidget(view);
    auto sub_window =
        mdi_area->addSubWindow(widget, Qt::WindowMinMaxButtonsHint);
    QPixmap pixmap{1, 1};
    pixmap.fill(Qt::transparent);
    sub_window->setWindowIcon(QIcon{pixmap});
    sub_window->setWindowTitle(QString(camera.get_serial_number().c_str()));
  }

  QTimer *timer = new QTimer(this);
  connect(timer, &QTimer::timeout, this, &MainWindow::update_frames);
  timer->start(1000 / 30);
}

void MainWindow::resizeEvent(QResizeEvent *event) {
  QMainWindow::resizeEvent(event);
  if (auto mdi_area = qobject_cast<QMdiArea *>(centralWidget())) {
    mdi_area->tileSubWindows();
  }
}

void MainWindow::update_frames() {
  int i = 0;
  for (auto &camera : camera_system) {
    pixmap_items[i++]->setPixmap(camera.get_pixmap());
  }
}