#pragma once
#include <pylon/PylonIncludes.h>
#include <pylon/BaslerUniversalInstantCamera.h>
#include <vector>
#include <memory>
#include <thread>
#include <iostream>
#include <opencv2/videoio.hpp>
#include <string>

using namespace Pylon;

class Camera {
public:
    explicit Camera(IPylonDevice * device);
    ~Camera();
    Camera(Camera && other);
    void grab(int n_frames);
    std::string get_serial_number();
    void load_config(const std::string & config);

private:
    std::unique_ptr<CBaslerUniversalInstantCamera> camera;
};

class CameraSystem {
public:
    explicit CameraSystem();
    ~CameraSystem();

    void record(int n_frames);
    void load_config(const std::string & directory);

private:
    PylonAutoInitTerm autoInitTerm;
    std::vector<Camera> cameras;
};