// Tier 1: C-string processing with loops and branches (no heap).
#include <iostream>

int count_vowels(const char* text) {
    int vowels = 0;
    for (int i = 0; text[i] != '\0'; ++i) {
        char c = text[i];
        if (c == 'a' || c == 'e' || c == 'i' || c == 'o' || c == 'u') {
            ++vowels;
        }
    }
    return vowels;
}

int word_count(const char* text) {
    int words = 0;
    bool in_word = false;
    for (int i = 0; text[i] != '\0'; ++i) {
        if (text[i] == ' ') {
            in_word = false;
        } else if (!in_word) {
            in_word = true;
            ++words;
        }
    }
    return words;
}

int main() {
    const char* sentence = "the quick brown fox";
    std::cout << "vowels=" << count_vowels(sentence) << "\n";
    std::cout << "words=" << word_count(sentence) << "\n";
    return 0;
}
