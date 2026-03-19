#include "timer.hpp"

#include <cmath>

PreciseTimer::PreciseTimer(std::function<void()> callback)
    : callback_(std::move(callback)),
      interval_{std::chrono::nanoseconds(10'000'000)}, running_{false} {}

PreciseTimer::~PreciseTimer() { stop(); }

void PreciseTimer::set_frequency(const double &hz) {
  if (hz <= 0.0) {
    return;
  }
  interval_ =
      std::chrono::nanoseconds(static_cast<long long>(std::round(1e9 / hz)));
}

void PreciseTimer::start(std::chrono::nanoseconds duration) {
  if (running_) {
    return;
  }
  running_ = true;
  thread_ = std::thread(&PreciseTimer::run_until, this, duration);
}

void PreciseTimer::start() {
  if (running_) {
    return;
  }
  running_ = true;
  thread_ = std::thread(&PreciseTimer::run_indefinite, this);
}

void PreciseTimer::stop() {
  running_ = false;
  if (thread_.joinable()) {
    thread_.join();
  }
}

void PreciseTimer::run_indefinite() {
  auto next_time = std::chrono::steady_clock::now();

  while (running_) {
    next_time += interval_.load();
    callback_();
    std::this_thread::sleep_until(next_time);
  }
  running_ = false;
}

void PreciseTimer::run_until(std::chrono::nanoseconds duration) {
  auto next_time = std::chrono::steady_clock::now();
  auto end_time = next_time + duration;

  while (running_ && next_time < end_time) {
    next_time += interval_.load();
    callback_();
    std::this_thread::sleep_until(next_time);
  }
  running_ = false;
}