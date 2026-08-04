// Tier 3: a point-of-sale model with several interacting structs and a
// multi-level call graph over plain data (no STL), so most functions survive
// -O2 as name-matchable decompiled targets. Deliberately bigger than tiers 0-2:
// six types and a dozen+ functions, where each function references only a SUBSET
// of the types. That subset gap is exactly what the context-retrieval experiment
// (TODO #5) needs: the static-deps oracle's project.h is much smaller than the
// full dump, even before synthetic distractors are added.
#include <cstdio>

struct Money {
    long cents;
};

struct Product {
    int id;
    long unit_price_cents;
    int stock;
    int category;
};

struct Customer {
    int id;
    int loyalty_points;
    int tier;
};

struct OrderLine {
    int product_id;
    int quantity;
};

struct Order {
    int customer_id;
    OrderLine lines[8];
    int line_count;
};

struct Catalog {
    Product products[16];
    int count;
};

Money money_add(Money a, Money b) {
    Money sum;
    sum.cents = a.cents + b.cents;
    return sum;
}

Money money_scale(Money m, int quantity) {
    Money out;
    out.cents = m.cents * quantity;
    return out;
}

int find_product(const Catalog* catalog, int product_id) {
    for (int i = 0; i < catalog->count; ++i) {
        if (catalog->products[i].id == product_id) {
            return i;
        }
    }
    return -1;
}

Money line_subtotal(const Catalog* catalog, OrderLine line) {
    Money zero;
    zero.cents = 0;
    int idx = find_product(catalog, line.product_id);
    if (idx < 0) {
        return zero;
    }
    Money unit;
    unit.cents = catalog->products[idx].unit_price_cents;
    return money_scale(unit, line.quantity);
}

Money order_subtotal(const Catalog* catalog, const Order* order) {
    Money total;
    total.cents = 0;
    for (int i = 0; i < order->line_count; ++i) {
        Money part = line_subtotal(catalog, order->lines[i]);
        total = money_add(total, part);
    }
    return total;
}

Money tax_for(Money subtotal, int rate_basis_points) {
    Money tax;
    tax.cents = (subtotal.cents * rate_basis_points) / 10000;
    return tax;
}

int customer_tier_for(int points) {
    if (points >= 1000) return 3;
    if (points >= 500) return 2;
    if (points >= 100) return 1;
    return 0;
}

Money loyalty_discount(Customer customer, Money subtotal) {
    int tier = customer_tier_for(customer.loyalty_points);
    Money discount;
    discount.cents = (subtotal.cents * tier * 5) / 100;
    return discount;
}

Money order_total(const Catalog* catalog, const Order* order, Customer customer) {
    Money subtotal = order_subtotal(catalog, order);
    Money tax = tax_for(subtotal, 825);
    Money discount = loyalty_discount(customer, subtotal);
    Money total = money_add(subtotal, tax);
    total.cents -= discount.cents;
    return total;
}

int restock(Catalog* catalog, int product_id, int quantity) {
    int idx = find_product(catalog, product_id);
    if (idx < 0) {
        return -1;
    }
    catalog->products[idx].stock += quantity;
    return catalog->products[idx].stock;
}

int low_stock_count(const Catalog* catalog, int threshold) {
    int low = 0;
    for (int i = 0; i < catalog->count; ++i) {
        if (catalog->products[i].stock < threshold) {
            ++low;
        }
    }
    return low;
}

void apply_purchase(Customer* customer, Money amount) {
    customer->loyalty_points += (int)(amount.cents / 100);
    customer->tier = customer_tier_for(customer->loyalty_points);
}

int main() {
    Catalog catalog;
    catalog.count = 3;
    catalog.products[0] = {101, 250, 5, 1};
    catalog.products[1] = {102, 999, 0, 2};
    catalog.products[2] = {103, 125, 12, 1};

    Order order;
    order.customer_id = 7;
    order.line_count = 2;
    order.lines[0] = {101, 3};
    order.lines[1] = {103, 4};

    Customer customer = {7, 540, 0};

    Money total = order_total(&catalog, &order, customer);
    apply_purchase(&customer, total);
    int low = low_stock_count(&catalog, 3);
    int stock = restock(&catalog, 102, 10);

    printf("total=%ld points=%d tier=%d low=%d stock=%d\n",
           total.cents, customer.loyalty_points, customer.tier, low, stock);
    return 0;
}
