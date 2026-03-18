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
#include "parser.hpp"
#include "serial.hpp"

int main(int argc, char **argv) {
  auto app = CLI::App{"octacam"};
  std::string config_dir_str = "./";
  app.add_option("config-dir", config_dir_str, "Config directory")
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

  auto serial_port = SerialPort();
  try {
    serial_port.open("/dev/ttyACM0", 115200);
  } catch (const std::system_error &ex) {
    spdlog::warn("Failed to open serial port {}: {}", "/dev/ttyACM0",
                 ex.what());
  }

  auto config_dir = std::filesystem::canonical(config_dir_str);
  spdlog::info("Using config directory: {}", config_dir.string());

  auto config_path = config_dir / "octacam_config.yml";
  if (!std::filesystem::exists(config_path)) {
    config_path = config_dir / "octacam_config.yaml";
  }

  OctacamConfig config;
  bool config_exists = std::filesystem::exists(config_path);

  if (config_exists) {
    config = parse_config(config_path.string());
  }

  std::vector<std::string> requested_serial_numbers;
  for (const auto &camera_config : config.camera_configs) {
    requested_serial_numbers.push_back(camera_config.serial_number);
  }

  CameraSystem camera_system(requested_serial_numbers);

  auto n_cameras = camera_system.get_camera_count();

  for (auto &camera : camera_system) {
    auto serial_number = camera.get_serial_number();
    auto it = std::ranges::find_if(
        config.camera_configs,
        [&serial_number](const CameraConfig &camera_config) {
          return camera_config.serial_number == serial_number;
        });
    if (it != config.camera_configs.end()) {
      camera.set_name(it->name);
    }
  }

  if (n_cameras <= 0) {
    spdlog::warn("No cameras opened. Exiting.");
    return 1;
  } else {
    spdlog::info("Opened {} camera(s)", n_cameras);
  }

  camera_system.load_config(config_dir);
  camera_system.start_preview();

  QApplication qapp(argc, argv);
  MainWindow main_window(camera_system, config, serial_port);
  main_window.setWindowTitle("octacam");
  main_window.showNormal();

  return qapp.exec();
}
