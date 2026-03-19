#include "video_writer.hpp"

OpencvVideoWriter::OpencvVideoWriter(size_t max_queue_size)
    : running_(false), max_queue_size_(max_queue_size) {}

OpencvVideoWriter::~OpencvVideoWriter() { close(); }

bool OpencvVideoWriter::open(const std::string &filename, int fourcc,
                             double fps, cv::Size frame_size, bool is_color) {
  close();

  {
    std::lock_guard<std::mutex> lock(mutex_);
    while (!frame_queue_.empty()) {
      frame_queue_.pop();
    }
    if (!writer_.open(filename, fourcc, fps, frame_size, is_color)) {
      return false;
    }
    running_ = true;
  }

  writer_thread_ = std::thread(&OpencvVideoWriter::writer_thread_func, this);
  return true;
}

bool OpencvVideoWriter::write(const cv::Mat &frame) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (!running_ || !writer_.isOpened()) {
    return false;
  }
  if (frame_queue_.size() >= max_queue_size_) {
    return false; // Drop frame
  }
  frame_queue_.push(frame.clone());
  cond_var_.notify_one();
  return true;
}

void OpencvVideoWriter::close() {
  {
    std::lock_guard<std::mutex> lock(mutex_);
    running_ = false;
    cond_var_.notify_all();
  }

  if (writer_thread_.joinable()) {
    writer_thread_.join();
  }

  writer_.release();
}

void OpencvVideoWriter::writer_thread_func() {
  while (true) {
    cv::Mat frame;

    {
      std::unique_lock<std::mutex> lock(mutex_);
      cond_var_.wait(lock,
                     [this]() { return !frame_queue_.empty() || !running_; });
      if (!running_ && frame_queue_.empty()) {
        break;
      }
      frame = std::move(frame_queue_.front());
      frame_queue_.pop();
    }

    if (!frame.empty()) {
      writer_ << frame;
    }
  }
}
