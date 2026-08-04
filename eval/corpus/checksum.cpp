#include <cstdint>
#include <cstdio>
#include <cstring>

uint32_t crc32_simple(const uint8_t* data, size_t length) {
    uint32_t crc = 0xFFFFFFFF;
    for (size_t i = 0; i < length; ++i) {
        crc ^= data[i];
        for (int bit = 0; bit < 8; ++bit) {
            if (crc & 1) {
                crc = (crc >> 1) ^ 0xEDB88320;
            } else {
                crc >>= 1;
            }
        }
    }
    return ~crc;
}

int main(int argc, char** argv) {
    const char* message = (argc > 1) ? argv[1] : "hello world";
    uint32_t checksum = crc32_simple(
        reinterpret_cast<const uint8_t*>(message), strlen(message));
    printf("crc32(\"%s\") = 0x%08x\n", message, checksum);
    return 0;
}
