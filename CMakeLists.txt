cmake_minimum_required(VERSION 3.22)
project(iot_mpquic LANGUAGES C CXX)

if(NOT CMAKE_SYSTEM_NAME STREQUAL "Linux")
    message(FATAL_ERROR "The MPQUIC experiments require Linux. See docs/experiment-environment.md.")
endif()

if(NOT EXISTS "${XQUIC_SOURCE_DIR}/CMakeLists.txt" OR NOT SSL_INC_PATH OR NOT SSL_LIB_PATH)
    message(FATAL_ERROR "Run bash scripts/build.sh to prepare the pinned sources and TLS dependency.")
endif()

find_package(Threads REQUIRED)
add_subdirectory("${XQUIC_SOURCE_DIR}" xquic EXCLUDE_FROM_ALL)

add_executable(iot-mpquic
    src/transport/endpoint.c
    src/transport/iot_wire.c
    src/gateway/iot_queue.c
    src/receiver/iot_receive.c
    src/scheduler/scheduler.c
    src/scheduler/proposed.c
    src/transport/path_affinity.c
)
target_compile_features(iot-mpquic PRIVATE c_std_11)
target_compile_options(iot-mpquic PRIVATE -Wall -Wextra -Werror)
target_include_directories(iot-mpquic PRIVATE src)
target_include_directories(iot-mpquic SYSTEM PRIVATE "${XQUIC_SOURCE_DIR}/include" "${XQUIC_SOURCE_DIR}" "${SSL_INC_PATH}")
target_link_libraries(iot-mpquic PRIVATE xquic-static ${SSL_LIB_PATH} Threads::Threads m dl)
set_property(TARGET iot-mpquic PROPERTY LINKER_LANGUAGE CXX)

enable_testing()
add_executable(iot-scheduler-test EXCLUDE_FROM_ALL
    tests/scheduler_test.c src/scheduler/scheduler.c src/scheduler/proposed.c src/gateway/iot_queue.c src/transport/iot_wire.c
)
target_compile_features(iot-scheduler-test PRIVATE c_std_11)
target_compile_options(iot-scheduler-test PRIVATE -Wall -Wextra -Werror)
target_include_directories(iot-scheduler-test PRIVATE src)
target_link_libraries(iot-scheduler-test PRIVATE m)
file(STRINGS configs/stage3-policies.txt IOT_BASELINES)
foreach(policy IN ITEMS ${IOT_BASELINES} proposed-strict proposed-cross)
    add_test(NAME "scheduler-${policy}" COMMAND iot-scheduler-test "${policy}")
endforeach()
