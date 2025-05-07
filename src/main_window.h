#pragma once
#include "camera.h"
#include <QGraphicsPixmapItem>
#include <QGraphicsView>
#include <QMainWindow>

class GraphicsView : public QGraphicsView {
  Q_OBJECT

public:
  explicit GraphicsView(QWidget *parent = nullptr) : QGraphicsView(parent) {}
  ~GraphicsView() override = default;

protected:
  void resizeEvent(QResizeEvent *event) override {
    QGraphicsView::resizeEvent(event);
    fitInView(scene()->itemsBoundingRect(), Qt::KeepAspectRatio);
  }
};

class MainWindow : public QMainWindow {
  Q_OBJECT

public:
  explicit MainWindow(CameraSystem &camera_system, QWidget *parent = nullptr);
  ~MainWindow();

protected:
  void resizeEvent(QResizeEvent *event) override;

private slots:
  void update_frames();

private:
  void setupUi();
  CameraSystem &camera_system;
  std::vector<QGraphicsPixmapItem *> pixmap_items;
  std::vector<QGraphicsView *> views;
};
