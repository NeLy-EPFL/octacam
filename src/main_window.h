#pragma once

#include "camera.h"
#include <QDir>
#include <QGraphicsPixmapItem>
#include <QGraphicsView>
#include <QKeyEvent>
#include <QMainWindow>
#include <QPlainTextEdit>
#include <QTimer>
#include <filesystem>

class DirectoryEdit : public QPlainTextEdit {
  Q_OBJECT

public:
  explicit DirectoryEdit(QWidget *parent = nullptr) : QPlainTextEdit(parent) {
    auto now = std::chrono::system_clock::now();
    auto now_time_t = std::chrono::system_clock::to_time_t(now);
    std::tm *now_tm = std::localtime(&now_time_t);
    char date_str[7];
    std::strftime(date_str, sizeof(date_str), "%y%m%d", now_tm);
    QString defaultPath = QString("~/data/TL/%1/fly1/001").arg(date_str);
    setPlainText(defaultPath);
  }

  virtual void setPlainText(const QString &text) {
    QString inputPath = text.trimmed();
    if (inputPath.startsWith("~")) {
      inputPath.replace(0, 1, QDir::homePath());
    }
    QDir dir(inputPath);
    QString absPath = dir.absolutePath().replace('\\', '/');
    QPlainTextEdit::setPlainText(absPath);
  }

protected:
  void keyPressEvent(QKeyEvent *event) override {
    if (event->key() == Qt::Key_Return || event->key() == Qt::Key_Enter) {
      setPlainText(toPlainText());
      event->accept();
      return;
    }
    QPlainTextEdit::keyPressEvent(event);
  }
  void focusOutEvent(QFocusEvent *event) override {
    setPlainText(toPlainText());
    QPlainTextEdit::focusOutEvent(event);
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
  DirectoryEdit *save_dir_edit;
};
