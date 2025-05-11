#pragma once

#include "camera.h"
#include <QGraphicsPixmapItem>
#include <QGraphicsView>
#include <QKeyEvent>
#include <QMainWindow>
#include <QTextEdit>
#include <QTimer>

class DirectoryEdit : public QTextEdit {
  Q_OBJECT

public:
  explicit DirectoryEdit(QWidget *parent = nullptr) : QTextEdit(parent) {}

protected:
  void keyPressEvent(QKeyEvent *event) override {
    if (event->key() == Qt::Key_Return || event->key() == Qt::Key_Enter) {
      event->ignore();
      return;
    }
    QTextEdit::keyPressEvent(event);
  }
};

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
