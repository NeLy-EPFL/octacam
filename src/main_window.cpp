#include "main_window.h"

#include <QApplication>
#include <QGraphicsPixmapItem>
#include <QGraphicsView>
#include <QSplitter>

MainWindow::MainWindow(QWidget *parent) : QMainWindow(parent) { setupUi(7); }

MainWindow::~MainWindow() {}

void MainWindow::setupUi(int n_views, int n_rows, int n_cols) {
  setWindowTitle("huitacam");

  if (n_rows < 0 && n_cols < 0) {
    throw std::invalid_argument("Either n_rows or n_cols must be set");
  } else if (n_cols <= 0) {
    n_cols = (n_views + n_rows - 1) / n_rows;
  } else {
    n_rows = (n_views + n_cols - 1) / n_cols;
  }

  QSplitter *splitter = new QSplitter(Qt::Orientation::Horizontal, this);
  setCentralWidget(splitter);

  for (auto i = 0; i < n_cols; ++i) {
    auto *splitter_i = new QSplitter(Qt::Orientation::Vertical, splitter);
    splitter->addWidget(splitter_i);
    for (auto j = 0; j < n_rows; ++j) {
      if (i * n_rows + j >= n_views) {
        break;
      }
      auto *view = new QGraphicsView(splitter_i);
      view->setScene(new QGraphicsScene(view));
      auto pixmap_item = new QGraphicsPixmapItem();
      splitter_i->addWidget(view);
    }
  }
}