#include "camera.h"
#include "main_window.h"
#include <CLI/CLI.hpp>
#include <QApplication>
#include <chrono>
#include <iostream>
#include <string>
#include <thread>

int main(int argc, char **argv) {
  auto app = CLI::App{"huitacam"};
  std::string config_dir;
  app.add_option("-c,--config-dir", config_dir, "Config directory")
      ->check(CLI::ExistingDirectory)
      ->required();
  CLI11_PARSE(app, argc, argv);
  CameraSystem camera_system;
  camera_system.load_config(config_dir);

  for (auto &camera : camera_system) {
    std::cout << "Camera serial number: " << camera.get_serial_number()
              << std::endl;
  }
  for (auto &camera : camera_system) {
    camera.preview();
  }

  QApplication qapp(argc, argv);
  MainWindow main_window(camera_system);
  main_window.setWindowTitle("huitacam");
  main_window.showMaximized();
  return qapp.exec();
}
