#include "serial.hpp"
#include <cerrno>
#include <stdexcept>
#include <system_error>

#include <fcntl.h>
#include <termios.h>
#include <unistd.h>

SerialPort::~SerialPort() { close(); }

SerialPort::SerialPort(SerialPort &&other) noexcept : fd_(other.fd_) {
  other.fd_ = -1;
}
SerialPort &SerialPort::operator=(SerialPort &&other) noexcept {
  if (this != &other) {
    close();
    fd_ = other.fd_;
    other.fd_ = -1;
  }
  return *this;
}

unsigned long SerialPort::toSpeed(int baud) {
  switch (baud) {
  case 9600:
    return B9600;
  case 115200:
    return B115200;
  default:
    throw std::invalid_argument("Unsupported baud");
  }
}

void SerialPort::configure(int baud) {
  termios tty{};
  if (tcgetattr(fd_, &tty) != 0)
    throw std::system_error(errno, std::generic_category(), "tcgetattr");

  cfmakeraw(&tty);
  tty.c_cflag &= ~PARENB;
  tty.c_cflag &= ~CSTOPB;
  tty.c_cflag &= ~CSIZE;
  tty.c_cflag |= CS8 | CLOCAL | CREAD;
#ifdef CRTSCTS
  tty.c_cflag &= ~CRTSCTS;
#endif
  tty.c_iflag &= ~(IXON | IXOFF | IXANY);

  tty.c_cc[VMIN] = 0;
  tty.c_cc[VTIME] = 1; // 100ms

  speed_t spd = static_cast<speed_t>(toSpeed(baud));
  if (cfsetispeed(&tty, spd) != 0 || cfsetospeed(&tty, spd) != 0)
    throw std::system_error(errno, std::generic_category(), "cfset speed");
  if (tcsetattr(fd_, TCSANOW, &tty) != 0)
    throw std::system_error(errno, std::generic_category(), "tcsetattr");
}

void SerialPort::open(const std::string &device, int baud) {
  close();
  fd_ = ::open(device.c_str(), O_RDWR | O_NOCTTY | O_SYNC);
  if (fd_ < 0)
    throw std::system_error(errno, std::generic_category(), "open");

  try {
    configure(baud);
    tcflush(fd_, TCIOFLUSH);
    usleep(2000 * 1000); // Arduino reset delay
  } catch (...) {
    ::close(fd_);
    fd_ = -1;
    throw;
  }
}

void SerialPort::close() {
  if (fd_ >= 0) {
    ::close(fd_);
    fd_ = -1;
  }
}

std::size_t SerialPort::writeAll(const void *data, std::size_t len) {
  auto p = static_cast<const uint8_t *>(data);
  std::size_t total = 0;
  while (total < len) {
    ssize_t n = ::write(fd_, p + total, len - total);
    if (n > 0)
      total += static_cast<std::size_t>(n);
    else if (n < 0 && errno == EINTR)
      continue;
    else
      throw std::system_error(errno, std::generic_category(), "write");
  }
  if (tcdrain(fd_) != 0)
    throw std::system_error(errno, std::generic_category(), "tcdrain");
  return total;
}

std::size_t SerialPort::readExact(void *out, std::size_t len, int timeoutMs) {
  auto p = static_cast<uint8_t *>(out);
  std::size_t total = 0;
  int waited = 0;
  constexpr int stepMs = 10;

  while (total < len && waited < timeoutMs) {
    ssize_t n = ::read(fd_, p + total, len - total);
    if (n > 0)
      total += static_cast<std::size_t>(n);
    else if (n < 0 && errno == EINTR)
      continue;
    else if (n < 0)
      throw std::system_error(errno, std::generic_category(), "read");
    else {
      usleep(stepMs * 1000);
      waited += stepMs;
    }
  }
  return total;
}