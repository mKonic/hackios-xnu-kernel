#include "png.h"

#include <cstdio>
#include <vector>

#include <zlib.h>

namespace emu {

namespace {

void be32(std::vector<uint8_t>& v, uint32_t x) {
	for (int s = 24; s >= 0; s -= 8) v.push_back(uint8_t(x >> s));
}

void chunk(FILE* f, const char* type, const std::vector<uint8_t>& data) {
	std::vector<uint8_t> out;
	be32(out, uint32_t(data.size()));
	out.insert(out.end(), type, type + 4);
	out.insert(out.end(), data.begin(), data.end());
	be32(out, uint32_t(crc32(0, out.data() + 4, uInt(out.size() - 4))));
	std::fwrite(out.data(), 1, out.size(), f);
}

}  // namespace

bool write_png(const std::string& path, const uint8_t* rgba, uint32_t w, uint32_t h) {
	std::vector<uint8_t> raw;
	raw.reserve(size_t(w * 4 + 1) * h);
	for (uint32_t y = 0; y < h; ++y) {
		raw.push_back(0);  // filter: none
		raw.insert(raw.end(), rgba + size_t(y) * w * 4, rgba + size_t(y + 1) * w * 4);
	}
	uLongf zlen = compressBound(uLong(raw.size()));
	std::vector<uint8_t> z(zlen);
	if (compress2(z.data(), &zlen, raw.data(), uLong(raw.size()), 6) != Z_OK) return false;
	z.resize(zlen);

	FILE* f = std::fopen(path.c_str(), "wb");
	if (!f) return false;
	static const uint8_t sig[8] = {0x89, 'P', 'N', 'G', '\r', '\n', 0x1a, '\n'};
	std::fwrite(sig, 1, 8, f);
	std::vector<uint8_t> ihdr;
	be32(ihdr, w);
	be32(ihdr, h);
	ihdr.insert(ihdr.end(), {8, 6, 0, 0, 0});  // 8-bit RGBA, no interlace
	chunk(f, "IHDR", ihdr);
	chunk(f, "IDAT", z);
	chunk(f, "IEND", {});
	return std::fclose(f) == 0;
}

}  // namespace emu
