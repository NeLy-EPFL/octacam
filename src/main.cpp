#include <chrono>
#include <filesystem>
#include <iostream>
#include <string>
#include <thread>

#include <CLI/CLI.hpp>
#include <QApplication>
#include <spdlog/sinks/stdout_color_sinks.h>
#include <spdlog/spdlog.h>

#include "camera.hpp"
#include "main_window.hpp"

int main(int argc, char **argv) {
  auto app = CLI::App{"octacam"};
  std::string config_dir = "./";
  app.add_option("config-dir", config_dir, "Config directory")
      ->check(CLI::ExistingDirectory);
  std::string log_level = "info";
  app.add_option("-l,--log-level", log_level, "Log level")
      ->check(
          CLI::IsMember({"trace", "debug", "info", "warn", "error", "fatal"}));
  CLI11_PARSE(app, argc, argv);

  try {
    auto console_sink = std::make_shared<spdlog::sinks::stdout_color_sink_mt>();
    auto logger =
        std::make_shared<spdlog::logger>("octacam_logger", console_sink);
    logger->set_level(spdlog::level::from_str(log_level));
    // Set a custom pattern to hide timestamp and logger name
    // %^ and %$ are for color start and end
    // %l is for log level
    // %v is for the log message
    logger->set_pattern("[%^%l%$] %v");
    spdlog::set_default_logger(logger);
    spdlog::flush_on(spdlog::level::info); // Flush on info and higher levels
  } catch (const spdlog::spdlog_ex &ex) {
    std::cerr << "Log initialization failed: " << ex.what() << std::endl;
    return 1;
  }

  config_dir = std::filesystem::canonical(config_dir).string();
  spdlog::info("Config directory: {}", config_dir);

  CameraSystem camera_system;

  auto n_cameras = camera_system.get_camera_count();

  if (n_cameras <= 0) {
    spdlog::warn("No cameras found. Exiting.");
    return 1;
  } else {
    spdlog::info("Found {} camera(s)", n_cameras);
  }

  camera_system.load_config(config_dir);
  camera_system.start_preview();

  QApplication qapp(argc, argv);
  MainWindow main_window(camera_system);
  main_window.setWindowTitle("octacam");
  main_window.showNormal();

  return qapp.exec();
}
