# Custom build types. Select with -DCMAKE_BUILD_TYPE=Asan (or Tsan).
# CMake picks up CMAKE_C_FLAGS_<CONFIG> for any build type name, known or not.

set(CMAKE_C_FLAGS_ASAN "-O1 -g -fno-omit-frame-pointer -fsanitize=address,undefined"
    CACHE STRING "Flags used by the C compiler for Asan builds." FORCE)
set(CMAKE_EXE_LINKER_FLAGS_ASAN "-fsanitize=address,undefined"
    CACHE STRING "Flags used by the linker for Asan builds." FORCE)

set(CMAKE_C_FLAGS_TSAN "-O1 -g -fno-omit-frame-pointer -fsanitize=thread"
    CACHE STRING "Flags used by the C compiler for Tsan builds." FORCE)
set(CMAKE_EXE_LINKER_FLAGS_TSAN "-fsanitize=thread"
    CACHE STRING "Flags used by the linker for Tsan builds." FORCE)

mark_as_advanced(
    CMAKE_C_FLAGS_ASAN CMAKE_EXE_LINKER_FLAGS_ASAN
    CMAKE_C_FLAGS_TSAN CMAKE_EXE_LINKER_FLAGS_TSAN
)
