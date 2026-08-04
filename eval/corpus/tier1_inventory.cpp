// Tier 1: struct with several fields + functions iterating an array of them.
#include <iostream>

struct Item {
    int id;
    int quantity;
    double price;
};

double total_value(const Item* items, int count) {
    double total = 0.0;
    for (int i = 0; i < count; ++i) {
        total += items[i].quantity * items[i].price;
    }
    return total;
}

int count_low_stock(const Item* items, int count, int threshold) {
    int low = 0;
    for (int i = 0; i < count; ++i) {
        if (items[i].quantity < threshold) {
            ++low;
        }
    }
    return low;
}

int find_by_id(const Item* items, int count, int target_id) {
    for (int i = 0; i < count; ++i) {
        if (items[i].id == target_id) {
            return i;
        }
    }
    return -1;
}

int main() {
    Item items[3] = {{101, 5, 2.50}, {102, 0, 9.99}, {103, 12, 1.25}};
    std::cout << "total=" << total_value(items, 3) << "\n";
    std::cout << "low=" << count_low_stock(items, 3, 3) << "\n";
    std::cout << "idx=" << find_by_id(items, 3, 103) << "\n";
    return 0;
}
