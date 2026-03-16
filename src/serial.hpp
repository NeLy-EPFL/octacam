#pragma once

#include <cstdint>
#include <string>

struct Command {
  uint8_t n_steps;
};

class SerialPort {
public:
  SerialPort(const std::string &device, int baud_rate);
  ~SerialPort();
  SerialPort(const SerialPort &) = delete;
  SerialPort &operator=(const SerialPort &) = delete;
  SerialPort(SerialPort &&) = delete;
  SerialPort &operator=(SerialPort &&) = delete;
  void write(const Command &cmd);

private:
  int fd_{-1};
};