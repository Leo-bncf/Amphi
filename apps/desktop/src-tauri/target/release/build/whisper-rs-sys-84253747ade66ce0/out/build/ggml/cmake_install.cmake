# Install script for directory: /Users/leo/amphi/apps/desktop/src-tauri/target/release/build/whisper-rs-sys-84253747ade66ce0/out/whisper.cpp/ggml

# Set the install prefix
if(NOT DEFINED CMAKE_INSTALL_PREFIX)
  set(CMAKE_INSTALL_PREFIX "/Users/leo/amphi/apps/desktop/src-tauri/target/release/build/whisper-rs-sys-84253747ade66ce0/out")
endif()
string(REGEX REPLACE "/$" "" CMAKE_INSTALL_PREFIX "${CMAKE_INSTALL_PREFIX}")

# Set the install configuration name.
if(NOT DEFINED CMAKE_INSTALL_CONFIG_NAME)
  if(BUILD_TYPE)
    string(REGEX REPLACE "^[^A-Za-z0-9_]+" ""
           CMAKE_INSTALL_CONFIG_NAME "${BUILD_TYPE}")
  else()
    set(CMAKE_INSTALL_CONFIG_NAME "Release")
  endif()
  message(STATUS "Install configuration: \"${CMAKE_INSTALL_CONFIG_NAME}\"")
endif()

# Set the component getting installed.
if(NOT CMAKE_INSTALL_COMPONENT)
  if(COMPONENT)
    message(STATUS "Install component: \"${COMPONENT}\"")
    set(CMAKE_INSTALL_COMPONENT "${COMPONENT}")
  else()
    set(CMAKE_INSTALL_COMPONENT)
  endif()
endif()

# Is this installation the result of a crosscompile?
if(NOT DEFINED CMAKE_CROSSCOMPILING)
  set(CMAKE_CROSSCOMPILING "FALSE")
endif()

# Set path to fallback-tool for dependency-resolution.
if(NOT DEFINED CMAKE_OBJDUMP)
  set(CMAKE_OBJDUMP "/usr/bin/objdump")
endif()

if(NOT CMAKE_INSTALL_LOCAL_ONLY)
  # Include the install script for the subdirectory.
  include("/Users/leo/amphi/apps/desktop/src-tauri/target/release/build/whisper-rs-sys-84253747ade66ce0/out/build/ggml/src/cmake_install.cmake")
endif()

if(CMAKE_INSTALL_COMPONENT STREQUAL "Unspecified" OR NOT CMAKE_INSTALL_COMPONENT)
  file(INSTALL DESTINATION "${CMAKE_INSTALL_PREFIX}/lib" TYPE STATIC_LIBRARY FILES "/Users/leo/amphi/apps/desktop/src-tauri/target/release/build/whisper-rs-sys-84253747ade66ce0/out/build/ggml/src/libggml.a")
  if(EXISTS "$ENV{DESTDIR}${CMAKE_INSTALL_PREFIX}/lib/libggml.a" AND
     NOT IS_SYMLINK "$ENV{DESTDIR}${CMAKE_INSTALL_PREFIX}/lib/libggml.a")
    execute_process(COMMAND "/usr/bin/ranlib" "$ENV{DESTDIR}${CMAKE_INSTALL_PREFIX}/lib/libggml.a")
  endif()
endif()

if(CMAKE_INSTALL_COMPONENT STREQUAL "Unspecified" OR NOT CMAKE_INSTALL_COMPONENT)
  file(INSTALL DESTINATION "${CMAKE_INSTALL_PREFIX}/include" TYPE FILE FILES
    "/Users/leo/amphi/apps/desktop/src-tauri/target/release/build/whisper-rs-sys-84253747ade66ce0/out/whisper.cpp/ggml/include/ggml.h"
    "/Users/leo/amphi/apps/desktop/src-tauri/target/release/build/whisper-rs-sys-84253747ade66ce0/out/whisper.cpp/ggml/include/ggml-cpu.h"
    "/Users/leo/amphi/apps/desktop/src-tauri/target/release/build/whisper-rs-sys-84253747ade66ce0/out/whisper.cpp/ggml/include/ggml-alloc.h"
    "/Users/leo/amphi/apps/desktop/src-tauri/target/release/build/whisper-rs-sys-84253747ade66ce0/out/whisper.cpp/ggml/include/ggml-backend.h"
    "/Users/leo/amphi/apps/desktop/src-tauri/target/release/build/whisper-rs-sys-84253747ade66ce0/out/whisper.cpp/ggml/include/ggml-blas.h"
    "/Users/leo/amphi/apps/desktop/src-tauri/target/release/build/whisper-rs-sys-84253747ade66ce0/out/whisper.cpp/ggml/include/ggml-cann.h"
    "/Users/leo/amphi/apps/desktop/src-tauri/target/release/build/whisper-rs-sys-84253747ade66ce0/out/whisper.cpp/ggml/include/ggml-cuda.h"
    "/Users/leo/amphi/apps/desktop/src-tauri/target/release/build/whisper-rs-sys-84253747ade66ce0/out/whisper.cpp/ggml/include/ggml-kompute.h"
    "/Users/leo/amphi/apps/desktop/src-tauri/target/release/build/whisper-rs-sys-84253747ade66ce0/out/whisper.cpp/ggml/include/ggml-opt.h"
    "/Users/leo/amphi/apps/desktop/src-tauri/target/release/build/whisper-rs-sys-84253747ade66ce0/out/whisper.cpp/ggml/include/ggml-metal.h"
    "/Users/leo/amphi/apps/desktop/src-tauri/target/release/build/whisper-rs-sys-84253747ade66ce0/out/whisper.cpp/ggml/include/ggml-rpc.h"
    "/Users/leo/amphi/apps/desktop/src-tauri/target/release/build/whisper-rs-sys-84253747ade66ce0/out/whisper.cpp/ggml/include/ggml-sycl.h"
    "/Users/leo/amphi/apps/desktop/src-tauri/target/release/build/whisper-rs-sys-84253747ade66ce0/out/whisper.cpp/ggml/include/ggml-vulkan.h"
    )
endif()

if(CMAKE_INSTALL_COMPONENT STREQUAL "Unspecified" OR NOT CMAKE_INSTALL_COMPONENT)
  file(INSTALL DESTINATION "${CMAKE_INSTALL_PREFIX}/lib" TYPE STATIC_LIBRARY FILES "/Users/leo/amphi/apps/desktop/src-tauri/target/release/build/whisper-rs-sys-84253747ade66ce0/out/build/ggml/src/libggml-base.a")
  if(EXISTS "$ENV{DESTDIR}${CMAKE_INSTALL_PREFIX}/lib/libggml-base.a" AND
     NOT IS_SYMLINK "$ENV{DESTDIR}${CMAKE_INSTALL_PREFIX}/lib/libggml-base.a")
    execute_process(COMMAND "/usr/bin/ranlib" "$ENV{DESTDIR}${CMAKE_INSTALL_PREFIX}/lib/libggml-base.a")
  endif()
endif()

string(REPLACE ";" "\n" CMAKE_INSTALL_MANIFEST_CONTENT
       "${CMAKE_INSTALL_MANIFEST_FILES}")
if(CMAKE_INSTALL_LOCAL_ONLY)
  file(WRITE "/Users/leo/amphi/apps/desktop/src-tauri/target/release/build/whisper-rs-sys-84253747ade66ce0/out/build/ggml/install_local_manifest.txt"
     "${CMAKE_INSTALL_MANIFEST_CONTENT}")
endif()
