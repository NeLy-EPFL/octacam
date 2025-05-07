#include "camera.h"
#include "main_window.h"
#include <CLI/CLI.hpp>
#include <iostream>
#include <string>
#include <QApplication>
#include "main_window.h"

int main(int argc, char **argv) {
  auto app = CLI::App{"huitacam"};
  std::string config_dir;
  app.add_option("-c,--config-dir", config_dir, "Config directory")
      ->check(CLI::ExistingDirectory)
      ->required();
  CLI11_PARSE(app, argc, argv);
  CameraSystem camera_system;
  camera_system.load_config(config_dir);
  camera_system.record(10);

  QApplication qapp(argc, argv);
  MainWindow main_window;
  main_window.setWindowTitle("huitacam");
  main_window.show();
  return qapp.exec();
}
