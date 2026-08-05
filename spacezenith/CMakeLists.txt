cmake_minimum_required(VERSION 3.16)

project(spacezenith_app_1 LANGUAGES CXX)

add_executable(app_1 app_1/src/app_1.cpp)
target_compile_features(app_1 PRIVATE cxx_std_17)

if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
  target_compile_options(app_1 PRIVATE -Wall -Wextra -Wpedantic -Werror)
endif()

install(TARGETS app_1 RUNTIME DESTINATION app_1/bin)
install(DIRECTORY common raw logs DESTINATION .)
install(DIRECTORY app_1/lib app_1/log app_1/result app_1/tmp DESTINATION app_1)
