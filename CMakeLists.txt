cmake_minimum_required(VERSION 3.28)
project(huitacam VERSION 0.1.0 LANGUAGES CXX)

include(FetchContent)
FetchContent_Declare(
    CLI11
    GIT_REPOSITORY https://github.com/CLIUtils/CLI11.git
    GIT_TAG v2.5.0
)
FetchContent_MakeAvailable(CLI11)

add_executable(huitacam)
target_link_libraries(huitacam
    PRIVATE
        CLI11::CLI11
)
target_sources(huitacam
    PRIVATE
        src/main.cpp
)

set(CPACK_PROJECT_NAME ${PROJECT_NAME})
set(CPACK_PROJECT_VERSION ${PROJECT_VERSION})
include(CPack)
