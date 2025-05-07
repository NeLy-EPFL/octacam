#pragma once
#include "camera.h"
#include <QGraphicsPixmapItem>
#include <QMainWindow>

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
  void setupUi(int n_views, int n_rows = 2, int n_cols = -1);
  CameraSystem &camera_system;
  std::vector<QGraphicsPixmapItem *> pixmap_items;
};