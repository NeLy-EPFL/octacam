#pragma once

#include "timer.h"
#include <QPixmap>
#include <atomic>
#include <future>
#include <iostream>
#include <memory>
#include <mutex>
#include <opencv2/videoio.hpp>
#include <optional>
#include <pylon/BaslerUniversalInstantCamera.h>
#include <pylon/PylonIncludes.h>
#include <string>
#include <thread>
#include <vector>

#include "video_writer.h"

class FrameForDisplay {
public:
  FrameForDisplay();
  ~FrameForDisplay();
  FrameForDisplay(FrameForDisplay &&other);
  std::optional<QPixmap> retrieve_as_pixmap();
  void store_frame(const uint8_t *data);
  void update_size(int width, int height);

private:
  int width = 0;
  int height = 0;
  size_t size = 0;
  uint8_t *data = nullptr;
  bool retrieved = false;
  std::mutex mtx;
};

class CameraSystem;

class Camera {
public:
  friend class CameraSystem;
  explicit Camera(Pylon::IPylonDevice *device, const CameraSystem &system);
  ~Camera();
  Camera(Camera &&other);
  std::string get_serial_number() const;

private:
  void start_preview();
  void start_record();
  void load_config(const std::string &config);
  void trigger_once();

  std::unique_ptr<Pylon::CBaslerUniversalInstantCamera> camera;
  std::unique_ptr<VideoWriter> video_writer;
  const CameraSystem &system;
  FrameForDisplay frame_for_display;
  std::atomic<bool> stop_flag{false};
  std::future<void> future;
};

class CameraSystem {
public:
  friend class Camera;
  explicit CameraSystem();
  ~CameraSystem();

  void load_config(const std::string &directory);
  void start_preview();
  void start_record();
  void start_software_trigger(std::chrono::nanoseconds interval,
                              std::chrono::nanoseconds duration);
  void start_software_trigger(std::chrono::nanoseconds interval);
  void stop_software_trigger();
  bool is_software_trigger_running() const;
  std::vector<std::optional<QPixmap>> get_pixmaps();

  std::vector<Camera>::iterator begin();
  std::vector<Camera>::iterator end();
  std::vector<Camera>::const_iterator begin() const;
  std::vector<Camera>::const_iterator end() const;

private:
  Pylon::PylonAutoInitTerm autoInitTerm;
  std::vector<Camera> cameras;
  PreciseTimer trigger_timer;
  void stop();
};