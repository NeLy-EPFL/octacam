#include "video_writer.h"

OpencvVideoWriter::OpencvVideoWriter(size_t maxQueueSize)
    : running_(false), isOpen_(false), maxQueueSize_(maxQueueSize) {}

OpencvVideoWriter::~OpencvVideoWriter() { close(); }

bool OpencvVideoWriter::open(const std::string &filename, int fourcc,
                             double fps, cv::Size frameSize, bool isColor) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (isOpen_)
    return false;

  if (!writer_.open(filename, fourcc, fps, frameSize, isColor)) {
    return false;
  }

  running_ = true;
  isOpen_ = true;
  writerThread_ = std::thread(&OpencvVideoWriter::writerThreadFunc, this);
  return true;
}

bool OpencvVideoWriter::write(const cv::Mat &frame) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (!isOpen_ || frame.empty())
    return false;

  if (frameQueue_.size() >= maxQueueSize_) {
    return false; // Drop frame
  }

  frameQueue_.push(frame.clone());
  condVar_.notify_one();
  return true;
}

void OpencvVideoWriter::close() {
  {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!isOpen_)
      return;

    running_ = false;
    condVar_.notify_all();
  }

  if (writerThread_.joinable()) {
    writerThread_.join();
  }

  std::lock_guard<std::mutex> lock(mutex_);
  while (!frameQueue_.empty()) {
    writer_ << frameQueue_.front();
    frameQueue_.pop();
  }

  writer_.release();
  isOpen_ = false;
}

void OpencvVideoWriter::writerThreadFunc() {
  while (running_) {
    std::unique_lock<std::mutex> lock(mutex_);
    condVar_.wait(lock, [this]() { return !frameQueue_.empty() || !running_; });

    while (!frameQueue_.empty()) {
      writer_ << frameQueue_.front();
      frameQueue_.pop();
    }
  }
}
