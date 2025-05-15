#pragma once

#include <atomic>
#include <filesystem>
#include <future>
#include <iostream>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include <QPixmap>
#include <opencv2/videoio.hpp>
#include <pylon/BaslerUniversalInstantCamera.h>
#include <pylon/PylonIncludes.h>

#include "timer.hpp"
#include "video_writer.hpp"

class FrameForDisplay {
public:
  FrameForDisplay();
  ~FrameForDisplay();

  FrameForDisplay(const FrameForDisplay &) = delete;
  FrameForDisplay &operator=(const FrameForDisplay &) = delete;
  FrameForDisplay(FrameForDisplay &&other) : mat_(other.mat_) {
    other.mat_ = nullptr;
  }
  FrameForDisplay &operator=(FrameForDisplay &&other) = delete;
  cv::Mat *pop();
  bool push(const cv::Mat &frame);

private:
  cv::Mat *mat_ = nullptr;
  std::mutex mtx_;
};

class CameraSystem;

class Camera {
public:
  friend class CameraSystem;
  explicit Camera(Pylon::IPylonDevice *device, const CameraSystem &system);
  ~Camera();

  Camera(const Camera &) = delete;
  Camera &operator=(const Camera &) = delete;
  Camera(Camera &&other) noexcept;
  Camera &operator=(Camera &&other) = delete;

  std::string get_serial_number() const;
  void set_name(const std::string &name);
  std::string get_name() const;

private:
  void start_preview();
  void start_record(const std::string &save_path, const double &fps,
                    const std::string &fourcc);
  void load_params(const std::string &config);
  void trigger_once();
  inline void store_timestamp(const Pylon::CGrabResultPtr &ptrGrabResult);
  inline void update_resulting_fps(size_t n_frames = 6);

  std::unique_ptr<Pylon::CBaslerUniversalInstantCamera> camera_;
  std::unique_ptr<VideoWriter> video_writer_;
  FrameForDisplay frame_for_display_;
  std::atomic<bool> started_{false};
  std::atomic<bool> stop_flag_{false};
  std::future<void> future_;
  std::vector<uint64_t> timestamps_;
  std::atomic<double> resulting_fps_{0.0};
  std::string name_;
  Basler_UniversalCameraParams::TriggerSourceEnums original_trigger_source_;
};

class CameraSystem {
public:
  friend class Camera;
  explicit CameraSystem(const std::vector<std::string> &serial_numbers);
  ~CameraSystem();

  CameraSystem(const CameraSystem &) = delete;
  CameraSystem &operator=(const CameraSystem &) = delete;
  CameraSystem(CameraSystem &&) = delete;
  CameraSystem &operator=(CameraSystem &&) = delete;

  void load_config(const std::filesystem::path &directory);
  void start_preview();
  void start_record(const std::string &save_dir, const double &fps,
                    const std::string &fourcc, const std::string &extension);
  void set_software_trigger_frequency(const double &hz);
  void start_software_trigger(std::chrono::nanoseconds duration);
  void start_software_trigger();
  void stop_software_trigger();
  void set_trigger_source(const bool &use_software_trigger);
  bool all_cameras_started() const;
  std::vector<std::pair<cv::Mat *, double>> get_mats_and_fps();
  int get_camera_count() const;

  std::vector<Camera>::iterator begin();
  std::vector<Camera>::iterator end();
  std::vector<Camera>::const_iterator begin() const;
  std::vector<Camera>::const_iterator end() const;

private:
  Pylon::PylonAutoInitTerm auto_init_term_;
  std::vector<Camera> cameras_;
  PreciseTimer trigger_timer_;
  void stop();
};