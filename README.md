# AI Reverse Engineering Agent

WORK IN PROGRESS - FOR TESTING PURPOSES ONLY
This tool utilizes Ghidra and Generative AI to assist in reverse engineering C++ binaries. It provides a web interface to decompile binaries, improve the code readability using LLMs, and synthesize header files from the results.

## Features

*   **Decompilation**: Automates `ghidrecomp` to extract decompiled C functions from a binary.
*   **AI Improvement**: Uses LLMs (Gemini or Local OpenAI-compatible) to rename variables, fix types, and add comments to raw decompilation.
*   **Batch Processing**: Improvements can be run on individual functions or in batch for the entire binary.
*   **Header Synthesis**: Scans improved code for struct/class definitions (using Tree-sitter) and synthesizes a unified `project.h` file.
*   **Persistence**: Caches decompilation results and saves AI improvements to disk.

## Prerequisites

1.  **Ghidra**: Ensure Ghidra is installed and the `GHIDRA_INSTALL_DIR` environment variable is set.
2.  **Python 3.10+**: Install dependencies using `pip`.
3.  **LLM Access**: 
    *   **Gemini**: Requires a Google API Key.
    *   **Local**: Requires a local server (like LM Studio or Ollama) running an OpenAI-compatible endpoint.

## Installation

1.  Clone the repository.
2.  Install the package (editable, with dev/test extras):
    ```bash
    pip install -e ".[dev]"
    ```
3.  Set up environment variables in a `.env` file (optional, can also be set in UI):
    ```
    GHIDRA_INSTALL_DIR=C:\Path\To\Ghidra
    GEMINI_API_KEY=your_key_here
    LOCAL_LLM_URL=http://localhost:1234/v1
    ```

## Usage

Run the Streamlit application:

```bash
streamlit run streamlit_app.py
```

### Workflow

1.  **Load Binary**: Enter the path to your target binary in the sidebar and click "Load / Decompile".
2.  **Improve Code**: Select a function to view its raw decompilation. Click "Improve with AI" to generate a readable C++ version. Use "Batch Improve All" to process all functions.
3.  **Synthesize Headers**: Navigate to the "Header Synthesis" page (sidebar). Scan the improved files to extract types and generate a consolidated `project.h` file.

## Project Structure

*   `streamlit_app.py`: Main Streamlit application entry point.
*   `pages/`: Streamlit multipage UI (Decompiler, Header Synthesis, Contextual Improver).
*   `src/cpp_re_agent/`: The importable `cpp_re_agent` package.
    *   `decompiler.py`: Wrapper for the `ghidrecomp` tool.
    *   `ai_improver.py`: Handles interactions with AI providers.
    *   `scanner.py`: Uses Tree-sitter to parse C++ struct/class definitions.
    *   `knowledge_graph.py`: Manages vector storage (ChromaDB) and type merging logic.
*   `tests/`: Pytest test suite.
*   `examples/`: Sample binaries/source for experimentation.
*   `workspace/`: Directory where outputs (raw decompilation, improved code, headers) are stored.
