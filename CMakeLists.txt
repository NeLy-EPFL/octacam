cmake_minimum_required(VERSION 3.28)
project(huitacam VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

include(FetchContent)
FetchContent_Declare(
    CLI11
    GIT_REPOSITORY https://github.com/CLIUtils/CLI11.git
    GIT_TAG v2.5.0
)
FetchContent_MakeAvailable(CLI11)

find_package(OpenCV REQUIRED)
find_package(pylon REQUIRED)

add_executable(huitacam)
target_sources(huitacam
    PRIVATE
        src/main.cpp
        src/camera.cpp
        src/camera.h
)
target_link_libraries(huitacam
    PRIVATE
        CLI11::CLI11
        pylon::pylon
        opencv_videoio
)

set(CPACK_PROJECT_NAME ${PROJECT_NAME})
set(CPACK_PROJECT_VERSION ${PROJECT_VERSION})
include(CPack)
