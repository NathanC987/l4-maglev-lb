function(l4mlb_unit_test name)
    add_executable(${name} ${name}.c ${ARGN})
    target_include_directories(${name} PRIVATE ${CMAKE_SOURCE_DIR}/src ${XXHASH_INCLUDE_DIRS})
    target_link_libraries(${name} PRIVATE l4mlb_warnings ${XXHASH_LIBRARIES} Threads::Threads)
    target_link_directories(${name} PRIVATE ${XXHASH_LIBRARY_DIRS})
    add_test(NAME ${name} COMMAND ${name})
endfunction()

l4mlb_unit_test(test_maglev_table
    ${CMAKE_SOURCE_DIR}/src/maglev/maglev_table.c
    ${CMAKE_SOURCE_DIR}/src/maglev/hash.c
)

l4mlb_unit_test(test_parse_ipv4
    ${CMAKE_SOURCE_DIR}/src/net/eth.c
    ${CMAKE_SOURCE_DIR}/src/net/ipv4.c
    ${CMAKE_SOURCE_DIR}/src/net/tcp_udp.c
    ${CMAKE_SOURCE_DIR}/src/net/flow.c
)

l4mlb_unit_test(test_conntrack
    ${CMAKE_SOURCE_DIR}/src/conntrack/conntrack.c
    ${CMAKE_SOURCE_DIR}/src/maglev/hash.c
)

l4mlb_unit_test(test_gre
    ${CMAKE_SOURCE_DIR}/src/net/gre.c
    ${CMAKE_SOURCE_DIR}/src/net/eth.c
    ${CMAKE_SOURCE_DIR}/src/net/ipv4.c
)

l4mlb_unit_test(test_stats_registry
    ${CMAKE_SOURCE_DIR}/src/stats/stats_registry.c
)
