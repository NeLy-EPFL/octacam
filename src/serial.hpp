#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <type_traits>

#pragma pack(push, 1)
struct Command {
  int16_t n_steps;
  uint16_t step_interval_us;
  uint16_t rest_duration_ms;
  uint8_t n_repeats;
  uint8_t init_wait_duration_s;
};
#pragma pack(pop)

class SerialPort {
public:
  SerialPort() = default;
  SerialPort(const std::string &device, int baud) { open(device, baud); }
  ~SerialPort();

  SerialPort(const SerialPort &) = delete;
  SerialPort &operator=(const SerialPort &) = delete;
  SerialPort(SerialPort &&other) noexcept;
  SerialPort &operator=(SerialPort &&other) noexcept;

  void open(const std::string &device, int baud);
  void close();
  bool is_open() const noexcept { return fd_ >= 0; }

  std::size_t write(const void *data, std::size_t len);
  template <typename T> std::size_t writeAll(const T &value) {
    static_assert(std::is_trivially_copyable_v<T>,
                  "writeAll requires trivially copyable types");
    return write(&value, sizeof(T));
  }
  std::size_t read(void *out, std::size_t len, int timeoutMs);

private:
  int fd_ = -1;
  static unsigned long to_speed(int baud);
  void configure(int baud);
};