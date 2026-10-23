// RGBA8 → PNG, for screenshots.
#pragma once

#include <cstdint>
#include <string>

namespace emu {

bool write_png(const std::string& path, const uint8_t* rgba, uint32_t width, uint32_t height);

}  // namespace emu
