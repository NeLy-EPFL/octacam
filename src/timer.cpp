#include "timer.hpp"

PreciseTimer::PreciseTimer(std::function<void()> callback)
    : callback_(callback), interval_{10000000}, running_{false} {}
PreciseTimer::~PreciseTimer() { stop(); }

void PreciseTimer::start(std::chrono::nanoseconds interval,
                         std::chrono::nanoseconds duration) {
  if (running_)
    return;
  interval_ = interval;
  duration_ = duration;
  running_ = true;
  thread_ = std::thread(&PreciseTimer::run, this);
}

void PreciseTimer::start(std::chrono::nanoseconds interval) {
  if (running_)
    return;
  interval_ = interval;
  running_ = true;
  thread_ = std::thread(&PreciseTimer::run2, this);
}

void PreciseTimer::stop() {
  running_ = false;
  if (thread_.joinable()) {
    thread_.join();
  }
}

void PreciseTimer::run() {
  auto next_time = std::chrono::steady_clock::now();
  auto end_time = next_time + duration_;

  while (running_ && (next_time < end_time)) {
    next_time += interval_;
    callback_();
    std::this_thread::sleep_until(next_time);
  }
  running_ = false;
}

void PreciseTimer::run2() {
  auto next_time = std::chrono::steady_clock::now();
  while (running_) {
    next_time += interval_;
    callback_();
    std::this_thread::sleep_until(next_time);
  }
  running_ = false;
}