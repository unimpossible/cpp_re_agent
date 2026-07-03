/* Mock AI-improved output for corpus/hello_world.cpp.
   Written by hand to look like a good LLM cleanup of the raw decompilation,
   so the eval harness has an offline "improved" candidate to score. */

#include <iostream>
#include <string>
#include <vector>

// Reconstructed from field accesses at offsets 0x0 (string) and 0x20 (int).
class User {
public:
    std::string name;
    int id;

    User(std::string name, int id) : name(name), id(id) {}

    // Prints a greeting including the user's name and numeric id.
    void introduce() {
        std::cout << "Hi, I'm " << name << " (ID: " << id << ")" << std::endl;
    }

    // Returns the id scaled by a fixed constant (0x2a == 42).
    int calculateMagicNumber() {
        return id * 42;
    }
};

int main() {
    std::cout << "Initializing system..." << std::endl;

    std::vector<User> users;
    users.push_back(User("Alice", 101));
    users.push_back(User("Bob", 102));

    for (auto& user : users) {
        user.introduce();
        std::cout << "Magic: " << user.calculateMagicNumber() << std::endl;
    }

    return 0;
}
