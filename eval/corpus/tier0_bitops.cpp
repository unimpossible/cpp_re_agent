// Tier 0: bit-twiddling free functions (decompile to gnarly shifts/masks).
#include <iostream>

int popcount(unsigned int value) {
    int count = 0;
    while (value != 0) {
        count += value & 1u;
        value >>= 1;
    }
    return count;
}

unsigned int reverse_bits(unsigned int value) {
    unsigned int result = 0;
    for (int i = 0; i < 32; ++i) {
        result = (result << 1) | (value & 1u);
        value >>= 1;
    }
    return result;
}

bool is_power_of_two(unsigned int value) {
    return value != 0 && (value & (value - 1)) == 0;
}

int main() {
    unsigned int n;
    if (!(std::cin >> n)) n = 0xB7u;
    std::cout << "popcount=" << popcount(n) << "\n";
    std::cout << "reversed=" << reverse_bits(n) << "\n";
    std::cout << "pow2=" << is_power_of_two(n) << "\n";
    return 0;
}
