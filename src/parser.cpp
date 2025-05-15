#include "parser.hpp"

#include <ctime>
#include <filesystem>

#include <spdlog/spdlog.h>

OctacamConfig parse_config(const std::string &file_path) {
  int n_cameras = 0;
  OctacamConfig ret;
  YAML::Node file;

  if (!std::filesystem::exists(file_path)) {
    spdlog::info("octacam config file not found at {}. ", file_path);
    spdlog::info("All detected cameras will be used.");
    goto label;
  }

  try {
    file = YAML::LoadFile(file_path);
  } catch (const YAML::ParserException &e) {
    spdlog::error("Failed to parse octacam config file: {}", e.what());
    goto label;
  }

  if (file["gui"].IsDefined()) {
    if (!file["gui"].IsMap()) {
      spdlog::warn("Ignoring \"default_parameters\" in octacam config as it is "
                   "not a map");
    } else {
      auto src = file["gui"];
      auto &dst = ret.gui_config;

      if (src["fps_default"].IsDefined()) {
        dst.fps_default = src["fps_default"].as<double>();
      }
      if (src["fps_min"].IsDefined()) {
        dst.fps_min = src["fps_min"].as<double>();
      }
      if (src["fps_max"].IsDefined()) {
        dst.fps_max = src["fps_max"].as<double>();
      }

      if (src["duration_default"].IsDefined()) {
        dst.duration_default = src["duration_default"].as<double>();
      }
      if (src["duration_min"].IsDefined()) {
        dst.duration_min = src["duration_min"].as<double>();
      }
      if (src["duration_max"].IsDefined()) {
        dst.duration_max = src["duration_max"].as<double>();
      }

      if (src["duration_unit_default_index"].IsDefined()) {
        dst.duration_unit_default_index =
            src["duration_unit_default_index"].as<int>();
      }
      if (src["save_directory_default"].IsDefined()) {
        dst.save_directory_default =
            src["save_directory_default"].as<std::string>();
      }
      if (src["trigger_source_default_index"].IsDefined()) {
        dst.trigger_source_default_index =
            src["trigger_source_default_index"].as<int>();
      }
      if (src["video_writer_default_index"].IsDefined()) {
        dst.video_writer_default_index =
            src["video_writer_default_index"].as<int>();
      }

      if (src["display_refresh_interval_ms"].IsDefined()) {
        dst.display_refresh_interval_ms =
            src["display_refresh_interval_ms"].as<int>();
      }
      if (src["record_countdown_timer_interval_ms"].IsDefined()) {
        dst.record_countdown_timer_interval_ms =
            src["record_countdown_timer_interval_ms"].as<int>();
      }
      if (src["check_record_started_timer_interval_ms"].IsDefined()) {
        dst.check_record_started_timer_interval_ms =
            src["check_record_started_timer_interval_ms"].as<int>();
      }

      if (src["dock_min_width"].IsDefined()) {
        dst.dock_min_width = src["dock_min_width"].as<int>();
      }
      if (src["dock_max_width"].IsDefined()) {
        dst.dock_max_width = src["dock_max_width"].as<int>();
      }
      if (src["save_dir_edit_height_factor"].IsDefined()) {
        dst.save_dir_edit_height_factor =
            src["save_dir_edit_height_factor"].as<int>();
      }
    }
  }

  if (!file["cameras"].IsDefined()) {
    goto label;
  }

  if (!file["cameras"].IsSequence()) {
    spdlog::warn(
        "Ignoring \"cameras\" in octacam config as it is not a sequence");
    goto label;
  }

  for (const auto &src : file["cameras"]) {
    CameraConfig dst;
    if (!src["serial_number"].IsDefined()) {
      spdlog::warn("Ignoring the {}th entry of \"cameras\" as its "
                   "\"serial_number\" is absent",
                   n_cameras);
      continue;
    }
    dst.serial_number = src["serial_number"].as<std::string>();

    bool is_serial_number_unique = true;
    for (int i = 0; i < n_cameras; i++) {
      if (dst.serial_number == ret.camera_configs[i].serial_number) {
        is_serial_number_unique = false;
        break;
      }
    }
    if (!is_serial_number_unique) {
      spdlog::warn("Ignoring the {}th entry of \"cameras\" as its "
                   "\"serial_number\" is not unique",
                   n_cameras);
      continue;
    }

    if (src["name"].IsDefined()) {
      dst.name = src["name"].as<std::string>();
    }

    bool is_name_unique = true;
    for (int i = 0; i < n_cameras; i++) {
      if (dst.name == ret.camera_configs[i].name) {
        is_name_unique = false;
        break;
      }
    }
    if (!is_name_unique) {
      spdlog::warn("Ignoring the {}th entry of \"cameras\" as its \"name\" is "
                   "not unique",
                   n_cameras);
      continue;
    }

    if (src["scale_x"].IsDefined()) {
      dst.scale_x = src["scale_x"].as<double>();
    }
    if (src["scale_y"].IsDefined()) {
      dst.scale_y = src["scale_y"].as<double>();
    }
    if (src["rotation_deg"].IsDefined()) {
      dst.rotation_deg = src["rotation_deg"].as<double>();
    }
    ret.camera_configs.push_back(dst);
    n_cameras++;
  }

  if (n_cameras == 0) {
    spdlog::info("No cameras found in octacam config file. All detected "
                 "cameras will be used.");
    goto label;
  }

  spdlog::info("Found {} camera(s) in octacam config file",
               ret.camera_configs.size());

label:
  char buffer[1000];
  time_t now = std::time(nullptr);
  std::strftime(buffer, sizeof(buffer),
                ret.gui_config.save_directory_default.c_str(),
                std::localtime(&now));
  ret.gui_config.save_directory_default = std::string(buffer);

  return ret;
}