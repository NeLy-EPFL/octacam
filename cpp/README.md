# octacam (C++ implementation)

This is the original C++/Qt6 implementation of octacam. It is kept as the reference implementation while the Python package (repo root) is developed. See the root README for the project overview.

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
2. From the repository root, configure and build:
   ```bash
   cmake -S cpp -B cpp/build -DCMAKE_BUILD_TYPE=Release
   cmake --build cpp/build -j
   ```

## Usage
After building the project, you can run the executable with the following command:
```bash
./cpp/build/octacam <config_dir>
```
where `<config_dir>` is the path to the configuration directory containing the `.pfs` Basler camera configuration files (see `configs/` at the repo root).

To run without hardware using Basler's camera emulation:
```bash
PYLON_CAMEMU=8 ./cpp/build/octacam configs/emulate_8_cameras
```

If you encounter errors related to too many opened file descriptors, increase the limit before running octacam:
```bash
ulimit -n 8192 && ./cpp/build/octacam <config_dir>
```

For a complete list of options, run:
```bash
./cpp/build/octacam --help
```

### Code Style
- Use `clang-format` for consistent code formatting.
