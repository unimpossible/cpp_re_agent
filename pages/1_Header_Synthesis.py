import streamlit as st
import os
import sys
from pathlib import Path

# Add parent dir to sys.path to import modules
sys.path.append(os.path.join(os.getcwd()))

import scanner
import knowledge_graph
import decompiler

st.set_page_config(layout="wide", page_title="Header Synthesis")

st.title("Header Synthesis Engine")

# Configuration (Shared)
# In a real app we'd put this in a shared state or config file
# For now we reuse the config.json logic
import json
CONFIG_FILE = os.path.join(os.getcwd(), "..", "config.json") # Adjust path relative to pages/
if not os.path.exists(CONFIG_FILE):
    CONFIG_FILE = os.path.join(os.getcwd(), "config.json")

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

st.info(f"Target: {binary_path} | Provider: {provider}")

col1, col2 = st.columns(2)

with col1:
    st.header("1. Maps (Extract Types)")
    if st.button("Scan All Improved Files"):
        binary_name = Path(binary_path).name
        # Assuming workspace structure checks out
        workspace_dir = os.path.join(os.getcwd(), "workspace")
        improved_dir = Path(workspace_dir) / binary_name / "improved"
        
        if not improved_dir.exists():
            st.error("No improved files found. Go to the main page and process the binary first.")
        else:
            files = list(improved_dir.glob("*.cpp"))
            progress_bar = st.progress(0)
            status = st.empty()
            
            # Clear old DB before full scan (optional, but good for cleanliness)
            knowledge_graph.clear_db() 
            
            count_types = 0
            count_funcs = 0
            
            for i, fpath in enumerate(files):
                status.text(f"Scanning {fpath.name}...")
                with open(fpath, "r") as f:
                    code = f.read()
                
                # Extract
                items = scanner.scan_code(code)
                
                # Store (The new add_scan_results handles splitting)
                if items:
                    knowledge_graph.add_scan_results(items, fpath.name)
                    
                    # Just for stats
                    count_types += len([t for t in items if t.kind != "function"])
                    count_funcs += len([t for t in items if t.kind == "function"])
                
                progress_bar.progress((i + 1) / len(files))
                
            status.success(f"Scan Complete. Indexed {count_types} types and {count_funcs} functions.")
            
    # Show current DB stats
    types = knowledge_graph.get_all_types()
    if types:
        st.write(f"**Database contains {len(types)} candidate types.**")
        st.dataframe(types)

with col2:
    st.header("2. Reduce (Synthesize Header)")
    
    if st.button("Consolidate & Generate project.h"):
        with st.spinner("Merging types..."):
            header_content = knowledge_graph.consolidate_types(provider=provider)
            st.session_state["header_content"] = header_content
            
    if "header_content" in st.session_state:
        st.subheader("Preview: project.h")
        st.code(st.session_state["header_content"], language="cpp")
        
        if st.button("Save project.h"):
            # Save to workspace/binary/project.h
            binary_name = Path(binary_path).name
            workspace_dir = os.path.join(os.getcwd(), "workspace")
            save_path = Path(workspace_dir) / binary_name / "project.h"
            
            with open(save_path, "wb") as f:
                f.write(st.session_state["header_content"].encode("utf-8"))
            st.success(f"Saved to {save_path}")
