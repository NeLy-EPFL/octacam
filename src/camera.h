#pragma once
#include <QBitmap>
#include <atomic>
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
  QPixmap get_pixmap();
  void set_data(const uint8_t *new_data);
  void set_size(int new_width, int new_height);

private:
  int width = 0;
  int height = 0;
  size_t size = 0;
  uint8_t *data = nullptr;
  std::mutex mtx;
};

class Camera {
public:
  explicit Camera(IPylonDevice *device);
  ~Camera();
  Camera(Camera &&other);
  void stop();
  void grab(int n_frames);
  void preview();
  std::string get_serial_number() const;
  QPixmap get_pixmap();
  void load_config(const std::string &config);

private:
  std::unique_ptr<CBaslerUniversalInstantCamera> camera;
  FrameForDisplay frame_for_display;
  std::atomic<bool> stop_preview{false};
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