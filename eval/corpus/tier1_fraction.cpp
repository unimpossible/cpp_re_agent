// Tier 1: a small struct whose helpers share a gcd (exercises callee ordering).
#include <iostream>

struct Fraction {
    int numerator;
    int denominator;
};

int gcd(int a, int b) {
    while (b != 0) {
        int t = a % b;
        a = b;
        b = t;
    }
    return a < 0 ? -a : a;
}

Fraction reduce(Fraction f) {
    int g = gcd(f.numerator, f.denominator);
    if (g != 0) {
        f.numerator /= g;
        f.denominator /= g;
    }
    return f;
}

Fraction add(Fraction a, Fraction b) {
    Fraction sum;
    sum.numerator = a.numerator * b.denominator + b.numerator * a.denominator;
    sum.denominator = a.denominator * b.denominator;
    return reduce(sum);
}

bool less_than(Fraction a, Fraction b) {
    return a.numerator * b.denominator < b.numerator * a.denominator;
}

int main() {
    Fraction a = {1, 2};
    Fraction b = {1, 3};
    Fraction s = add(a, b);
    std::cout << s.numerator << "/" << s.denominator << "\n";
    std::cout << "less=" << less_than(b, a) << "\n";
    std::cout << "reduced=" << reduce(Fraction{4, 8}).numerator << "\n";
    return 0;
}
