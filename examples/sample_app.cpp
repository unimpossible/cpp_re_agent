// A small but non-trivial C++ program used as a reverse-engineering sample.
// Build it, decompile it, and run the improvement pipeline over the result:
//
//   g++ -O2 -o sample_app examples/sample_app.cpp        // optimized: gnarlier decompilation
//   cl /O2 /EHsc examples\sample_app.cpp                 // MSVC equivalent
//
// The decompiler.MOCK_FUNCTIONS fallback mirrors these functions so the UI has
// realistic data even before a real binary is decompiled.

#include <iostream>
#include <string>
#include <vector>

class User {
public:
    int id;
    std::string name;
    std::vector<int> scores;
    long score_total;

    User(int id, const std::string &name)
        : id(id), name(name), score_total(0) {}

    void add_score(int value) {
        scores.push_back(value);
        score_total += value;
    }

    void introduce() const {
        std::cout << "User #" << id << " (" << name << ")\n";
    }
};

double compute_average(const std::vector<int> &values) {
    if (values.empty()) {
        return 0.0;
    }
    long total = 0;
    for (int v : values) {
        total += v;
    }
    return static_cast<double>(total) / static_cast<double>(values.size());
}

int main() {
    std::vector<User> users;
    users.emplace_back(1, "Ada");
    users.emplace_back(2, "Linus");

    users[0].add_score(90);
    users[0].add_score(85);
    users[1].add_score(70);

    for (const User &u : users) {
        u.introduce();
        std::cout << "  average: " << compute_average(u.scores) << "\n";
    }
    return 0;
}
