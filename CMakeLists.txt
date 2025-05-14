cmake_minimum_required(VERSION 3.19)
project(octacam VERSION 0.1.0 LANGUAGES CXX)

# Set CMP0144 policy to NEW to avoid warnings about <PACKAGENAME>_ROOT variables
cmake_policy(SET CMP0144 NEW)

set(CMAKE_BUILD_TYPE Release)
set(CMAKE_CXX_FLAGS_RELEASE "-O3 -flto=auto -march=native")

set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

include(FetchContent)
FetchContent_Declare(
    CLI11
    GIT_REPOSITORY https://github.com/CLIUtils/CLI11.git
    GIT_TAG v2.5.0
)
FetchContent_MakeAvailable(CLI11)

FetchContent_Declare(
    spdlog
    GIT_REPOSITORY https://github.com/gabime/spdlog.git
    GIT_TAG v1.15.3
)
FetchContent_MakeAvailable(spdlog)

FetchContent_Declare(
  yaml-cpp
  GIT_REPOSITORY https://github.com/jbeder/yaml-cpp.git
  GIT_TAG 0.8.0
)
FetchContent_MakeAvailable(yaml-cpp)

find_package(OpenCV REQUIRED)
find_package(pylon REQUIRED)
find_package(Qt6 REQUIRED COMPONENTS Core Gui Widgets)
set(CMAKE_AUTOMOC ON)
set(CMAKE_AUTOUIC ON)
set(CMAKE_AUTORCC ON)

add_executable(octacam)
target_sources(octacam
    PRIVATE
        src/main.cpp
        src/camera.cpp
        src/camera.hpp
        src/main_window.cpp
        src/main_window.hpp
        src/parser.cpp
        src/parser.hpp
        src/timer.cpp
        src/timer.hpp
        src/video_writer.cpp
        src/video_writer.hpp
)
target_link_libraries(octacam
    PRIVATE
        CLI11::CLI11
        pylon::pylon
        opencv_videoio
        Qt6::Core
        Qt6::Gui
        Qt6::Widgets
        spdlog::spdlog
        yaml-cpp::yaml-cpp
)

set(CPACK_PROJECT_NAME ${PROJECT_NAME})
set(CPACK_PROJECT_VERSION ${PROJECT_VERSION})
include(CPack)
