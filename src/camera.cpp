#include "camera.hpp"

#include <algorithm>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <future>
#include <iostream>
#include <thread>

#include <spdlog/spdlog.h>

namespace {
constexpr int GRAB_TIMEOUT_MS = 100;
constexpr int TRIGGER_READY_TIMEOUT_MS = 1000;
} // namespace

FrameForDisplay::FrameForDisplay() = default;

FrameForDisplay::~FrameForDisplay() = default;

cv::Mat *FrameForDisplay::pop() {
  std::lock_guard<std::mutex> lock(mtx_);
  if (mat_) {
    cv::Mat *temp = mat_;
    mat_ = nullptr;
    return temp;
  }
  return nullptr;
}

bool FrameForDisplay::push(const cv::Mat &frame) {
  std::unique_lock<std::mutex> lock(mtx_, std::try_to_lock);
  if (lock.owns_lock() && mat_ == nullptr) {
    mat_ = new cv::Mat(frame.clone());
    return true;
  }
  return false;
}

Camera::Camera(Pylon::IPylonDevice *device, const CameraSystem &system)
    : camera_(std::make_unique<Pylon::CBaslerUniversalInstantCamera>(device)),
      video_writer_(std::make_unique<OpencvVideoWriter>(20)), started_(false),
      stop_flag_(false) {
  camera_->Open();
  name_ = get_serial_number();
}

Camera::~Camera() {
  stop_flag_ = true;
  if (future_.valid()) {
    future_.wait();
  }
}

Camera::Camera(Camera &&other) noexcept
    : camera_(std::move(other.camera_)),
      video_writer_(std::move(other.video_writer_)),
      frame_for_display_(std::move(other.frame_for_display_)),
      started_(other.started_.load()), stop_flag_(other.stop_flag_.load()),
      future_(std::move(other.future_)),
      timestamps_(std::move(other.timestamps_)),
      resulting_fps_(other.resulting_fps_.load()) {
  other.stop_flag_ = true;
}

std::string Camera::get_serial_number() const {
  if (!camera_) {
    return "N/A";
  }
  return std::string(camera_->GetDeviceInfo().GetSerialNumber().c_str());
}

void Camera::set_name(const std::string &name) {
  if (!camera_) {
    return;
  }
  name_ = name;
}

std::string Camera::get_name() const { return name_; }

inline void Camera::update_resulting_fps(size_t window_size) {
  if (timestamps_.size() < 2 || window_size < 1) {
    resulting_fps_ = 0.0;
    return;
  }

  size_t last_index = timestamps_.size() - 1;
  size_t start_index =
      (last_index > window_size) ? last_index - window_size : 0;
  size_t actual_window_size = last_index - start_index;
  uint64_t delta_ns = timestamps_[last_index] - timestamps_[start_index];

  if (delta_ns == 0) {
    resulting_fps_ = 0.0;
    return;
  }

  resulting_fps_ = static_cast<double>(actual_window_size * 1e9) /
                   static_cast<double>(delta_ns);
}

inline void
Camera::store_timestamp(const Pylon::CGrabResultPtr &ptrGrabResult) {
  uint64_t timestamp = ptrGrabResult->GetTimeStamp();
  if (timestamp == 0) {
    auto now = std::chrono::high_resolution_clock::now();
    timestamp = std::chrono::duration_cast<std::chrono::nanoseconds>(
                    now.time_since_epoch())
                    .count();
  }
  timestamps_.push_back(timestamp);
}

void Camera::start_preview() {
  stop_flag_ = false;
  if (!camera_ || !camera_->IsOpen()) {
    return;
  }
  camera_->TriggerMode.SetValue(Basler_UniversalCameraParams::TriggerMode_On);
  camera_->TriggerSource.SetValue(
      Basler_UniversalCameraParams::TriggerSource_Software);
  timestamps_.clear();
  camera_->StartGrabbing(Pylon::GrabStrategy_LatestImageOnly);
  future_ = std::async(std::launch::async, [this]() {
    while (!stop_flag_ && camera_ && camera_->IsGrabbing()) {
      Pylon::CGrabResultPtr ptrGrabResult;
      camera_->RetrieveResult(GRAB_TIMEOUT_MS, ptrGrabResult,
                              Pylon::TimeoutHandling_Return);
      if (ptrGrabResult && ptrGrabResult->GrabSucceeded()) {
        store_timestamp(ptrGrabResult);

        void *pImageBuffer = ptrGrabResult->GetBuffer();
        auto stored = frame_for_display_.push(
            cv::Mat(camera_->Height.GetValue(), camera_->Width.GetValue(),
                    CV_8UC1, pImageBuffer));
        if (stored) {
          update_resulting_fps();
        }
      }
    }
    if (camera_) {
      camera_->StopGrabbing();
    }
  });
}

void Camera::start_record(const std::string &save_path, const double &fps,
                          const std::string &fourcc_str) {
  stop_flag_ = false;
  started_ = false;

  if (!camera_ || !camera_->IsOpen() || !video_writer_) {
    return;
  }
  timestamps_.clear();
  auto fourcc_int = cv::VideoWriter::fourcc(fourcc_str[0], fourcc_str[1],
                                            fourcc_str[2], fourcc_str[3]);
  auto frame_size =
      cv::Size(camera_->Width.GetValue(), camera_->Height.GetValue());
  bool opened =
      video_writer_->open(save_path, fourcc_int, fps, frame_size, false);

  if (!opened) {
    spdlog::error("Failed to open video writer for: {}", save_path);
    return;
  }

  camera_->StartGrabbing(Pylon::GrabStrategy_OneByOne);
  bool ready = camera_->WaitForFrameTriggerReady(TRIGGER_READY_TIMEOUT_MS,
                                                 Pylon::TimeoutHandling_Return);

  if (!ready) {
    spdlog::error("Failed to start grabbing for recording on camera {}",
                  get_serial_number());
    video_writer_->close();
    return;
  }

  future_ = std::async(std::launch::async, [this]() {
    bool local_started_flag = false;
    while (camera_ && camera_->IsGrabbing() && !stop_flag_) {
      Pylon::CGrabResultPtr ptrGrabResult;
      camera_->RetrieveResult(GRAB_TIMEOUT_MS, ptrGrabResult,
                              Pylon::TimeoutHandling_Return);
      if (ptrGrabResult && ptrGrabResult->GrabSucceeded()) {
        store_timestamp(ptrGrabResult);
        void *pImageBuffer = ptrGrabResult->GetBuffer();
        cv::Mat frame(camera_->Height.GetValue(), camera_->Width.GetValue(),
                      CV_8UC1, pImageBuffer);
        bool written = video_writer_->write(frame);
        if (!written) {
          spdlog::warn("Frame dropped for camera {}", get_serial_number());
        }
        auto stored = frame_for_display_.push(frame);
        if (stored) {
          update_resulting_fps();
        }

        if (!local_started_flag) {
          local_started_flag = true;
          started_ = true;
        }
      }
    }
    if (camera_) {
      camera_->StopGrabbing();
    }
    if (video_writer_) {
      video_writer_->close();
    }
  });
}

void Camera::load_params(const std::string &config_str) {
  if (!camera_) {
    return;
  }
  Pylon::CFeaturePersistence::LoadFromString(config_str.c_str(),
                                             &camera_->GetNodeMap());
  cv::Mat frame(camera_->Height.GetValue(), camera_->Width.GetValue(), CV_8UC1);
  frame_for_display_.push(frame);
  original_trigger_source_ = camera_->TriggerSource.GetValue();
}

void Camera::trigger_once() {
  if (camera_ && camera_->IsGrabbing()) {
    camera_->ExecuteSoftwareTrigger();
  }
}

CameraSystem::CameraSystem(
    const std::vector<std::string> &requested_serial_numbers)
    : trigger_timer_([this]() {
        for (auto &cam : cameras_) {
          cam.trigger_once();
        }
      }) {
  Pylon::CTlFactory &tl_factory = Pylon::CTlFactory::GetInstance();
  Pylon::DeviceInfoList_t devices;
  auto n_devices = tl_factory.EnumerateDevices(devices);

  if (n_devices == 0) {
    return;
  }

  spdlog::info("Detected {} camera(s)", n_devices);

  std::vector<std::string> detected_serial_numbers;

  for (auto &device : devices) {
    detected_serial_numbers.push_back(
        std::string(device.GetSerialNumber().c_str()));
  }

  std::vector<std::string> final_serial_numbers;

  if (requested_serial_numbers.empty()) {
    final_serial_numbers = detected_serial_numbers;
    std::sort(final_serial_numbers.begin(), final_serial_numbers.end());
  } else {
    final_serial_numbers = requested_serial_numbers;
  }

  for (auto serial_number : final_serial_numbers) {
    size_t i = std::find(detected_serial_numbers.begin(),
                         detected_serial_numbers.end(), serial_number) -
               detected_serial_numbers.begin();
    if (i >= detected_serial_numbers.size()) {
      spdlog::warn("Camera with serial number {} not found", serial_number);
      continue;
    }
    cameras_.emplace_back(tl_factory.CreateDevice(devices[i]), *this);
  }
}

CameraSystem::~CameraSystem() {
  stop_software_trigger();
  stop();
}

void CameraSystem::load_config(const std::filesystem::path &directory) {
  for (auto &camera : cameras_) {
    auto serial_number = camera.get_serial_number();
    auto config_path = directory / (serial_number + ".pfs");
    std::ifstream file_stream(config_path);
    if (file_stream) {
      spdlog::info("Loading parameters for camera: {}", serial_number);
      std::string content_str((std::istreambuf_iterator<char>(file_stream)),
                              std::istreambuf_iterator<char>());
      camera.load_params(content_str);
    } else {
      camera.load_params("");
      spdlog::warn("Parameters file not found at {}", config_path.string());
    }
  }
}

void CameraSystem::start_preview() {
  stop();
  for (auto &camera : cameras_) {
    camera.start_preview();
  }
}

void CameraSystem::start_record(const std::string &save_dir_str,
                                const double &fps_val,
                                const std::string &fourcc_str,
                                const std::string &extension_str) {
  stop();
  for (auto &camera : cameras_) {
    std::filesystem::path save_path_obj =
        std::filesystem::path(save_dir_str) /
        (camera.get_name() + "." + extension_str);
    camera.start_record(save_path_obj.string(), fps_val, fourcc_str);
  }
}

void CameraSystem::set_software_trigger_frequency(const double &hz) {
  trigger_timer_.set_frequency(hz);
}

void CameraSystem::start_software_trigger(std::chrono::nanoseconds duration) {
  trigger_timer_.start(duration);
}
void CameraSystem::start_software_trigger() { trigger_timer_.start(); }
void CameraSystem::stop_software_trigger() { trigger_timer_.stop(); }

bool CameraSystem::all_cameras_started() const {
  return std::all_of(
      cameras_.begin(), cameras_.end(),
      [](const Camera &camera) { return camera.started_.load(); });
}

void CameraSystem::set_trigger_source(const bool &use_software_trigger) {
  for (auto &camera : cameras_) {
    if (camera.camera_ && camera.camera_->IsOpen()) {
      if (use_software_trigger) {
        camera.camera_->TriggerSource.SetValue(
            Basler_UniversalCameraParams::TriggerSource_Software);
      } else {
        camera.camera_->TriggerSource.SetValue(camera.original_trigger_source_);
      }
    }
  }
}

std::vector<std::pair<cv::Mat *, double>> CameraSystem::get_mats_and_fps() {
  std::vector<std::pair<cv::Mat *, double>> pixmaps_vec;
  pixmaps_vec.reserve(cameras_.size());
  for (auto &camera : cameras_) {
    auto *mat = camera.frame_for_display_.pop();
    if (mat) {
      pixmaps_vec.emplace_back(
          std::make_pair(mat, camera.resulting_fps_.load()));
    } else {
      pixmaps_vec.emplace_back(std::make_pair(nullptr, 0.0));
    }
  }
  return pixmaps_vec;
}

int CameraSystem::get_camera_count() const { return cameras_.size(); }

std::vector<Camera>::iterator CameraSystem::begin() { return cameras_.begin(); }
std::vector<Camera>::iterator CameraSystem::end() { return cameras_.end(); }
std::vector<Camera>::const_iterator CameraSystem::begin() const {
  return cameras_.cbegin();
}
std::vector<Camera>::const_iterator CameraSystem::end() const {
  return cameras_.cend();
}

void CameraSystem::stop() {
  for (auto &camera : cameras_) {
    camera.stop_flag_ = true;
  }
  for (auto &camera : cameras_) {
    if (camera.future_.valid()) {
      camera.future_.wait();
    }
  }
}