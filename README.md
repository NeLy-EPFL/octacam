# octacam
octacam is the successor to SeptaCam, a tool for previewing, recording, and saving video streams from multiple Basler cameras. It is designed to be fast, easy to use, and maintainable.

<p align="center">
  <img src="https://github.com/user-attachments/assets/a7b6ac6e-5ae3-45fa-ae5a-2e3f5281e5c3" width="400"/>
</p>

## Features
- Support >7 cameras (8 is not the limit despite the name)
- See live updates of all cameras while recording. 
- Save frames directly to videos

## Installation
### Prerequisites
Ensure you have the following installed:
- C++ compiler with C++20 support (e.g., g++ 11 or higher)
- CMake 3.19 or higher.
- Git.

### Build Instructions
1. Install OpenCV, Qt6, and Basler Pylon SDK.
   ```bash
    sudo apt-get install libopencv-dev qt6-base-dev libxkbcommon-dev
    ```
    For the Basler Pylon SDK, follow the instructions on the [Basler website](https://www.baslerweb.com/en/support/downloads/software-downloads/).
2. Clone the repository:
   ```bash
   git clone https://github.com/NeLy-EPFL/octacam.git
   cd octacam
   ```
3. Create a build directory, configure the project, and build it:
   ```bash
   mkdir -p build \
      && cd build \
      && cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_FLAGS_RELEASE="-O3 -flto -march=native" .. \
      && cmake --build .
   ```

## Usage
After building the project, you can run the executable with the following command:
```bash
./octacam <config_dir>
```
where `<config_dir>` is the path to the configuration directory containing the `.pfs` Basler camera configuraion files.
If you encounter errors related to too many opened file descriptors, increase the limit before running octacam:
```bash
ulimit -n 8192 && ./octacam <config_dir>
```

For a complete list of options, run:
```bash
./octacam --help
```
### Code Style
- Use `clang-format` for consistent code formatting.
