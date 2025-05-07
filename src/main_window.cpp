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
  setupUi(8);
}

MainWindow::~MainWindow() {}

void MainWindow::setupUi(int n_views, int n_rows, int n_cols) {
  setWindowTitle("huitacam");

  QMdiArea *mdi_area = new QMdiArea(this);
  setCentralWidget(mdi_area);

  for (int i = 0; i < n_views; ++i) {
    auto *sub_window = new QMdiSubWindow(mdi_area);
    auto *widget = new QWidget(mdi_area);
    auto *layout = new QVBoxLayout(widget);
    auto *view = new QGraphicsView(widget);
    view->setScene(new QGraphicsScene(view));
    auto *pixmap_item = new QGraphicsPixmapItem();
    pixmap_items.push_back(pixmap_item);
    view->scene()->addItem(pixmap_item);
    layout->addWidget(view);
    sub_window->setWidget(widget);
    sub_window->setWindowTitle(QString("Camera %1").arg(i + 1));
    mdi_area->addSubWindow(sub_window);
  }

  QTimer *timer = new QTimer(this);
  connect(timer, &QTimer::timeout, this, &MainWindow::update_frames);
  timer->start(1000); // 30 FPS
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