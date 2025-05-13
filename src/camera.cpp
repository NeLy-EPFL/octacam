#include "camera.hpp"

#include <algorithm>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <future>
#include <iostream>
#include <thread>

namespace {
constexpr int GRAB_TIMEOUT_MS = 1000;
constexpr int TRIGGER_READY_TIMEOUT_MS = 1000;
} // namespace

FrameForDisplay::FrameForDisplay() : retrieved_(false) {}

FrameForDisplay::~FrameForDisplay() = default;

FrameForDisplay::FrameForDisplay(FrameForDisplay &&other) noexcept
    : width_(other.width_), height_(other.height_), size_(other.size_),
      data_(std::move(other.data_)), retrieved_(other.retrieved_.load()) {
  other.width_ = 0;
  other.height_ = 0;
  other.size_ = 0;
  other.retrieved_ = true;
}

FrameForDisplay &FrameForDisplay::operator=(FrameForDisplay &&other) noexcept {
  if (this != &other) {
    std::lock(mtx_, other.mtx_);
    std::lock_guard<std::mutex> lhs_lock(mtx_, std::adopt_lock);
    std::lock_guard<std::mutex> rhs_lock(other.mtx_, std::adopt_lock);

    width_ = other.width_;
    height_ = other.height_;
    size_ = other.size_;
    data_ = std::move(other.data_);
    retrieved_ = other.retrieved_.load();

    other.width_ = 0;
    other.height_ = 0;
    other.size_ = 0;
    other.retrieved_ = true;
  }
  return *this;
}

std::optional<QPixmap> FrameForDisplay::retrieve_as_pixmap() {
  std::lock_guard<std::mutex> lock(mtx_);
  if (retrieved_.load()) {
    return std::nullopt;
  }
  if (!data_) {
    return std::nullopt;
  }
  QImage image(static_cast<const uint8_t *>(data_.get()), width_, height_,
               QImage::Format_Grayscale8);
  auto pixmap = QPixmap::fromImage(image);
  retrieved_ = true;
  return pixmap;
}

void FrameForDisplay::store_frame(const uint8_t *raw_data_ptr) {
  std::unique_lock<std::mutex> lock(mtx_, std::try_to_lock);
  if (lock.owns_lock() && retrieved_.load()) {
    if (data_ && size_ > 0) {
      std::copy(raw_data_ptr, raw_data_ptr + size_, data_.get());
      retrieved_ = false;
    }
  }
}

void FrameForDisplay::update_size(int new_width, int new_height) {
  std::lock_guard<std::mutex> lock(mtx_);
  if (width_ != new_width || height_ != new_height) {
    width_ = new_width;
    height_ = new_height;
    size_ = static_cast<size_t>(width_) * height_;
    if (size_ > 0) {
      data_ = std::make_unique<uint8_t[]>(size_);
    } else {
      data_.reset();
    }
  }
}

Camera::Camera(Pylon::IPylonDevice *device, const CameraSystem &system)
    : camera_(std::make_unique<Pylon::CBaslerUniversalInstantCamera>(device)),
      video_writer_(std::make_unique<OpencvVideoWriter>(20)), system_(system),
      started_(false), stop_flag_(false) {
  camera_->Open();
}

Camera::~Camera() {
  stop_flag_ = true;
  if (future_.valid()) {
    future_.wait();
  }
}

Camera::Camera(Camera &&other) noexcept
    : camera_(std::move(other.camera_)),
      video_writer_(std::move(other.video_writer_)), system_(other.system_),
      frame_for_display_(std::move(other.frame_for_display_)),
      started_(other.started_.load()), stop_flag_(other.stop_flag_.load()),
      future_(std::move(other.future_)) {
  other.stop_flag_ = true;
}

Camera &Camera::operator=(Camera &&other) noexcept {
  if (this != &other) {
    stop_flag_ = true;
    if (future_.valid()) {
      future_.wait();
    }

    camera_ = std::move(other.camera_);
    video_writer_ = std::move(other.video_writer_);
    frame_for_display_ = std::move(other.frame_for_display_);
    started_ = other.started_.load();
    stop_flag_ = other.stop_flag_.load();
    future_ = std::move(other.future_);

    other.stop_flag_ = true;
  }
  return *this;
}

std::string Camera::get_serial_number() const {
  if (!camera_) {
    return "N/A";
  }
  return std::string(camera_->GetDeviceInfo().GetSerialNumber().c_str());
}

void Camera::start_preview() {
  if (!camera_) {
    return;
  }
  stop_flag_ = false;
  camera_->StartGrabbing(Pylon::GrabStrategy_LatestImageOnly);
  future_ = std::async(std::launch::async, [this]() {
    while (!this->stop_flag_ && this->camera_ && this->camera_->IsGrabbing()) {
      Pylon::CGrabResultPtr ptrGrabResult;
      camera_->RetrieveResult(GRAB_TIMEOUT_MS, ptrGrabResult,
                              Pylon::TimeoutHandling_Return);
      if (ptrGrabResult && ptrGrabResult->GrabSucceeded()) {
        const auto *pImageBuffer =
            static_cast<const uint8_t *>(ptrGrabResult->GetBuffer());
        this->frame_for_display_.store_frame(pImageBuffer);
      }
    }
    if (this->camera_) {
      this->camera_->StopGrabbing();
    }
  });
}

void Camera::start_record(const std::string &save_path, const double &fps,
                          const std::string &fourcc_str) {
  if (!camera_ || !video_writer_) {
    return;
  }
  auto fourcc_int = cv::VideoWriter::fourcc(fourcc_str[0], fourcc_str[1],
                                            fourcc_str[2], fourcc_str[3]);
  auto frame_size =
      cv::Size(camera_->Width.GetValue(), camera_->Height.GetValue());
  bool opened =
      video_writer_->open(save_path, fourcc_int, fps, frame_size, false);

  if (!opened) {
    std::cerr << "Failed to open video writer for: " << save_path << '\n';
    return;
  }

  stop_flag_ = false;
  started_ = false;
  camera_->StartGrabbing(Pylon::GrabStrategy_OneByOne);
  bool ready = camera_->WaitForFrameTriggerReady(TRIGGER_READY_TIMEOUT_MS,
                                                 Pylon::TimeoutHandling_Return);

  if (!ready) {
    std::cerr << "Failed to start grabbing for recording on camera "
              << get_serial_number() << '\n';
    video_writer_->close();
    return;
  }

  future_ = std::async(std::launch::async, [this]() {
    bool local_started_flag = false;
    while (this->camera_ && camera_->IsGrabbing() && !stop_flag_) {
      Pylon::CGrabResultPtr ptrGrabResult;
      camera_->RetrieveResult(GRAB_TIMEOUT_MS, ptrGrabResult,
                              Pylon::TimeoutHandling_Return);
      if (ptrGrabResult && ptrGrabResult->GrabSucceeded()) {
        const auto *pImageBuffer =
            static_cast<const uint8_t *>(ptrGrabResult->GetBuffer());
        bool written = video_writer_->write(
            cv::Mat(camera_->Height.GetValue(), camera_->Width.GetValue(),
                    CV_8UC1, const_cast<uint8_t *>(pImageBuffer)));
        if (!written) {
          std::cerr << "Frame dropped for camera " << get_serial_number()
                    << '\n';
        }
        frame_for_display_.store_frame(pImageBuffer);
        if (!local_started_flag) {
          local_started_flag = true;
          started_ = true;
        }
      }
    }
    if (this->camera_) {
      camera_->StopGrabbing();
    }
    if (this->video_writer_) {
      video_writer_->close();
    }
  });
}

void Camera::load_config(const std::string &config_str) {
  if (!camera_) {
    return;
  }
  Pylon::CFeaturePersistence::LoadFromString(config_str.c_str(),
                                             &camera_->GetNodeMap());
  frame_for_display_.update_size(camera_->Width.GetValue(),
                                 camera_->Height.GetValue());
  camera_->TriggerMode.SetValue(Basler_UniversalCameraParams::TriggerMode_On);
  camera_->TriggerSource.SetValue(
      Basler_UniversalCameraParams::TriggerSource_Software);
}

void Camera::trigger_once() {
  if (camera_ && camera_->IsGrabbing()) {
    camera_->ExecuteSoftwareTrigger();
  }
}

CameraSystem::CameraSystem()
    : trigger_timer_([this]() {
        for (auto &cam : cameras_) {
          cam.trigger_once();
        }
      }) {
  Pylon::CTlFactory &tlFactory = Pylon::CTlFactory::GetInstance();
  Pylon::DeviceInfoList_t devices;
  if (tlFactory.EnumerateDevices(devices) == 0) {
    std::cerr << "No camera present." << '\n';
  }
  cameras_.reserve(devices.size());
  for (size_t i = 0; i < devices.size(); ++i) {
    cameras_.emplace_back(tlFactory.CreateDevice(devices[i]), *this);
  }
}

CameraSystem::~CameraSystem() {
  stop_software_trigger();
  stop();
}

void CameraSystem::load_config(const std::string &directory_str) {
  for (auto &camera : cameras_) {
    auto serial_number = camera.get_serial_number();
    std::filesystem::path config_file_path =
        std::filesystem::path(directory_str) / (serial_number + ".pfs");
    std::ifstream file_stream(config_file_path);
    if (file_stream) {
      std::cout << "Loading config for camera: " << serial_number << '\n';
      std::string content_str((std::istreambuf_iterator<char>(file_stream)),
                              std::istreambuf_iterator<char>());
      camera.load_config(content_str);
    } else {
      camera.load_config("");
      std::cerr << "Warning: config file not found at " << config_file_path
                << '\n';
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
        (camera.get_serial_number() + "." + extension_str);
    camera.start_record(save_path_obj.string(), fps_val, fourcc_str);
  }
}

void CameraSystem::start_software_trigger(std::chrono::nanoseconds interval,
                                          std::chrono::nanoseconds duration) {
  trigger_timer_.start(interval, duration);
}
void CameraSystem::start_software_trigger(std::chrono::nanoseconds interval) {
  trigger_timer_.start(interval);
}
void CameraSystem::stop_software_trigger() { trigger_timer_.stop(); }

bool CameraSystem::all_cameras_started() const {
  return std::all_of(
      cameras_.begin(), cameras_.end(),
      [](const Camera &camera) { return camera.started_.load(); });
}

std::vector<std::optional<QPixmap>> CameraSystem::get_pixmaps() {
  std::vector<std::optional<QPixmap>> pixmaps_vec;
  pixmaps_vec.reserve(cameras_.size());
  for (auto &camera : cameras_) {
    pixmaps_vec.push_back(camera.frame_for_display_.retrieve_as_pixmap());
  }
  return pixmaps_vec;
}

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