cmake_minimum_required(VERSION 3.20)
project(l4-maglev-lb C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
add_compile_definitions(_GNU_SOURCE)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Debug)
endif()

list(APPEND CMAKE_MODULE_PATH "${CMAKE_SOURCE_DIR}/cmake")
include(warnings)
include(sanitizers)

find_package(PkgConfig REQUIRED)
pkg_check_modules(XXHASH REQUIRED libxxhash)
find_package(Threads REQUIRED)

set(MAGLEV_LB_SOURCES
    src/main.c
    src/config/config.c
    src/net/eth.c
    src/net/ipv4.c
    src/net/tcp_udp.c
    src/net/gre.c
    src/net/flow.c
    src/net/raw_socket.c
    src/net/arp_resolve.c
    src/maglev/hash.c
    src/maglev/maglev_table.c
    src/conntrack/conntrack.c
    src/conntrack/reaper.c
    src/backend/backend_manager.c
    src/backend/health_checker.c
    src/backend/table_generator.c
    src/stats/stats_registry.c
    src/forward/datapath.c
)

add_executable(maglev-lb ${MAGLEV_LB_SOURCES})
target_include_directories(maglev-lb PRIVATE src ${XXHASH_INCLUDE_DIRS})
target_link_libraries(maglev-lb PRIVATE l4mlb_warnings ${XXHASH_LIBRARIES} Threads::Threads)
target_link_directories(maglev-lb PRIVATE ${XXHASH_LIBRARY_DIRS})

enable_testing()
add_subdirectory(test/unit)
