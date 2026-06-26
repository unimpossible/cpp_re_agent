// Tier 0: free functions, arithmetic and loops. No structs/classes.
#include <iostream>

int gcd(int a, int b) {
    while (b != 0) {
        int t = a % b;
        a = b;
        b = t;
    }
    return a < 0 ? -a : a;
}

bool is_prime(int n) {
    if (n < 2) return false;
    for (int d = 2; (long)d * d <= n; ++d) {
        if (n % d == 0) return false;
    }
    return true;
}

int collatz_steps(int n) {
    int steps = 0;
    while (n != 1) {
        if (n % 2 == 0) n /= 2;
        else n = 3 * n + 1;
        ++steps;
    }
    return steps;
}

int main() {
    int n;
    if (!(std::cin >> n)) n = 27;
    std::cout << "gcd=" << gcd(n, 48) << "\n";
    std::cout << "prime=" << (is_prime(n) ? 1 : 0) << "\n";
    std::cout << "collatz=" << collatz_steps(n) << "\n";
    return 0;
}
