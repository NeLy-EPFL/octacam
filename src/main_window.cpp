#include "main_window.h"

#include <QApplication>
#include <QGraphicsPixmapItem>
#include <QGraphicsScene>
#include <QGraphicsView>
#include <QMdiArea>
#include <QMdiSubWindow>
#include <QVBoxLayout>
#include <QWidget>

MainWindow::MainWindow(QWidget *parent) : QMainWindow(parent) { setupUi(8); }

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
    pixmap_item->setPixmap(
        QPixmap::fromImage(QImage(640, 480, QImage::Format_Grayscale8)));
    layout->addWidget(view);
    sub_window->setWidget(widget);
    sub_window->setWindowTitle(QString("Camera %1").arg(i + 1));
    mdi_area->addSubWindow(sub_window);
  }
}

void MainWindow::resizeEvent(QResizeEvent *event) {
  QMainWindow::resizeEvent(event);
  if (auto mdi_area = qobject_cast<QMdiArea *>(centralWidget())) {
    mdi_area->tileSubWindows();
  }
}
