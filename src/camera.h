#pragma once
#include <QBitmap>
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
  ~FrameForDisplay() { delete[] data; }
  QBitmap get_bitmap() {
    std::lock_guard<std::mutex> lock(mtx);
    QImage image(static_cast<const uchar *>(data), width, height,
                 QImage::Format_Grayscale8);
    return QBitmap::fromImage(image);
  }
  void set_data(const uint8_t *new_data) {
    if (mtx.try_lock()) {
      std::copy(new_data, new_data + size, data);
    }
  }

  void set_size(int new_width, int new_height) {
    std::lock_guard<std::mutex> lock(mtx);
    if (width != new_width || height != new_height) {
      delete[] data;
      width = new_width;
      height = new_height;
      size = width * height;
      data = new uint8_t[size];
    }
  }

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
  void grab(int n_frames);
  void preview();
  std::string get_serial_number();
  void load_config(const std::string &config);

private:
  std::unique_ptr<CBaslerUniversalInstantCamera> camera;
  FrameForDisplay frame_for_display;
};

class CameraSystem {
public:
  explicit CameraSystem();
  ~CameraSystem();

  void record(int n_frames);
  void load_config(const std::string &directory);

  auto begin() { return cameras.begin(); }
  auto end() { return cameras.end(); }

  auto begin() const { return cameras.begin(); }
  auto end() const { return cameras.end(); }

private:
  PylonAutoInitTerm autoInitTerm;
  std::vector<Camera> cameras;
};