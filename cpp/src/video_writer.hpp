#pragma once

#include <atomic>
#include <condition_variable>
#include <mutex>
#include <queue>
#include <thread>

#include <opencv2/opencv.hpp>

class VideoWriter {
public:
  virtual ~VideoWriter() = default;

  virtual bool open(const std::string &filename, int fourcc, double fps,
                    cv::Size frame_size, bool is_color = true) = 0;
  virtual bool write(const cv::Mat &frame) = 0;
  virtual void close() = 0;
};

class OpencvVideoWriter : public VideoWriter {
public:
  OpencvVideoWriter(size_t max_queue_size = 30);
  ~OpencvVideoWriter();

  OpencvVideoWriter(const OpencvVideoWriter &) = delete;
  OpencvVideoWriter &operator=(const OpencvVideoWriter &) = delete;
  OpencvVideoWriter(OpencvVideoWriter &&) = delete;
  OpencvVideoWriter &operator=(OpencvVideoWriter &&) = delete;

  bool open(const std::string &filename, int fourcc, double fps,
            cv::Size frame_size, bool is_color = true) override;
  bool write(const cv::Mat &frame) override;
  void close() override;

private:
  void writer_thread_func();

  cv::VideoWriter writer_;
  std::queue<cv::Mat> frame_queue_;
  std::mutex mutex_;
  std::condition_variable cond_var_;
  std::thread writer_thread_;
  std::atomic<bool> running_;
  size_t max_queue_size_;
};