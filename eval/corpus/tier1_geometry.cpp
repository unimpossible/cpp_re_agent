// Tier 1: struct Point + geometric helpers over an array of points.
#include <iostream>
#include <cmath>

struct Point {
    double x;
    double y;
};

double distance(Point a, Point b) {
    double dx = a.x - b.x;
    double dy = a.y - b.y;
    return std::sqrt(dx * dx + dy * dy);
}

double polygon_perimeter(const Point* vertices, int count) {
    double perimeter = 0.0;
    for (int i = 0; i < count; ++i) {
        Point current = vertices[i];
        Point next = vertices[(i + 1) % count];
        perimeter += distance(current, next);
    }
    return perimeter;
}

int main() {
    Point square[4] = {{0, 0}, {0, 2}, {2, 2}, {2, 0}};
    std::cout << "perimeter=" << polygon_perimeter(square, 4) << "\n";
    return 0;
}
