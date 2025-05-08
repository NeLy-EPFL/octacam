#include "camera.h"
#include <chrono>
#include <fstream>
#include <future>
#include <iostream>
#include <thread>

FrameForDisplay::~FrameForDisplay() { delete[] data; }

QPixmap FrameForDisplay::get_pixmap() {
  std::lock_guard<std::mutex> lock(mtx);
  QImage image(static_cast<const uchar *>(data), width, height,
               QImage::Format_Grayscale8);
  return QPixmap::fromImage(image);
}

void FrameForDisplay::set_data(const uint8_t *new_data) {
  if (mtx.try_lock()) {
    std::copy(new_data, new_data + size, data);
    mtx.unlock();
  }
}

void FrameForDisplay::set_size(int new_width, int new_height) {
  std::lock_guard<std::mutex> lock(mtx);
  if (width != new_width || height != new_height) {
    delete[] data;
    width = new_width;
    height = new_height;
    size = width * height;
    data = new uint8_t[size];
  }
}

Camera::Camera(IPylonDevice *device)
    : camera(std::make_unique<CBaslerUniversalInstantCamera>(device)) {
  camera->Open();
}

Camera::~Camera() { stop_preview(); }

Camera::Camera(Camera &&other) : camera(std::move(other.camera)) {
  other.camera = nullptr;
}

std::string Camera::get_serial_number() const {
  return std::string(camera->GetDeviceInfo().GetSerialNumber().c_str());
}

QPixmap Camera::get_pixmap() { return frame_for_display.get_pixmap(); }

void Camera::start_preview() {
  stop_preview_flag = false;
  preview_future = std::async(std::launch::async, [this]() {
    camera->StartGrabbing(GrabStrategy_LatestImageOnly);
    while (camera->IsGrabbing() && !stop_preview_flag) {
      CGrabResultPtr ptrGrabResult;
      camera->RetrieveResult(5000, ptrGrabResult,
                             TimeoutHandling_ThrowException);
      if (ptrGrabResult->GrabSucceeded()) {
        intptr_t cameraContextValue = ptrGrabResult->GetCameraContext();
        const uint8_t *pImageBuffer = (uint8_t *)ptrGrabResult->GetBuffer();
        frame_for_display.set_data(pImageBuffer);
      } else {
        std::cerr << "Error: " << std::hex << ptrGrabResult->GetErrorCode()
                  << std::dec << ptrGrabResult->GetErrorDescription()
                  << std::endl;
      }
    }
    camera->StopGrabbing();
  });
}

void Camera::stop_preview() {
  stop_preview_flag = true;
  if (preview_future.valid()) {
    preview_future.get();
  }
}

void Camera::grab(int n_frames) {
  int width = camera->Width.GetValue();
  int height = camera->Height.GetValue();
  std::string filename = get_serial_number() + ".mp4";
  auto writer =
      cv::VideoWriter(filename, cv::VideoWriter::fourcc('a', 'v', 'c', '1'), 30,
                      cv::Size(width, height), false);
  camera->StartGrabbing();
  CGrabResultPtr ptrGrabResult;
  for (int i = 0; i < n_frames; ++i) {
    camera->RetrieveResult(5000, ptrGrabResult, TimeoutHandling_ThrowException);
    if (ptrGrabResult->GrabSucceeded()) {
      intptr_t cameraContextValue = ptrGrabResult->GetCameraContext();
      const uint8_t *pImageBuffer = (uint8_t *)ptrGrabResult->GetBuffer();
      writer.write(cv::Mat(ptrGrabResult->GetHeight(),
                           ptrGrabResult->GetWidth(), CV_8UC1,
                           (void *)pImageBuffer));
    } else {
      std::cerr << "Error: " << std::hex << ptrGrabResult->GetErrorCode()
                << std::dec << ptrGrabResult->GetErrorDescription()
                << std::endl;
    }
  }
  camera->StopGrabbing();
}

void Camera::load_config(const std::string &config) {
  CFeaturePersistence::LoadFromString(config.c_str(), &camera->GetNodeMap());
  frame_for_display.set_size(camera->Width.GetValue(),
                             camera->Height.GetValue());
}

CameraSystem::CameraSystem() {
  CTlFactory &tlFactory = CTlFactory::GetInstance();
  DeviceInfoList_t devices_;
  if (tlFactory.EnumerateDevices(devices_) == 0) {
    std::cerr << "No camera present." << std::endl;
  }
  for (size_t i = 0; i < devices_.size(); ++i) {
    cameras.emplace_back(tlFactory.CreateDevice(devices_[i]));
  }
}

CameraSystem::~CameraSystem() = default;

void CameraSystem::record(int n_frames) {
  std::vector<std::thread> grab_threads;
  for (auto &camera : cameras) {
    grab_threads.emplace_back([&camera, n_frames]() { camera.grab(n_frames); });
  }
  for (auto &thread : grab_threads) {
    thread.join();
  }
}

void CameraSystem::load_config(const std::string &directory) {
  for (auto &camera : cameras) {
    auto serial_number = camera.get_serial_number();
    std::string config_file = directory + "/" + serial_number + ".pfs";
    std::ifstream file(config_file);
    if (!file) {
      std::cerr << "Config file not found: " << config_file << std::endl;
      continue;
    }
    std::cout << "Loading config for camera: " << serial_number << std::endl;
    std::string content((std::istreambuf_iterator<char>(file)),
                        std::istreambuf_iterator<char>());
    camera.load_config(content);
  }
}

std::vector<Camera>::iterator CameraSystem::begin() { return cameras.begin(); }
std::vector<Camera>::iterator CameraSystem::end() { return cameras.end(); }
std::vector<Camera>::const_iterator CameraSystem::begin() const {
  return cameras.begin();
}
std::vector<Camera>::const_iterator CameraSystem::end() const {
  return cameras.end();
}