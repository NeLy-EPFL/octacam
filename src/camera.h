#pragma once
#include <QBitmap>
#include <atomic>
#include <future>
#include <iostream>
#include <memory>
#include <mutex>
#include <opencv2/videoio.hpp>
#include <pylon/BaslerUniversalInstantCamera.h>
#include <pylon/PylonIncludes.h>
#include <string>
#include <thread>
#include <vector>

using namespace Pylon;

class FrameForDisplay {
public:
  ~FrameForDisplay();
  QPixmap retrieve_as_pixmap();
  void store_frame(const uint8_t *new_data);
  void update_size(int new_width, int new_height);

private:
  int width = 0;
  int height = 0;
  size_t size = 0;
  uint8_t *data = nullptr;
  bool retrieved = true;
  std::mutex mtx;
};

class Camera {
public:
  explicit Camera(IPylonDevice *device);
  ~Camera();
  Camera(Camera &&other);
  void grab(int n_frames);
  void start_preview();
  void stop_preview();
  std::string get_serial_number() const;
  QPixmap get_pixmap();
  void load_config(const std::string &config);

private:
  std::unique_ptr<CBaslerUniversalInstantCamera> camera;
  FrameForDisplay frame_for_display;
  std::atomic<bool> stop_preview_flag{false};
  std::future<void> preview_future;
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
  PylonAutoInitTerm autoInitTerm;
  std::vector<Camera> cameras;
};