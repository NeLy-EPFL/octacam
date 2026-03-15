#pragma once

#include <string>

class SerialPort {
public:
  SerialPort(const std::string &device, int baud_rate);
  ~SerialPort();
  SerialPort(const SerialPort &) = delete;
  SerialPort &operator=(const SerialPort &) = delete;
  SerialPort(SerialPort &&) = delete;
  SerialPort &operator=(SerialPort &&) = delete;
  void write(const std::string &data);

private:
  int fd_{-1};
};