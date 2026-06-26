// Tier 2: a class with methods and internal state.
#include <iostream>

class Account {
public:
    Account(int owner_id, double opening_balance)
        : owner_id_(owner_id), balance_(opening_balance) {}

    bool withdraw(double amount) {
        if (amount <= 0 || amount > balance_) {
            return false;
        }
        balance_ -= amount;
        return true;
    }

    void deposit(double amount) {
        if (amount > 0) {
            balance_ += amount;
        }
    }

    double balance() const { return balance_; }

private:
    int owner_id_;
    double balance_;
};

int main() {
    Account account(7, 100.0);
    account.deposit(50.0);
    bool ok = account.withdraw(120.0);
    std::cout << "balance=" << account.balance() << " ok=" << ok << "\n";
    return 0;
}
