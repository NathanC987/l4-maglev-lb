add_library(l4mlb_warnings INTERFACE)
target_compile_options(l4mlb_warnings INTERFACE
    -Wall
    -Wextra
    -Wconversion
    -Wsign-conversion
    -Wshadow
    -Wpointer-arith
    $<$<NOT:$<CONFIG:Release>>:-Werror>
)
