#pragma once

#include <map>
#include <optional>
#include <string>
#include <vector>
#include <yaml-cpp/yaml.h>

struct CameraConfig {
  std::string serial_number;
  std::string name;
  double scale_x = 1;
  double scale_y = 1;
  double rotation_deg = 0;
};

struct GuiConfig {
  double fps = 100.0;
  double duration = 0.0;
  std::string unit = "s";
  std::string save_directory = "./";
  std::string trigger_source = "software";
  std::string video_writer = "opencv MJPG avi";
};

struct OctacamConfig {
  GuiConfig gui_config;
  std::vector<CameraConfig> camera_configs;
};

OctacamConfig parse_config(const std::string &file_path);