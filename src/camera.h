#pragma once
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

class FrameForDisplay {
public:
  ~FrameForDisplay();
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

class Camera {
public:
  explicit Camera(Pylon::IPylonDevice *device);
  ~Camera();
  Camera(Camera &&other);
  void start_preview();
  void start_record(int n_frames);
  void abort_record();
  std::string get_serial_number() const;
  std::optional<QPixmap> get_pixmap();
  void load_config(const std::string &config);

private:
  std::unique_ptr<Pylon::CBaslerUniversalInstantCamera> camera;
  FrameForDisplay frame_for_display;
  std::atomic<bool> stop_flag{false};
  std::future<void> future;
  void stop();
};

class CameraSystem {
public:
  explicit CameraSystem();
  ~CameraSystem();

  void record(int n_frames);
  void load_config(const std::string &directory);

  std::vector<Camera>::iterator begin();
  std::vector<Camera>::iterator end();
  std::vector<Camera>::const_iterator begin() const;
  std::vector<Camera>::const_iterator end() const;

private:
  Pylon::PylonAutoInitTerm autoInitTerm;
  std::vector<Camera> cameras;
};