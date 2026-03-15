#include "serial.hpp"

#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <stdexcept>
#include <termios.h>
#include <unistd.h>

#include <spdlog/sinks/stdout_color_sinks.h>
#include <spdlog/spdlog.h>

namespace {
speed_t to_termios_baud(int baud_rate) {
  switch (baud_rate) {
  case 9600:
    return B9600;
  case 19200:
    return B19200;
  case 38400:
    return B38400;
  case 57600:
    return B57600;
  case 115200:
    return B115200;
  default:
    return B9600;
  }
}
} // namespace

SerialPort::SerialPort(const std::string &device, int baud_rate) {
  fd_ = open(device.c_str(), O_RDWR | O_NOCTTY | O_SYNC);
  spdlog::info("Opened serial port: {}, fd: {}", device, fd_);
  if (fd_ < 0) {
    spdlog::error("Error opening serial port {}: {}", device,
                  std::strerror(errno));
    throw std::runtime_error("Failed to open serial port");
  }

  struct termios tty{};
  if (tcgetattr(fd_, &tty) != 0) {
    spdlog::error("tcgetattr failed for {}: {}", device, std::strerror(errno));
    throw std::runtime_error("Failed to get serial settings");
  }

  const speed_t termios_baud = to_termios_baud(baud_rate);
  cfsetospeed(&tty, termios_baud);
  cfsetispeed(&tty, termios_baud);

  tty.c_cflag = (tty.c_cflag & ~CSIZE) | CS8;
  tty.c_iflag &= ~IGNBRK;
  tty.c_lflag = 0;
  tty.c_oflag = 0;
  tty.c_cflag |= (CLOCAL | CREAD);
  tty.c_cflag &= ~(PARENB | PARODD | CSTOPB | CRTSCTS);

  if (tcsetattr(fd_, TCSANOW, &tty) != 0) {
    spdlog::error("tcsetattr failed for {}: {}", device, std::strerror(errno));
    throw std::runtime_error("Failed to set serial settings");
  }
}

SerialPort::~SerialPort() {
  if (fd_ >= 0) {
    close(fd_);
  }
}

void SerialPort::write(const std::string &data) {
  if (fd_ < 0) {
    spdlog::error("Serial write skipped: invalid file descriptor");
    return;
  }

  size_t total_written = 0;
  while (total_written < data.size()) {
    const ssize_t bytes_written =
        ::write(fd_, data.data() + total_written, data.size() - total_written);
    if (bytes_written < 0) {
      if (errno == EINTR) {
        continue;
      }
      spdlog::error("Serial write failed: errno={} ({}) after {}/{} bytes",
                    errno, std::strerror(errno), total_written, data.size());
      return;
    }
    total_written += static_cast<size_t>(bytes_written);
  }

  if (tcdrain(fd_) != 0) {
    spdlog::warn("tcdrain failed: errno={} ({})", errno, std::strerror(errno));
  }
}