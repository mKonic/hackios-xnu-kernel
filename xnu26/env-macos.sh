# source this (from bash) before building the xnu-12377 kernel on macOS (xnu26/build-macos.sh)
XNU26_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export XNU26_ROOT
export XNU26_SRC=${XNU26_SRC:-$XNU26_ROOT/xnu-src/xnu-12377.121.6}
export XNU26_DEPS=${XNU26_DEPS:-$XNU26_ROOT/xnu-deps/26}
export XNU26_SDK=$XNU26_DEPS/sdk/iPhoneOS.sdk
