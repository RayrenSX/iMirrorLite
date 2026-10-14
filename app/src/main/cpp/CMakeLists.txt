cmake_minimum_required(VERSION 3.22)
project(imirror_native LANGUAGES C)
add_library(imirrorio SHARED usb_io.c)
target_compile_options(imirrorio PRIVATE -Wall -Wextra -Werror)
target_link_options(imirrorio PRIVATE "-Wl,-z,max-page-size=16384")

set(PLAYFAIR "${CMAKE_CURRENT_SOURCE_DIR}/../../../../third_party/uxplay/playfair")
add_library(imirrorairplay SHARED airplay_crypto.c
    ${PLAYFAIR}/playfair.c ${PLAYFAIR}/omg_hax.c ${PLAYFAIR}/modified_md5.c
    ${PLAYFAIR}/sap_hash.c ${PLAYFAIR}/hand_garble.c)
target_include_directories(imirrorairplay PRIVATE ${PLAYFAIR})
# Upstream performs unaligned word loads; use memcpy-compatible aliasing.
target_compile_options(imirrorairplay PRIVATE -fno-strict-aliasing)
target_link_options(imirrorairplay PRIVATE "-Wl,-z,max-page-size=16384")
