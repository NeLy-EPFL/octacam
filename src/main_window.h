#pragma once

#include "camera.h"
#include <QDir>
#include <QGraphicsPixmapItem>
#include <QGraphicsView>
#include <QKeyEvent>
#include <QLineEdit>
#include <QMainWindow>
#include <QPlainTextEdit>
#include <QTimer>
#include <filesystem>
#include <regex>

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

  void increment() {
    std::string input = toPlainText().toStdString();

    std::regex pattern("\\d{3}"); // Match exactly three digits
    std::sregex_iterator begin(input.begin(), input.end(), pattern);
    std::sregex_iterator end;
    std::smatch lastMatch;

    // Find the last match
    for (std::sregex_iterator it = begin; it != end; ++it) {
      lastMatch = *it; // Update the last match
    }

    // Check if a match was found
    if (!lastMatch.empty()) {
      std::string matchedNumber = lastMatch.str(); // Get the matched number
      int number = std::stoi(matchedNumber);       // Convert to integer
      number += 1;                                 // Increment by 1

      // Convert back to a zero-padded string
      std::ostringstream oss;
      oss << std::setw(3) << std::setfill('0') << number;
      std::string incrementedNumber = oss.str();

      // Replace the last match in the original string
      std::string result = input;
      result.replace(lastMatch.position(), lastMatch.length(),
                     incrementedNumber);

      setPlainText(QString::fromStdString(result));
    }
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
  QLineEdit *duration_edit;
  QLineEdit *fps_edit;
  DirectoryEdit *save_dir_edit;
};
