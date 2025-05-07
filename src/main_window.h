#pragma once
#include <QMainWindow>

class MainWindow : public QMainWindow {
  Q_OBJECT

public:
  explicit MainWindow(QWidget *parent = nullptr);
  ~MainWindow();

private:
  void setupUi(int n_views, int n_rows = 2, int n_cols = -1);
};