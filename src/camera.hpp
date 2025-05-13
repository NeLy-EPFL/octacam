#pragma once

#include <atomic>
#include <future>
#include <iostream>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
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
  FrameForDisplay(FrameForDisplay &&other) noexcept;
  FrameForDisplay &operator=(FrameForDisplay &&other) noexcept;

  std::optional<QPixmap> retrieve_as_pixmap();
  void store_frame(const uint8_t *data);
  void update_size(int width, int height);

private:
  int width_ = 0;
  int height_ = 0;
  size_t size_ = 0;
  std::unique_ptr<uint8_t[]> data_;
  bool retrieved_{false};
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
  Camera &operator=(Camera &&other) noexcept;

  std::string get_serial_number() const;

private:
  void start_preview();
  void start_record(const std::string &save_path, const double &fps,
                    const std::string &fourcc);
  void load_config(const std::string &config);
  void trigger_once();

  std::unique_ptr<Pylon::CBaslerUniversalInstantCamera> camera_;
  std::unique_ptr<VideoWriter> video_writer_;
  const CameraSystem &system_;
  FrameForDisplay frame_for_display_;
  std::atomic<bool> started_{false};
  std::atomic<bool> stop_flag_{false};
  std::future<void> future_;
};

class CameraSystem {
public:
  friend class Camera;
  explicit CameraSystem();
  ~CameraSystem();

  CameraSystem(const CameraSystem &) = delete;
  CameraSystem &operator=(const CameraSystem &) = delete;
  CameraSystem(CameraSystem &&) = delete;
  CameraSystem &operator=(CameraSystem &&) = delete;

  void load_config(const std::string &directory);
  void start_preview();
  void start_record(const std::string &save_dir, const double &fps,
                    const std::string &fourcc, const std::string &extension);
  void start_software_trigger(std::chrono::nanoseconds interval,
                              std::chrono::nanoseconds duration);
  void start_software_trigger(std::chrono::nanoseconds interval);
  void stop_software_trigger();
  bool all_cameras_started() const;
  std::vector<std::optional<QPixmap>> get_pixmaps();

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