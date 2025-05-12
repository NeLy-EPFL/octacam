#include "camera.h"

#include <chrono>
#include <filesystem>
#include <fstream>
#include <future>
#include <iostream>
#include <thread>

FrameForDisplay::FrameForDisplay() = default;

FrameForDisplay::~FrameForDisplay() { delete[] data; }

FrameForDisplay::FrameForDisplay(FrameForDisplay &&other)
    : data(other.data), width(other.width), height(other.height),
      size(other.size), retrieved(other.retrieved) {
  other.data = nullptr;
  other.width = 0;
  other.height = 0;
  other.size = 0;
  other.retrieved = true;
}

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
      video_writer(std::make_unique<OpencvVideoWriter>(20)), system(system) {
  camera->Open();
}

Camera::~Camera() {
  stop_flag = true;
  if (future.valid()) {
    future.get();
  }
}

Camera::Camera(Camera &&other)
    : camera(std::move(other.camera)), system(other.system),
      video_writer(std::move(other.video_writer)),
      frame_for_display(std::move(other.frame_for_display)),
      stop_flag(other.stop_flag.load()), future(std::move(other.future)) {
  other.camera = nullptr;
  other.stop_flag = true;
}

std::string Camera::get_serial_number() const {
  return std::string(camera->GetDeviceInfo().GetSerialNumber().c_str());
}

void Camera::start_preview() {
  stop_flag = false;
  this->camera->StartGrabbing(Pylon::GrabStrategy_LatestImageOnly);
  future = std::async(std::launch::async, [this]() {
    while (!this->stop_flag && this->camera->IsGrabbing()) {
      Pylon::CGrabResultPtr ptrGrabResult;
      camera->RetrieveResult(33, ptrGrabResult, Pylon::TimeoutHandling_Return);
      if (ptrGrabResult && ptrGrabResult->GrabSucceeded()) {
        const uint8_t *pImageBuffer = (uint8_t *)ptrGrabResult->GetBuffer();
        this->frame_for_display.store_frame(pImageBuffer);
      }
    }
    this->camera->StopGrabbing();
  });
}

void Camera::start_record() {
  auto opened = video_writer->open(
      get_serial_number() + ".avi", cv::VideoWriter::fourcc('M', 'J', 'P', 'G'),
      30, cv::Size(camera->Width.GetValue(), camera->Height.GetValue()), false);

  if (!opened) {
    std::cerr << "Failed to open video writer" << std::endl;
    return;
  }

  stop_flag = false;
  camera->StartGrabbing(Pylon::GrabStrategy_OneByOne);
  auto ready =
      camera->WaitForFrameTriggerReady(1000, Pylon::TimeoutHandling_Return);

  if (!ready) {
    std::cerr << "Failed to start grabbing" << std::endl;
    return;
  }

  future = std::async(std::launch::async, [this]() {
    while (camera->IsGrabbing() && !stop_flag) {
      Pylon::CGrabResultPtr ptrGrabResult;
      camera->RetrieveResult(33, ptrGrabResult, Pylon::TimeoutHandling_Return);
      if (ptrGrabResult && ptrGrabResult->GrabSucceeded()) {
        const uint8_t *pImageBuffer = (uint8_t *)ptrGrabResult->GetBuffer();
        frame_for_display.store_frame(pImageBuffer);
        bool written = video_writer->write(
            cv::Mat(camera->Height.GetValue(), camera->Width.GetValue(),
                    CV_8UC1, (void *)pImageBuffer));
        if (!written) {
          std::cerr << "Frame dropped" << std::endl;
        }
      }
    }
    camera->StopGrabbing();
    video_writer->close();
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
  camera->TriggerSource.SetValue(
      Basler_UniversalCameraParams::TriggerSource_Software);
}

void Camera::trigger_once() {
  // if (camera->IsGrabbing()) {
  camera->ExecuteSoftwareTrigger();
  // }
}

CameraSystem::CameraSystem()
    : trigger_timer([this]() {
        for (auto &c : cameras) {
          c.trigger_once();
        }
      }) {
  auto &tlFactory = Pylon::CTlFactory::GetInstance();
  Pylon::DeviceInfoList_t devices;
  if (tlFactory.EnumerateDevices(devices) == 0) {
    std::cerr << "No camera present." << std::endl;
  }
  for (size_t i = 0; i < devices.size(); ++i) {
    cameras.emplace_back(tlFactory.CreateDevice(devices[i]), *this);
  }
}

CameraSystem::~CameraSystem() { trigger_timer.stop(); }

void CameraSystem::load_config(const std::string &directory) {
  for (auto &camera : cameras) {
    auto serial_number = camera.get_serial_number();
    std::filesystem::path config_file =
        std::filesystem::path(directory) / (serial_number + ".pfs");
    std::ifstream file(config_file);
    if (file) {
      std::cout << "Loading config for camera: " << serial_number << std::endl;
      std::string content((std::istreambuf_iterator<char>(file)),
                          std::istreambuf_iterator<char>());
      camera.load_config(content);
    } else {
      camera.load_config("");
      std::cerr << "Warning: config file not found at " << config_file
                << std::endl;
    }
  }
}

void CameraSystem::start_preview() {
  stop();
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

void CameraSystem::start_software_trigger(std::chrono::nanoseconds interval,
                                          std::chrono::nanoseconds duration) {
  trigger_timer.start(interval, duration);
}
void CameraSystem::start_software_trigger(std::chrono::nanoseconds interval) {
  trigger_timer.start(interval);
}
void CameraSystem::stop_software_trigger() { trigger_timer.stop(); }
bool CameraSystem::is_software_trigger_running() const {
  return trigger_timer.is_running();
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