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
  double window_x = -1;
  double window_y = -1;
  double window_width = -1;
  double window_height = -1;
};

struct GuiConfig {
  double fps_default = 100.0;
  double fps_min = 0.01;
  double fps_max = 1000.0;
  double duration_default = 0.0;
  double duration_min = 0.01;
  double duration_max = 1000000.0;
  int duration_unit_default_index = 1;
  std::string save_directory_default = "./";
  int trigger_source_default_index = 0;
  int video_writer_default_index = 0;

  int display_refresh_interval_ms = 33;
  int record_countdown_timer_interval_ms = 1000;
  int check_record_started_timer_interval_ms = 100;

  int dock_min_width = 200;
  int dock_max_width = 300;
  int save_dir_edit_height_factor = 4;
};

struct OctacamConfig {
  GuiConfig gui_config;
  std::vector<CameraConfig> camera_configs;
};

OctacamConfig parse_config(const std::string &file_path);