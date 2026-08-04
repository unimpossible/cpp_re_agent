#include <cstdio>
#include <cstring>

struct Item {
    char name[32];
    int quantity;
    double unit_price;
};

double total_value(const Item* items, int count) {
    double total = 0.0;
    for (int i = 0; i < count; ++i) {
        total += items[i].quantity * items[i].unit_price;
    }
    return total;
}

int find_item(const Item* items, int count, const char* name) {
    for (int i = 0; i < count; ++i) {
        if (strcmp(items[i].name, name) == 0) {
            return i;
        }
    }
    return -1;
}

int main() {
    Item items[3];
    strcpy(items[0].name, "bolt");
    items[0].quantity = 100;
    items[0].unit_price = 0.05;
    strcpy(items[1].name, "nut");
    items[1].quantity = 80;
    items[1].unit_price = 0.03;
    strcpy(items[2].name, "washer");
    items[2].quantity = 200;
    items[2].unit_price = 0.01;

    printf("Total inventory value: %.2f\n", total_value(items, 3));
    printf("Index of 'nut': %d\n", find_item(items, 3, "nut"));
    return 0;
}
