#pragma once

#include <atomic>
#include <chrono>
#include <functional>
#include <memory>
#include <thread>

class PreciseTimer {
public:
  PreciseTimer(std::function<void()> callback);
  ~PreciseTimer();
  void start(std::chrono::nanoseconds interval,
             std::chrono::nanoseconds duration);
  void start(std::chrono::nanoseconds interval);
  void stop();
  bool is_running() const;

private:
  void run();
  void run2();
  std::function<void()> callback_;
  std::chrono::nanoseconds interval_;
  std::chrono::nanoseconds duration_;
  std::atomic<bool> running_;
  std::thread thread_;
};