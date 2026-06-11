#include "parser.hpp"

#include <ctime>
#include <filesystem>
#include <unordered_set>

#include <spdlog/spdlog.h>

OctacamConfig parse_config(const std::string &file_path) {
  OctacamConfig ret;

  const auto finalize = [&ret]() {
    char buffer[1000]{};
    const std::time_t now = std::time(nullptr);
    const std::tm *local_tm = std::localtime(&now);
    if (local_tm && std::strftime(buffer, sizeof(buffer),
                                  ret.gui_config.save_directory_default.c_str(),
                                  local_tm) > 0) {
      ret.gui_config.save_directory_default = std::string(buffer);
    }
    return ret;
  };

  if (!std::filesystem::exists(file_path)) {
    spdlog::info("octacam config file not found at {}. ", file_path);
    spdlog::info("All detected cameras will be used.");
    return finalize();
  }

  YAML::Node file;
  try {
    file = YAML::LoadFile(file_path);
  } catch (const YAML::ParserException &e) {
    spdlog::error("Failed to parse octacam config file: {}", e.what());
    return finalize();
  }

  if (file["gui"].IsDefined()) {
    if (!file["gui"].IsMap()) {
      spdlog::warn("Ignoring \"gui\" in octacam config as it is "
                   "not a map");
    } else {
      auto src = file["gui"];
      auto &dst = ret.gui_config;

      if (src["fps_default"].IsDefined()) {
        try {
          dst.fps_default = src["fps_default"].as<double>();
        } catch (const YAML::BadConversion &) {
          spdlog::warn(
              "\"fps_default\" is not of type double in the config file");
        }
      }
      if (src["fps_min"].IsDefined()) {
        try {
          dst.fps_min = src["fps_min"].as<double>();
        } catch (const YAML::BadConversion &) {
          spdlog::warn("\"fps_min\" is not of type double in the config file");
        }
      }
      if (src["fps_max"].IsDefined()) {
        try {
          dst.fps_max = src["fps_max"].as<double>();
        } catch (const YAML::BadConversion &) {
          spdlog::warn("\"fps_max\" is not of type double in the config file");
        }
      }

      if (src["duration_default"].IsDefined()) {
        try {
          dst.duration_default = src["duration_default"].as<double>();
        } catch (const YAML::BadConversion &) {
          spdlog::warn(
              "\"duration_default\" is not of type double in the config file");
        }
      }
      if (src["duration_min"].IsDefined()) {
        try {
          dst.duration_min = src["duration_min"].as<double>();
        } catch (const YAML::BadConversion &) {
          spdlog::warn(
              "\"duration_min\" is not of type double in the config file");
        }
      }
      if (src["duration_max"].IsDefined()) {
        try {
          dst.duration_max = src["duration_max"].as<double>();
        } catch (const YAML::BadConversion &) {
          spdlog::warn(
              "\"duration_max\" is not of type double in the config file");
        }
      }

      if (src["duration_unit_default_index"].IsDefined()) {
        try {
          dst.duration_unit_default_index =
              src["duration_unit_default_index"].as<int>();
        } catch (const YAML::BadConversion &) {
          spdlog::warn("\"duration_unit_default_index\" is not of type int in "
                       "the config file");
        }
      }
      if (src["save_directory_default"].IsDefined()) {
        try {
          dst.save_directory_default =
              src["save_directory_default"].as<std::string>();
        } catch (const YAML::BadConversion &) {
          spdlog::warn("\"save_directory_default\" is not of type string in "
                       "the config file");
        }
      }
      if (src["trigger_source_default_index"].IsDefined()) {
        try {
          dst.trigger_source_default_index =
              src["trigger_source_default_index"].as<int>();
        } catch (const YAML::BadConversion &) {
          spdlog::warn("\"trigger_source_default_index\" is not of type int in "
                       "the config file");
        }
      }
      if (src["video_writer_default_index"].IsDefined()) {
        try {
          dst.video_writer_default_index =
              src["video_writer_default_index"].as<int>();
        } catch (const YAML::BadConversion &) {
          spdlog::warn("\"video_writer_default_index\" is not of type int in "
                       "the config file");
        }
      }

      if (src["display_refresh_interval_ms"].IsDefined()) {
        try {
          dst.display_refresh_interval_ms =
              src["display_refresh_interval_ms"].as<int>();
        } catch (const YAML::BadConversion &) {
          spdlog::warn("\"display_refresh_interval_ms\" is not of type int in "
                       "the config file");
        }
      }
      if (src["record_countdown_timer_interval_ms"].IsDefined()) {
        try {
          dst.record_countdown_timer_interval_ms =
              src["record_countdown_timer_interval_ms"].as<int>();
        } catch (const YAML::BadConversion &) {
          spdlog::warn("\"record_countdown_timer_interval_ms\" is not of type "
                       "int in the config file");
        }
      }
      if (src["check_record_started_timer_interval_ms"].IsDefined()) {
        try {
          dst.check_record_started_timer_interval_ms =
              src["check_record_started_timer_interval_ms"].as<int>();
        } catch (const YAML::BadConversion &) {
          spdlog::warn("\"check_record_started_timer_interval_ms\" is not of "
                       "type int in the config file");
        }
      }

      if (src["dock_min_width"].IsDefined()) {
        try {
          dst.dock_min_width = src["dock_min_width"].as<int>();
        } catch (const YAML::BadConversion &) {
          spdlog::warn(
              "\"dock_min_width\" is not of type int in the config file");
        }
      }
      if (src["dock_max_width"].IsDefined()) {
        try {
          dst.dock_max_width = src["dock_max_width"].as<int>();
        } catch (const YAML::BadConversion &) {
          spdlog::warn(
              "\"dock_max_width\" is not of type int in the config file");
        }
      }
      if (src["save_dir_edit_height_factor"].IsDefined()) {
        try {
          dst.save_dir_edit_height_factor =
              src["save_dir_edit_height_factor"].as<int>();
        } catch (const YAML::BadConversion &) {
          spdlog::warn("\"save_dir_edit_height_factor\" is not of type int in "
                       "the config file");
        }
      }
    }
  }

  if (!file["cameras"].IsDefined()) {
    return finalize();
  }

  if (!file["cameras"].IsSequence()) {
    spdlog::warn(
        "Ignoring \"cameras\" in octacam config as it is not a sequence");
    return finalize();
  }

  std::unordered_set<std::string> used_serial_numbers;
  std::unordered_set<std::string> used_names;
  int camera_entry_index = 0;

  for (const auto &src : file["cameras"]) {
    CameraConfig dst;
    if (!src["serial_number"].IsDefined()) {
      spdlog::warn("Ignoring the {}th entry of \"cameras\" as its "
                   "\"serial_number\" is absent",
                   camera_entry_index);
      ++camera_entry_index;
      continue;
    }
    try {
      dst.serial_number = src["serial_number"].as<std::string>();
    } catch (const YAML::BadConversion &) {
      spdlog::warn("Ignoring the {}th entry of \"cameras\" as its "
                   "\"serial_number\" is not a string",
                   camera_entry_index);
      ++camera_entry_index;
      continue;
    }

    if (!used_serial_numbers.emplace(dst.serial_number).second) {
      spdlog::warn("Ignoring the {}th entry of \"cameras\" as its "
                   "\"serial_number\" is not unique",
                   camera_entry_index);
      ++camera_entry_index;
      continue;
    }

    if (src["name"].IsDefined()) {
      try {
        dst.name = src["name"].as<std::string>();
      } catch (const YAML::BadConversion &) {
        spdlog::warn("Ignoring \"name\" for camera {} as it is not a string",
                     dst.serial_number);
      }
    }

    if (!dst.name.empty() && !used_names.emplace(dst.name).second) {
      spdlog::warn("Ignoring the {}th entry of \"cameras\" as its \"name\" is "
                   "not unique",
                   camera_entry_index);
      ++camera_entry_index;
      continue;
    }

    if (src["scale_x"].IsDefined()) {
      try {
        dst.scale_x = src["scale_x"].as<double>();
      } catch (const YAML::BadConversion &) {
        spdlog::warn("\"scale_x\" is not of type double in the config file");
      }
    }
    if (src["scale_y"].IsDefined()) {
      try {
        dst.scale_y = src["scale_y"].as<double>();
      } catch (const YAML::BadConversion &) {
        spdlog::warn("\"scale_y\" is not of type double in the config file");
      }
    }
    if (src["rotation_deg"].IsDefined()) {
      try {
        dst.rotation_deg = src["rotation_deg"].as<double>();
      } catch (const YAML::BadConversion &) {
        spdlog::warn(
            "\"rotation_deg\" is not of type double in the config file");
      }
    }
    if (src["window_x"].IsDefined()) {
      try {
        dst.window_x = src["window_x"].as<double>();
      } catch (const YAML::BadConversion &) {
        spdlog::warn("\"window_x\" is not of type double in the config file");
      }
    }
    if (src["window_y"].IsDefined()) {
      try {
        dst.window_y = src["window_y"].as<double>();
      } catch (const YAML::BadConversion &) {
        spdlog::warn("\"window_y\" is not of type double in the config file");
      }
    }
    if (src["window_width"].IsDefined()) {
      try {
        dst.window_width = src["window_width"].as<double>();
      } catch (const YAML::BadConversion &) {
        spdlog::warn(
            "\"window_width\" is not of type double in the config file");
      }
    }
    if (src["window_height"].IsDefined()) {
      try {
        dst.window_height = src["window_height"].as<double>();
      } catch (const YAML::BadConversion &) {
        spdlog::warn(
            "\"window_height\" is not of type double in the config file");
      }
    }

    ret.camera_configs.push_back(dst);
    ++camera_entry_index;
  }

  if (ret.camera_configs.empty()) {
    spdlog::info("No cameras found in octacam config file. All detected "
                 "cameras will be used.");
    return finalize();
  }

  spdlog::info("Found {} camera(s) in octacam config file",
               ret.camera_configs.size());
  return finalize();
}