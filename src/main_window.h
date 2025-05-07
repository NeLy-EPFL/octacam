#pragma once
#include "camera.h"
#include <QMainWindow>

class MainWindow : public QMainWindow {
  Q_OBJECT

public:
  explicit MainWindow(const CameraSystem &camera_system,
                      QWidget *parent = nullptr);
  ~MainWindow();

protected:
  void resizeEvent(QResizeEvent *event) override;

private:
  void setupUi(int n_views, int n_rows = 2, int n_cols = -1);
  const CameraSystem &camera_system;
};