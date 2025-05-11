#pragma once

#include "camera.h"
#include <QGraphicsPixmapItem>
#include <QGraphicsView>
#include <QMainWindow>
#include <QTimer>

class GraphicsView : public QGraphicsView {
  Q_OBJECT

public:
  explicit GraphicsView(QWidget *parent = nullptr);
  ~GraphicsView() override;

protected:
  void resizeEvent(QResizeEvent *event) override;
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
  void trigger_once();
  void on_record_button_clicked();

private:
  void setupUi();

  CameraSystem &camera_system;
  std::vector<QGraphicsPixmapItem *> pixmap_items;
  QTimer *display_trigger_timer;
  QTimer *record_trigger_timer;
};
