import streamlit as st
import os
import time
import json
import pandas as pd
from pathlib import Path

from cpp_re_agent import decompiler
from cpp_re_agent import ai_improver
from cpp_re_agent import callgraph
from cpp_re_agent import symbol_map

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

# Default model per provider, used when no custom model name is configured.
DEFAULT_MODELS = {"gemini": "gemini-2.5-flash", "local": "openai/gpt-oss-20b"}

def default_model_for(provider):
    return DEFAULT_MODELS.get(provider, "openai/gpt-oss-20b")

def save_config():
    config = {
        "provider": st.session_state.provider_input,
        "binary_path": st.session_state.binary_input,
        "model_name": st.session_state.get("model_input", "").strip()
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
        default_url = "http://localhost:1234/v1"
        local_url = os.getenv("LOCAL_LLM_URL", default_url)
        suffix = " (Default)" if local_url == default_url else ""
        st.info(f"Using Local LLM at {local_url}{suffix}")

    # Model name: defaults per provider, but the user can override (e.g. to
    # match whatever a custom OpenAI-compatible endpoint serves).
    saved_model = config.get("model_name", "")
    model_name = st.text_input(
        "Model Name",
        value=saved_model or default_model_for(provider),
        key="model_input",
        on_change=save_config,
        help="Model identifier sent to the provider. Defaults per provider; override for custom endpoints.",
    ).strip() or default_model_for(provider)

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

            # Process functions leaves-first so callers see improved callee
            # signatures (carried in the shared symbol map) as context.
            workspace_dir = improved_dir.parent
            graph = callgraph.build_callgraph(functions)
            # Score each function once; use it only to break ties in the
            # leaves-first order so high-value functions are improved earlier.
            scores = {n: ai_improver.score_function(c) for n, c in functions.items()}
            order = callgraph.topological_order(graph, priority=scores.get)
            valid_names = set(functions.keys())
            symbols = symbol_map.load_symbols(workspace_dir)
            bin_path_batch = config.get("binary_path", "hello_world")

            for i, name in enumerate(order):
                code = functions[name]
                status_text.text(f"Processing {i+1}/{len(order)}: {name}")

                target_file = improved_dir / f"{name}.cpp"

                # Skip if already exists, but still record its signature so
                # callers later in the order can use it as context.
                if target_file.exists():
                    if name not in symbols:
                        symbol_map.record_improvement(symbols, name, target_file.read_text(encoding="utf-8"))
                elif ai_improver.should_improve(code, name=name):
                    try:
                        improved_code = ai_improver.improve_function(
                            code,
                            provider=provider,
                            model_name=model_name,
                            binary_path=bin_path_batch,
                            recursive=False,
                            symbols=symbols,
                            valid_names=valid_names,
                        )
                        target_file.write_text(improved_code, encoding="utf-8")
                        symbol_map.record_improvement(symbols, name, improved_code)
                    except Exception as e:
                        print(f"Error improving {name}: {e}")

                progress_bar.progress((i + 1) / len(order))

            symbol_map.save_symbols(workspace_dir, symbols)
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

                        # Live preview of the streaming model output.
                        stream_box = st.empty()

                        def update_status(msg):
                            status.write(msg)

                        def on_stream(text):
                            # Show a tail of the running output so the box
                            # doesn't grow unbounded while streaming.
                            stream_box.code(text[-2000:], language="cpp")

                        improved_code = ai_improver.improve_function(
                            selected_func,
                            provider=provider,
                            model_name=model_name,
                            binary_path=bin_path,
                            recursive=True,
                            status_callback=update_status,
                            stream_callback=on_stream,
                        )
                        stream_box.empty()
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
