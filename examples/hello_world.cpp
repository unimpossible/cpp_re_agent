#include <iostream>
#include <string>
#include <vector>

class User {
private:
    std::string name;
    int id;

public:
    User(std::string n, int i) : name(n), id(i) {}

    void introduce() {
        std::cout << "Hi, I'm " << name << " (ID: " << id << ")" << std::endl;
    }

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
