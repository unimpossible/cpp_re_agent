// Tier 2: small fixed struct + value-returning math methods.
#include <iostream>

struct Matrix2x2 {
    double a, b, c, d;

    double determinant() const {
        return a * d - b * c;
    }

    Matrix2x2 multiply(const Matrix2x2& other) const {
        Matrix2x2 result;
        result.a = a * other.a + b * other.c;
        result.b = a * other.b + b * other.d;
        result.c = c * other.a + d * other.c;
        result.d = c * other.b + d * other.d;
        return result;
    }
};

int main() {
    Matrix2x2 m{1, 2, 3, 4};
    Matrix2x2 identity{1, 0, 0, 1};
    Matrix2x2 product = m.multiply(identity);
    std::cout << "det=" << m.determinant() << "\n";
    std::cout << "product.a=" << product.a << "\n";
    return 0;
}
