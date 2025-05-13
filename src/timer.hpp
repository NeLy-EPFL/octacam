#pragma once

#include <atomic>
#include <chrono>
#include <functional>
#include <memory>
#include <optional> // Required for std::optional
#include <thread>

class PreciseTimer {
public:
  explicit PreciseTimer(
      std::function<void()> callback); // Pass by value for sink
  ~PreciseTimer();

  // Rule of Five: Make non-copyable
  PreciseTimer(const PreciseTimer &) = delete;
  PreciseTimer &operator=(const PreciseTimer &) = delete;
  PreciseTimer(PreciseTimer &&) = delete; // Or implement custom move if needed
  PreciseTimer &
  operator=(PreciseTimer &&) = delete; // Or implement custom move if needed

  void start(std::chrono::nanoseconds interval,
             std::chrono::nanoseconds duration);
  void start(std::chrono::nanoseconds interval);
  void stop();

private:
  void run_loop(); // Renamed and unified run method
  std::function<void()> callback_;
  std::chrono::nanoseconds interval_;
  std::optional<std::chrono::nanoseconds>
      total_duration_; // For unified run_loop
  std::atomic<bool> running_;
  std::thread thread_;
};