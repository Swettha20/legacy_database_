import sys
import os

# Make app/ importable so we can pull in detector.py and registry.py directly
sys.path.append(os.path.join(os.path.dirname(__file__), 'app'))

import streamlit as st
from detector import detect_file_type
from registry import PluginRegistry

st.set_page_config(page_title="AI Code Migrator", page_icon="🔄", layout="wide")

st.title("🔄 AI Code Migrator")
st.caption("Upload a legacy file. We'll detect what it is and offer a plugin to modernize it — powered by a local AI model.")

registry = PluginRegistry()

uploaded_file = st.file_uploader(
    "Upload a legacy file",
    type=["java", "php", "sql", "txt"],
    help="Java, PHP, SQL, or even a .txt file — detection is based on content, not extension."
)

if uploaded_file is not None:
    content = uploaded_file.read().decode("utf-8")

    with st.expander("📄 View uploaded file content", expanded=False):
        st.code(content, language=None)

    detected_type = detect_file_type(content)

    if detected_type == "unknown":
        st.error(f"Couldn't detect a supported file type for **{uploaded_file.name}**. Supported types: Java, PHP, SQL.")
    else:
        st.success(f"Detected type: **{detected_type.upper()}**")

        matching_plugins = registry.get_plugins_for(detected_type)

        if not matching_plugins:
            st.warning(f"No plugin currently handles '{detected_type}' files.")
        else:
            plugin_names = [p.name for p in matching_plugins]
            selected_name = st.selectbox("Choose a conversion plugin:", plugin_names)
            selected_plugin = next(p for p in matching_plugins if p.name == selected_name)

            st.info(f"**{selected_plugin.source_type}** → **{selected_plugin.target_type}**")

            if st.button("🚀 Convert", type="primary"):
                with st.spinner(f"Running {selected_plugin.name}... this may take a minute (local AI model)"):
                    try:
                        result = selected_plugin.convert(content)
                        st.session_state["conversion_result"] = result
                        st.session_state["target_type"] = selected_plugin.target_type
                    except Exception as e:
                        st.error(f"Conversion failed: {e}")

            if "conversion_result" in st.session_state:
                st.subheader("✅ Converted Output")
                st.code(st.session_state["conversion_result"], language=None)

                extension_map = {
                    "python": "py",
                    "node": "js",
                    "postgresql": "sql",
                }
                out_ext = extension_map.get(st.session_state["target_type"], "txt")
                out_filename = f"converted.{out_ext}"

                st.download_button(
                    "⬇️ Download converted file",
                    data=st.session_state["conversion_result"],
                    file_name=out_filename,
                    mime="text/plain"
                )
else:
    st.info("👆 Upload a file to get started.")