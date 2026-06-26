// Tier 1: a plain struct plus functions operating over a fixed array of them.
#include <iostream>

struct Sample {
    int id;
    double value;
};

double mean_value(const Sample* samples, int count) {
    double total = 0.0;
    for (int i = 0; i < count; ++i) {
        total += samples[i].value;
    }
    return count > 0 ? total / count : 0.0;
}

int argmax_value(const Sample* samples, int count) {
    int best = 0;
    for (int i = 1; i < count; ++i) {
        if (samples[i].value > samples[best].value) {
            best = i;
        }
    }
    return count > 0 ? samples[best].id : -1;
}

int main() {
    Sample samples[4] = {{10, 3.5}, {20, 9.1}, {30, 1.2}, {40, 7.8}};
    std::cout << "mean=" << mean_value(samples, 4) << "\n";
    std::cout << "argmax_id=" << argmax_value(samples, 4) << "\n";
    return 0;
}
