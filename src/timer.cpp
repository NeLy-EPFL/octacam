#include "timer.hpp"

PreciseTimer::PreciseTimer(std::function<void()> callback)
    : callback_(std::move(callback)), interval_{10000000}, running_{false} {
} // Use std::move for callback

PreciseTimer::~PreciseTimer() { stop(); }

void PreciseTimer::start(std::chrono::nanoseconds interval,
                         std::chrono::nanoseconds duration) {
  if (running_) {
    return;
  }
  interval_ = interval;
  total_duration_ = duration; // Set total_duration_
  running_ = true;
  thread_ = std::thread(&PreciseTimer::run_loop, this);
}

void PreciseTimer::start(std::chrono::nanoseconds interval) {
  if (running_) {
    return;
  }
  interval_ = interval;
  total_duration_.reset(); // Clear total_duration_ for indefinite run
  running_ = true;
  thread_ = std::thread(&PreciseTimer::run_loop, this);
}

void PreciseTimer::stop() {
  running_ = false;
  if (thread_.joinable()) {
    thread_.join();
  }
}

void PreciseTimer::run_loop() {
  auto next_time = std::chrono::steady_clock::now();
  std::optional<std::chrono::time_point<std::chrono::steady_clock>> end_time;

  if (total_duration_) {
    end_time = next_time + *total_duration_;
  }

  while (running_) {
    if (end_time && next_time >= *end_time) {
      break;
    }
    next_time += interval_;
    callback_();
    std::this_thread::sleep_until(next_time);
  }
  running_ = false; // Ensure running_ is set to false when loop exits
}