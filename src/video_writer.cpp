#include "video_writer.hpp"

OpencvVideoWriter::OpencvVideoWriter(size_t maxQueueSize)
    : running_(false), maxQueueSize_(maxQueueSize) {}

OpencvVideoWriter::~OpencvVideoWriter() { close(); }

bool OpencvVideoWriter::open(const std::string &filename, int fourcc,
                             double fps, cv::Size frameSize, bool isColor) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (!writer_.open(filename, fourcc, fps, frameSize, isColor)) {
    return false;
  }

  running_ = true;
  writerThread_ = std::thread(&OpencvVideoWriter::writerThreadFunc, this);
  return true;
}

bool OpencvVideoWriter::write(const cv::Mat &frame) {
  std::lock_guard<std::mutex> lock(mutex_);
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
    running_ = false;
    condVar_.notify_all();
  }

  if (writerThread_.joinable()) {
    writerThread_.join();
  }

  writer_.release();
}

void OpencvVideoWriter::writerThreadFunc() {
  while (running_) {
    cv::Mat frame;
    {
      std::unique_lock<std::mutex> lock(mutex_);
      condVar_.wait(lock, [this]() { return !frameQueue_.empty() || !running_; });
      if (!running_) {
        break;
      }
      frame = std::move(frameQueue_.front());
      frameQueue_.pop();
    }
    writer_ << frame;
  }

  std::lock_guard<std::mutex> lock(mutex_);
  while (!frameQueue_.empty()) {
    writer_ << frameQueue_.front();
    frameQueue_.pop();
  }
}
