import streamlit as st
import os
import sys
from pathlib import Path

# Add parent dir to sys.path to import modules
sys.path.append(os.path.join(os.getcwd()))

import decompiler
import contextual_improver
import knowledge_graph
from scanner import scan_code

st.set_page_config(layout="wide", page_title="Contextual Improver")

st.title("Component 3: Contextual Improver")

# Load Config
CONFIG_FILE = os.path.join(os.getcwd(), "config.json")
import json
def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {"provider": "local", "binary_path": "hello_world"}

config = load_config()
provider = config.get("provider", "local")
binary_path = config.get("binary_path", "hello_world")
binary_name = Path(binary_path).name
workspace_dir = os.path.join(os.getcwd(), "workspace")
project_h_path = Path(workspace_dir) / binary_name / "project.h"

# Sidebar
with st.sidebar:
    st.header("Target Selection")
    st.info(f"Target: {binary_name}")
    
    improved_dir = Path(workspace_dir) / binary_name / "improved"
    
    if not improved_dir.exists():
        st.error("No improved functions found. Run Step 0 first.")
        files = []
    else:
        files = list(improved_dir.glob("*.cpp"))
    
    # We want to select based on function name, which matches filename stem
    func_names = [f.stem for f in files]
    
    selected_func_name = st.selectbox("Select Function", func_names)


# Main Area
col1, col2 = st.columns(2)

if selected_func_name:
    file_path = improved_dir / f"{selected_func_name}.cpp"
    
    with open(file_path, "r", encoding="utf-8") as f:
        current_code = f.read()

    with col1:
        st.subheader("Current Improved State")
        st.code(current_code, language="cpp")

        # Dependency Analysis
        st.divider()
        st.write("### Analysis")
        
        # We scan on the fly to show the user what we see
        scanned = scan_code(current_code)
        target_func = next((i for i in scanned if i.kind == "function"), None)
        
        if target_func:
            deps = sorted(list(set(target_func.dependencies)))
            st.write(f"**Calls ({len(deps)}):**")
            
            # Check which are in DB
            for dep in deps:
                data = knowledge_graph.get_function_by_name(dep)
                if data:
                    st.success(f"• {dep} (Found in {data['source']})")
                else:
                    st.warning(f"• {dep} (Not found in DB)")
        else:
            st.warning("Could not parse function definition.")

    with col2:
        st.subheader("Contextual Refinement")
        
        # Check Project Header
        header_content = "// project.h not found"
        if project_h_path.exists():
            st.success("✅ project.h loaded")
            with open(project_h_path, "r", encoding="utf-8") as f:
                header_content = f.read()
            with st.expander("View project.h"):
                st.code(header_content, language="cpp")
        else:
            st.error("❌ project.h missing. Run Step 1 (Header Synthesis) first.")
            st.stop()

        if st.button("Run Contextual Pass", type="primary"):
            with st.spinner("Refining with context..."):
                new_code = contextual_improver.run_contextual_improvement(
                    current_code, 
                    header_content, 
                    provider=provider,
                    model_name="openai/gpt-oss-20b" if provider=="local" else "gemini-1.5-flash"
                )
                st.session_state[f"ctx_{selected_func_name}"] = new_code
        
        # Show result
        if f"ctx_{selected_func_name}" in st.session_state:
            st.write("### Proposed Result")
            st.code(st.session_state[f"ctx_{selected_func_name}"], language="cpp")
            
            if st.button("Save & Overwrite"):
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(st.session_state[f"ctx_{selected_func_name}"])
                st.success("Saved!")
                # Optional: trigger re-indexing? 
                # For now user must re-scan in Step 1 manually or we add auto-update logic later.
                del st.session_state[f"ctx_{selected_func_name}"]
                st.rerun()
