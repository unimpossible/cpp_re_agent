import os
import subprocess
import json
import hashlib
from pathlib import Path

def get_binary_md5(filepath: str) -> str:
    with open(filepath, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()

# Mock data for testing when binary/ghidra is unavailable
MOCK_FUNCTIONS = {
    "main": """
    int main() {
        // This is a raw decompilation
        int iVar1;
        void *pvVar2;
        
        std::cout << "Starting..." << std::endl;
        // ... more messy code ...
        return 0;
    }
    """,
    "User::introduce": """
    void User::introduce(User *this) {
        std::basic_ostream *this_00;
        
        this_00 = std::operator<<((basic_ostream *)std::cout,"Hi, I'm ");
        // ...
    }
    """
}

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
    except Exception as e:
        print(f"Warning: Could not check for existing decompilation: {e}")

    try:
        cmd = ["ghidrecomp", "-o", output_dir, binary_path]
        subprocess.run(cmd, check=True, capture_output=True)
    except Exception as e:
        print(f"Decompilation failed (or tool missing): {e}")

def get_functions(output_dir: str):
    """
    Lists functions available in the output directory.
    Returns a dict of {func_name: code_content}
    """
    functions = {}
    
    # If directory doesn't exist or is empty, return mock data for testing
    if not os.path.exists(output_dir) or not os.listdir(output_dir):
        return MOCK_FUNCTIONS
        
    for file_path in Path(output_dir).rglob("*.c"):
        with open(file_path, "r") as f:
            content = f.read()
            name = file_path.stem
            functions[name] = content
            
    return functions
