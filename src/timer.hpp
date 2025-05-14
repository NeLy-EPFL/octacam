#pragma once

#include <atomic>
#include <chrono>
#include <functional>
#include <memory>
#include <thread>

class PreciseTimer {
public:
  explicit PreciseTimer(std::function<void()> callback);
  ~PreciseTimer();

  PreciseTimer(const PreciseTimer &) = delete;
  PreciseTimer &operator=(const PreciseTimer &) = delete;
  PreciseTimer(PreciseTimer &&) = delete;
  PreciseTimer &operator=(PreciseTimer &&) = delete;

  void set_frequency(const double &hz);
  void start(std::chrono::nanoseconds duration);
  void start();
  void stop();

private:
  void run_indefinite();
  void run_until(std::chrono::nanoseconds duration);
  std::function<void()> callback_;
  std::atomic<std::chrono::nanoseconds> interval_;
  std::atomic<bool> running_;
  std::thread thread_;
};