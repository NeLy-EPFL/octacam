# HuitaCam

HuitaCam is the successor to SeptaCam, a tool for previewing, recording, and saving video streams from multiple Basler cameras. It is designed to be fast, easy to use, and maintainable.

## Features
- **Support >7 cameras**: Despite the name, HuitaCam can support more than 8 cameras.
- **Preview all cameras**: HuitaCam can preview all cameras while recording. 
- **Direct video saving**

## Installation

### Prerequisites

Ensure you have the following installed:
- C++ compiler with C++23 support
- CMake 3.28 or higher.
- Git.

### Build Instructions

1. Install OpenCV, Qt6, and Basler Pylon SDK.
   ```bash
    sudo apt-get install libopencv-dev qt6-base-dev
    ```
    For the Basler Pylon SDK, follow the instructions on the [Basler website](https://www.baslerweb.com/en/support/downloads/software-downloads/).

1. Clone the repository:
   ```bash
   git clone https://github.com/NeLy-EPFL/huitacam.git
   cd huitacam
   ```

2. Create a build directory and configure the project:
   ```bash
   mkdir build
   cd build
   cmake ..
   ```

3. Build the project:
   ```bash
   cmake --build .
   ```

## Usage

After building the project, you can run the executable with the following command:
```bash
./huitacam <config_dir>
```
where `<config_dir>` is the path to the configuration directory containing the `.pfs` Basler camera configuraion files.

For a complete list of options, run:
```bash
./huitacam --help
```

### Code Style

- Follow modern C++ best practices.
- Use `clang-format` for consistent code formatting.