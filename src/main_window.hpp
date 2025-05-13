#pragma once

#include <array>
#include <filesystem>
#include <regex>

#include <QComboBox>
#include <QDir>
#include <QDoubleSpinBox> // Added include
#include <QGraphicsPixmapItem>
#include <QGraphicsView>
#include <QKeyEvent>
#include <QLabel>
#include <QLineEdit>
#include <QMainWindow>
#include <QMdiArea>
#include <QPlainTextEdit>
#include <QPushButton>
#include <QRadioButton>
#include <QString>
#include <QTimer>
#include <QVector>

#include "camera.hpp"

class DirectoryEdit : public QPlainTextEdit {
  Q_OBJECT

public:
  explicit DirectoryEdit(QWidget *parent = nullptr) : QPlainTextEdit(parent) {
    auto now = std::chrono::system_clock::now();
    auto now_time_t = std::chrono::system_clock::to_time_t(now);
    std::tm *now_tm = std::localtime(&now_time_t);
    std::array<char, 7> date_str_arr;
    std::strftime(date_str_arr.data(), date_str_arr.size(), "%y%m%d", now_tm);
    QString defaultPath =
        QString("~/data/TL/%1-dfd_g8m/Fly1/001-neck").arg(date_str_arr.data());
    setPlainText(defaultPath);
  }

  DirectoryEdit(const DirectoryEdit &) = delete;
  DirectoryEdit &operator=(const DirectoryEdit &) = delete;
  DirectoryEdit(DirectoryEdit &&) = delete;
  DirectoryEdit &operator=(DirectoryEdit &&) = delete;

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
  GraphicsView(const GraphicsView &) = delete;
  GraphicsView &operator=(const GraphicsView &) = delete;
  GraphicsView(GraphicsView &&) = delete;
  GraphicsView &operator=(GraphicsView &&) = delete;

protected:
  void resizeEvent(QResizeEvent *event) override;
};

class MainWindow : public QMainWindow {
  Q_OBJECT

public:
  explicit MainWindow(CameraSystem &camera_system, QWidget *parent = nullptr);
  ~MainWindow() override;
  MainWindow(const MainWindow &) = delete;
  MainWindow &operator=(const MainWindow &) = delete;
  MainWindow(MainWindow &&) = delete;
  MainWindow &operator=(MainWindow &&) = delete;

protected:
  void resizeEvent(QResizeEvent *event) override;

private slots:
  void rotate_displays();
  void update_frames();
  void check_record_started();
  void update_record_countdown();
  void on_record_button_clicked();
  void on_fps_value_changed(double value); // Changed slot signature

private:
  void setup_ui();
  void start_record();
  void stop_record();

  CameraSystem &camera_system;
  QVector<QGraphicsPixmapItem *> pixmap_items;
  QVector<QWidget *> input_widgets;
  QMdiArea *mdi_area;
  QTimer *record_countdown_timer;
  QTimer *check_record_started_timer;
  QPushButton *record_button;
  QLineEdit *duration_edit;
  QDoubleSpinBox *fps_edit; // Changed type from QLineEdit*
  DirectoryEdit *save_dir_edit;
  QLabel *status_label;
  QComboBox *video_writer_combo;
  QComboBox *trigger_source_combo;
  QRadioButton *rotate_selected_button;
  QRadioButton *rotate_all_button;
  std::chrono::milliseconds record_remaining_time_;
  QVector<QString> window_titles_;
};