#include "camera.h"
#include <chrono>
#include <fstream>
#include <future>
#include <iostream>
#include <thread>

FrameForDisplay::~FrameForDisplay() { delete[] data; }

std::optional<QPixmap> FrameForDisplay::retrieve_as_pixmap() {
  std::lock_guard<std::mutex> lock(mtx);
  if (retrieved) {
    return std::nullopt;
  }
  QImage image(static_cast<const uint8_t *>(data), width, height,
               QImage::Format_Grayscale8);
  auto pixmap = QPixmap::fromImage(image);
  retrieved = true;
  return pixmap;
}

void FrameForDisplay::store_frame(const uint8_t *data) {
  std::unique_lock<std::mutex> lock(mtx, std::try_to_lock);
  if (lock.owns_lock() && retrieved) {
    std::copy(data, data + size, this->data);
    retrieved = false;
  }
}

void FrameForDisplay::update_size(int width, int height) {
  std::lock_guard<std::mutex> lock(mtx);
  if (this->width != width || this->height != height) {
    delete[] data;
    this->width = width;
    this->height = height;
    size = this->width * this->height;
    data = new uint8_t[size];
  }
}

Camera::Camera(Pylon::IPylonDevice *device, const CameraSystem &system)
    : camera(std::make_unique<Pylon::CBaslerUniversalInstantCamera>(device)),
      system(system) {
  camera->Open();
}

Camera::~Camera() {
  stop_flag = true;
  if (future.valid()) {
    future.get();
  }
}

Camera::Camera(Camera &&other)
    : camera(std::move(other.camera)), system(other.system) {
  other.camera = nullptr;
}

std::string Camera::get_serial_number() const {
  return std::string(camera->GetDeviceInfo().GetSerialNumber().c_str());
}

void Camera::start_preview() {
  stop_flag = false;
  future = std::async(std::launch::async, [this]() {
    camera->TriggerSource.SetValue(
        Basler_UniversalCameraParams::TriggerSource_Software);
    camera->StartGrabbing(Pylon::GrabStrategy_LatestImageOnly);
    while (camera->IsGrabbing() && !stop_flag) {
      Pylon::CGrabResultPtr ptrGrabResult;
      camera->RetrieveResult(1000 / 33, ptrGrabResult,
                             Pylon::TimeoutHandling_Return);
      if (ptrGrabResult && ptrGrabResult->GrabSucceeded()) {
        const uint8_t *pImageBuffer = (uint8_t *)ptrGrabResult->GetBuffer();
        frame_for_display.store_frame(pImageBuffer);
      }
    }
    camera->StopGrabbing();
  });
}

void Camera::start_record() {
  future = std::async(std::launch::async, [this]() {
    // camera->TriggerSource.SetValue(
    //     Basler_UniversalCameraParams::TriggerSource_Software);
    camera->StartGrabbing(Pylon::GrabStrategy_OneByOne);
    while (camera->IsGrabbing() && !stop_flag) {
      Pylon::CGrabResultPtr ptrGrabResult;
      camera->RetrieveResult(Pylon::INFINITE, ptrGrabResult);
      if (ptrGrabResult->GrabSucceeded()) {
        const uint8_t *pImageBuffer = (uint8_t *)ptrGrabResult->GetBuffer();
        frame_for_display.store_frame(pImageBuffer);
        std::cout << "Frame saved" << std::endl;
      } else {
        std::cerr << "Error: " << std::hex << ptrGrabResult->GetErrorCode()
                  << std::dec << ptrGrabResult->GetErrorDescription()
                  << std::endl;
      }
    }
    camera->StopGrabbing();
  });
}

void Camera::load_config(const std::string &config) {
  if (!config.empty()) {
    Pylon::CFeaturePersistence::LoadFromString(config.c_str(),
                                               &camera->GetNodeMap());
  }
  frame_for_display.update_size(camera->Width.GetValue(),
                                camera->Height.GetValue());
  camera->TriggerMode.SetValue(Basler_UniversalCameraParams::TriggerMode_On);
}

CameraSystem::CameraSystem() {
  auto &tlFactory = Pylon::CTlFactory::GetInstance();
  Pylon::DeviceInfoList_t devices;
  if (tlFactory.EnumerateDevices(devices) == 0) {
    std::cerr << "No camera present." << std::endl;
  }
  for (size_t i = 0; i < devices.size(); ++i) {
    cameras.emplace_back(tlFactory.CreateDevice(devices[i]), *this);
  }
}

CameraSystem::~CameraSystem() = default;

void CameraSystem::load_config(const std::string &directory) {
  for (auto &camera : cameras) {
    auto serial_number = camera.get_serial_number();
    std::string config_file = directory + "/" + serial_number + ".pfs";
    std::ifstream file(config_file);
    if (file) {
      std::cout << "Loading config for camera: " << serial_number << std::endl;
      std::string content((std::istreambuf_iterator<char>(file)),
                          std::istreambuf_iterator<char>());
      camera.load_config(content);
    } else {
      camera.load_config("");
      std::cerr << "Warning: config file not found at" << config_file
                << std::endl;
    }
  }
}

void CameraSystem::start_preview() {
  for (auto &camera : cameras) {
    camera.start_preview();
  }
}

void CameraSystem::start_record() {
  stop();
  for (auto &camera : cameras) {
    camera.start_record();
  }
}

void CameraSystem::abort_record() { stop(); }

void CameraSystem::trigger_once() {
  for (auto &camera : cameras) {
    camera.camera->ExecuteSoftwareTrigger();
  }
}

std::vector<std::optional<QPixmap>> CameraSystem::get_pixmaps() {
  std::vector<std::optional<QPixmap>> pixmaps;
  pixmaps.reserve(cameras.size());
  for (auto &camera : cameras) {
    pixmaps.push_back(camera.frame_for_display.retrieve_as_pixmap());
  }
  return pixmaps;
}

std::vector<Camera>::iterator CameraSystem::begin() { return cameras.begin(); }
std::vector<Camera>::iterator CameraSystem::end() { return cameras.end(); }
std::vector<Camera>::const_iterator CameraSystem::begin() const {
  return cameras.begin();
}
std::vector<Camera>::const_iterator CameraSystem::end() const {
  return cameras.end();
}

void CameraSystem::stop() {
  for (auto &camera : cameras) {
    camera.stop_flag = true;
  }
  for (auto &camera : cameras) {
    if (camera.future.valid()) {
      camera.future.get();
    }
  }
}