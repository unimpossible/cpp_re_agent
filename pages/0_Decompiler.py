import streamlit as st
import os
import sys
import time
import json
import pandas as pd
from pathlib import Path

# Add parent dir to sys.path to import modules
sys.path.append(os.path.join(os.getcwd()))

import decompiler
import ai_improver

# Constants
WORKSPACE_DIR = os.path.join(os.getcwd(), "workspace")
CONFIG_FILE = os.path.join(os.getcwd(), "config.json")

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return {"provider": "local", "binary_path": "hello_world"}

def save_config():
    config = {
        "provider": st.session_state.provider_input,
        "binary_path": st.session_state.binary_input
    }
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f)

st.set_page_config(layout="wide", page_title="Decompiler")

st.title("Decompilation & Improvement Engine")

# Sidebar - Configuration & Selection
with st.sidebar:
    st.header("Configuration")
    
    # Load Config
    config = load_config()
    
    # Provider Selection
    options = ["gemini", "local"]
    default_index = 0
    if config["provider"] in options:
        default_index = options.index(config["provider"])

    provider = st.selectbox(
        "AI Provider", 
        options, 
        index=default_index,
        key="provider_input",
        on_change=save_config
    )
    
    if provider == "gemini":
        api_key = st.text_input("Gemini API Key", type="password")
        if api_key:
            os.environ["GEMINI_API_KEY"] = api_key
    elif provider == "local":
        st.info("Using Local LLM at http://localhost:1234/v1 (Default)")
    
    st.divider()
    
    st.header("Target Binary")
    binary_path = st.text_input(
        "Binary Path", 
        value=config.get("binary_path", "hello_world"),
        key="binary_input",
        on_change=save_config
    )
    
    if st.button("Load / Decompile"):
        with st.spinner("Decompiling..."):
            # logic to define output dir based on binary name
            binary_name = Path(binary_path).name
            output_dir = os.path.join(WORKSPACE_DIR, binary_name, "raw")
            decompiler.decompile_binary(binary_path, output_dir)
            st.session_state["output_dir"] = output_dir
            st.success("Decompilation Complete!")

    st.divider()
    
    # Function List
    output_dir = st.session_state.get("output_dir")
    selected_func = None
    
    if output_dir:
        functions = decompiler.get_functions(output_dir)
        
        # Define improved directory logic
        improved_dir = Path(output_dir).parent / "improved"
        os.makedirs(improved_dir, exist_ok=True)
        
        # Batch Processing
        if st.button(f"Batch Improve All ({len(functions)} functions)"):
            progress_bar = st.progress(0)
            status_text = st.empty()
            
            # Convert to list for iteration
            func_items = list(functions.items())
            
            for i, (name, code) in enumerate(func_items):
                status_text.text(f"Processing {i+1}/{len(func_items)}: {name}")
                
                target_file = improved_dir / f"{name}.cpp"
                
                # Skip if already exists to save time/cost
                if not target_file.exists():
                    if ai_improver.should_improve(code):
                        try:
                            # Get binary_path - using loop context or config
                            bin_path_batch = config.get("binary_path", "hello_world")
                            improved_code = ai_improver.improve_function(
                                code,
                                provider=provider,
                                model_name="gemini-2.5-flash" if provider == "gemini" else "openai/gpt-oss-20b",
                                binary_path=bin_path_batch,
                                recursive=False
                            )
                            with open(target_file, "w", encoding="utf-8") as f:
                                f.write(improved_code)
                        except Exception as e:
                            print(f"Error improving {name}: {e}")
                
                progress_bar.progress((i + 1) / len(func_items))
            
            status_text.success("Batch Processing Complete!")
            time.sleep(2)
            status_text.empty()
            progress_bar.empty()
            st.rerun()
        
        # Create DataFrame
        data = [{"Function Name": name, "Size": len(code)} for name, code in functions.items()]
        df = pd.DataFrame(data).sort_values("Size", ascending=False).reset_index(drop=True)
        
        st.write("Select Function:")
        event = st.dataframe(
            df,
            column_config={
                "Function Name": st.column_config.TextColumn("Function", help="Name of the function"),
                "Size": st.column_config.ProgressColumn("Size", format="%d chars", min_value=0, max_value=max(d["Size"] for d in data) if data else 0),
            },
            width="stretch",
            hide_index=True,
            on_select="rerun",
            selection_mode="single-cell",
            key="func_table"
        )
        
        if len(event.selection.cells) > 0:
            selected_idx = event.selection.cells[0][0]
            selected_func_name = df.iloc[selected_idx]["Function Name"]
            selected_func = functions[selected_func_name]
        else:
            st.info("Select a row above.")
    else:
        st.info("Load a binary to see functions.")

# Main Area
if selected_func:
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("Raw Decompilation")
        st.code(selected_func, language="cpp")
        
    with col2:
        st.subheader("AI Improved")
        
        # Define improved directory: workspace/binary_name/improved
        # output_dir is workspace/binary_name/raw
        improved_dir = Path(output_dir).parent / "improved"
        os.makedirs(improved_dir, exist_ok=True)
        improved_file_path = improved_dir / f"{selected_func_name}.cpp"
        
        # Load from disk if exists and not in session state
        improved_key = f"improved_{selected_func_name}"
        if improved_key not in st.session_state and improved_file_path.exists():
             with open(improved_file_path, "r", encoding="utf-8") as f:
                 st.session_state[improved_key] = f.read()
        
        if improved_key not in st.session_state:
            if st.button("Improve with AI"):
                if ai_improver.should_improve(selected_func):
                    with st.status("AI is thinking...", expanded=True) as status:
                        # Get binary_path relative to workspace or config
                        bin_path = config.get("binary_path", "hello_world")
                        
                        def update_status(msg):
                            status.write(msg)
                        
                        improved_code = ai_improver.improve_function(
                            selected_func, 
                            provider=provider,
                            model_name="gemini-2.5-flash" if provider == "gemini" else "openai/gpt-oss-20b",
                            binary_path=bin_path,
                            recursive=True,
                            status_callback=update_status
                        )
                        status.update(label="Improvement Complete!", state="complete", expanded=False)
                        st.session_state[improved_key] = improved_code
                        
                        # Save to disk
                        with open(improved_file_path, "w", encoding="utf-8") as f:
                            f.write(improved_code)
                            
                        st.rerun() # Rerun to show the code
                else:
                    st.warning("Function is too simple or empty to improve.")
        
        if improved_key in st.session_state:
            st.code(st.session_state[improved_key], language="cpp")
            if st.button("Clear Improved Version"):
                del st.session_state[improved_key]
                if improved_file_path.exists():
                    os.remove(improved_file_path)
                st.rerun()

else:
    st.write("👈 Select a binary and function from the sidebar to begin.")
