#pragma once
#include <iostream>
#include <memory>
#include <opencv2/videoio.hpp>
#include <pylon/BaslerUniversalInstantCamera.h>
#include <pylon/PylonIncludes.h>
#include <string>
#include <thread>
#include <vector>

using namespace Pylon;

class Camera {
public:
  explicit Camera(IPylonDevice *device);
  ~Camera();
  Camera(Camera &&other);
  void grab(int n_frames);
  std::string get_serial_number();
  void load_config(const std::string &config);

private:
  std::unique_ptr<CBaslerUniversalInstantCamera> camera;
};

class CameraSystem {
public:
  explicit CameraSystem();
  ~CameraSystem();

  void record(int n_frames);
  void load_config(const std::string &directory);

private:
  PylonAutoInitTerm autoInitTerm;
  std::vector<Camera> cameras;
};