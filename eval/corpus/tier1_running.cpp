// Tier 1: reductions over an int array (decompile to index/pointer loops).
#include <iostream>

long array_sum(const int* values, int count) {
    long total = 0;
    for (int i = 0; i < count; ++i) {
        total += values[i];
    }
    return total;
}

int max_subarray(const int* values, int count) {
    int best = values[0];
    int current = values[0];
    for (int i = 1; i < count; ++i) {
        int extend = current + values[i];
        current = values[i] > extend ? values[i] : extend;
        if (current > best) {
            best = current;
        }
    }
    return best;
}

int longest_run(const int* values, int count) {
    int best = count > 0 ? 1 : 0;
    int run = best;
    for (int i = 1; i < count; ++i) {
        if (values[i] > values[i - 1]) {
            ++run;
            if (run > best) {
                best = run;
            }
        } else {
            run = 1;
        }
    }
    return best;
}

int main() {
    int values[8] = {3, -2, 5, -1, 6, -4, 2, 7};
    std::cout << "sum=" << array_sum(values, 8) << "\n";
    std::cout << "maxsub=" << max_subarray(values, 8) << "\n";
    std::cout << "run=" << longest_run(values, 8) << "\n";
    return 0;
}
