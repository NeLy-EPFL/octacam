#pragma once

#include <opencv2/opencv.hpp>

#include <atomic>
#include <condition_variable>
#include <mutex>
#include <queue>
#include <thread>

class VideoWriter {
public:
  virtual ~VideoWriter() = default;

  virtual bool open(const std::string &filename, int fourcc, double fps,
                    cv::Size frameSize, bool isColor = true) = 0;
  virtual bool write(const cv::Mat &frame) = 0;
  virtual void close() = 0;
};

class OpencvVideoWriter : public VideoWriter {
public:
  OpencvVideoWriter(size_t maxQueueSize = 30);
  ~OpencvVideoWriter();

  bool open(const std::string &filename, int fourcc, double fps,
            cv::Size frameSize, bool isColor = true) override;
  bool write(const cv::Mat &frame) override;
  void close() override;

private:
  void writerThreadFunc();

  cv::VideoWriter writer_;
  std::queue<cv::Mat> frameQueue_;
  std::mutex mutex_;
  std::condition_variable condVar_;
  std::thread writerThread_;
  std::atomic<bool> running_;
  std::atomic<bool> isOpen_;
  size_t maxQueueSize_;
};