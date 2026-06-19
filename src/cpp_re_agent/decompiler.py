import os
import subprocess
import json
import hashlib
from pathlib import Path

# Fallback demo data: a small but non-trivial C++ program split into
# per-function snippets, mirroring the shape of what get_functions returns from
# a real decompilation (one entry per function/method). Used when no
# decompilation output exists yet so the UI and tests have something realistic
# to exercise. The matching compilable source lives in examples/sample_app.cpp.
MOCK_FUNCTIONS = {
    "User::User": (
        "User::User(int id, const std::string &name)\n"
        "    : id(id), name(name), score_total(0)\n"
        "{\n"
        "}\n"
    ),
    "User::add_score": (
        "void User::add_score(int value)\n"
        "{\n"
        "    this->scores.push_back(value);\n"
        "    this->score_total += value;\n"
        "}\n"
    ),
    "User::introduce": (
        "void User::introduce() const\n"
        "{\n"
        "    std::cout << \"User #\" << this->id << \" (\" << this->name << \")\\n\";\n"
        "}\n"
    ),
    "compute_average": (
        "double compute_average(const std::vector<int> &values)\n"
        "{\n"
        "    if (values.empty()) {\n"
        "        return 0.0;\n"
        "    }\n"
        "    long total = 0;\n"
        "    for (int v : values) {\n"
        "        total += v;\n"
        "    }\n"
        "    return (double)total / (double)values.size();\n"
        "}\n"
    ),
    "main": (
        "int main()\n"
        "{\n"
        "    std::vector<User> users;\n"
        "    users.emplace_back(1, \"Ada\");\n"
        "    users.emplace_back(2, \"Linus\");\n"
        "\n"
        "    users[0].add_score(90);\n"
        "    users[0].add_score(85);\n"
        "    users[1].add_score(70);\n"
        "\n"
        "    for (const User &u : users) {\n"
        "        u.introduce();\n"
        "        std::cout << \"  average: \" << compute_average(u.scores) << \"\\n\";\n"
        "    }\n"
        "    return 0;\n"
        "}\n"
    ),
}

def get_binary_md5(filepath: str) -> str:
    with open(filepath, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()

def decompile_binary(binary_path: str, output_dir: str):
    """
    Runs ghidrecomp on the binary.
    """
    # Ensure output directory exists
    os.makedirs(output_dir, exist_ok=True)
    
    # Check if ghidrecomp is installed/runnable
    # For now, we utilize the CLI tool `ghidrecomp`
    # ghidrecomp --binary <path> --output <dir>
    
    # we return early if the binary doesn't exist to allow UI testing.
    if not os.path.exists(binary_path):
        return
        
    # Check if we already have the output for this specific binary hash
    # Ghidrecomp structure: output_dir/bins/<name>-<md5>
    try:
        binary_name = Path(binary_path).name
        binary_hash = get_binary_md5(binary_path)
        expected_dir = Path(output_dir) / "bins" / f"{binary_name}-{binary_hash}"
        
        if expected_dir.exists():
            print(f"Skipping decompilation: Output already exists at {expected_dir}")
            return

        expected_dir = Path(output_dir) / "results" / "bins" / f"{binary_name}-{binary_hash[:6]}"
        
        if expected_dir.exists():
            print(f"Skipping decompilation: Output already exists at {expected_dir}")
            return
    except Exception as e:
        raise Exception(f"Warning: Could not check for existing decompilation: {e}")

    try:
        cmd = ["ghidrecomp", "-o", output_dir, binary_path]
        subprocess.run(cmd, check=True, capture_output=True)
    except Exception as e:
        raise Exception(f"Decompilation failed (or tool missing): {e}")

def get_functions(output_dir: str):
    """
    Lists functions available in the output directory.
    Returns a dict of {func_name: code_content}
    """
    functions = {}

    # If directory doesn't exist or is empty, fall back to the bundled demo
    # program so the UI and tests have something to work with.
    if not os.path.exists(output_dir) or not os.listdir(output_dir):
        return dict(MOCK_FUNCTIONS)

    for file_path in Path(output_dir).rglob("*.c"):
        with open(file_path, "r") as f:
            content = f.read()
            name = file_path.stem
            functions[name] = content
            
    return functions
