"""
Metadata Inspector & Scrubber
Run with:  streamlit run app.py
"""

import re
import shutil
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

import metadata_core as mc

st.set_page_config(page_title="Metadata Inspector & Scrubber", page_icon="🗂️", layout="wide")

# --------------------------------------------------------------------------- #
# Session helpers
# --------------------------------------------------------------------------- #

def _workdir() -> Path:
    if "workdir" not in st.session_state:
        st.session_state.workdir = Path(tempfile.mkdtemp(prefix="metatool_"))
    return st.session_state.workdir


def _save_upload(uploaded) -> Path:
    """Write the uploaded file to a private temp folder (once per upload)."""
    key = f"{uploaded.file_id}"
    if st.session_state.get("saved_key") != key:
        wd = _workdir()
        for p in wd.iterdir():                    # discard the previous file
            p.unlink(missing_ok=True) if p.is_file() else shutil.rmtree(p, ignore_errors=True)
        safe = re.sub(r"[^\w.\- ]", "_", uploaded.name) or "upload.bin"
        path = wd / safe
        path.write_bytes(uploaded.getbuffer())
        st.session_state.saved_key = key
        st.session_state.saved_path = str(path)
        st.session_state.pop("clean_result", None)
    return Path(st.session_state.saved_path)


def _sections_table(section: mc.Section) -> pd.DataFrame:
    rows = [{"Field": k, "Value": str(v),
             "Privacy": "⚠️ sensitive" if section.kind == mc.METADATA and mc.is_sensitive(k) else ""}
            for k, v in section.items.items()]
    return pd.DataFrame(rows)


def _render_sections(sections, expand_metadata=True):
    meta = [s for s in sections if s.kind == mc.METADATA]
    tech = [s for s in sections if s.kind == mc.TECHNICAL]

    st.markdown("#### Metadata")
    if not meta:
        st.success("No descriptive or identifying metadata found.")
    for s in meta:
        with st.expander(f"**{s.name}** · {len(s.items)} field(s)", expanded=expand_metadata):
            st.dataframe(_sections_table(s), hide_index=True, use_container_width=True,
                         column_config={"Field": st.column_config.TextColumn(width="medium"),
                                        "Value": st.column_config.TextColumn(width="large"),
                                        "Privacy": st.column_config.TextColumn(width="small")})

    st.markdown("#### File & technical properties")
    for s in tech:
        with st.expander(f"{s.name} · {len(s.items)} field(s)", expanded=False):
            st.dataframe(_sections_table(s).drop(columns="Privacy"), hide_index=True,
                         use_container_width=True)


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #

st.title("🗂️ Metadata Inspector & Scrubber")
st.caption("View hidden metadata in your files — and remove it before sharing. "
           "Everything runs locally on your PC; nothing is uploaded anywhere.")

with st.sidebar:
    st.header("Supported formats")
    st.markdown(
        "**Images:** JPG, PNG, BMP, GIF, TIFF, WebP  \n"
        "**Documents:** PDF, DOCX, XLSX, PPTX  \n"
        "**Video:** MP4, MOV, M4V, MKV, AVI, WebM, WMV, 3GP  \n"
        "**Audio:** MP3, FLAC, OGG, M4A, WAV, AAC, WMA"
    )
    st.caption("Other file types can still be *viewed* (best effort).")
    st.divider()
    st.markdown("**How cleaning works**")
    st.markdown(
        "- JPG / PNG: lossless — pixels are copied byte-for-byte\n"
        "- Video / audio: stream copy — no re-encoding\n"
        "- PDF: fully rewritten, so old info can't be recovered\n"
        "- Your original file is never modified; you download a cleaned copy"
    )
    if shutil.which("exiftool"):
        st.success("ExifTool detected — extra-detailed view enabled.")

uploaded = st.file_uploader("Browse for a file", type=None,
                            help="Click 'Browse files' or drag a file here.")

mode = st.radio("Choose an option", ["🔍 Check metadata", "🧹 Remove metadata"],
                horizontal=True, label_visibility="visible")

if uploaded is None:
    st.info("👆 Click **Browse files** to pick a file from your computer.")
    st.stop()

path = _save_upload(uploaded)
ext = path.suffix.lower()

with st.spinner("Reading metadata…"):
    sections = mc.read_metadata(path, display_name=uploaded.name)

n_meta = mc.count_metadata(sections)
sensitive = mc.sensitive_fields(sections)

c1, c2, c3, c4 = st.columns(4)
c1.metric("File type", mc.category_of(path))
c2.metric("Size", f"{path.stat().st_size / 1024:,.1f} KB")
c3.metric("Metadata fields", n_meta)
c4.metric("Privacy-sensitive", len(sensitive))

# --------------------------------------------------------------------------- #
# Option 1 — Check
# --------------------------------------------------------------------------- #
if mode.startswith("🔍"):
    if sensitive:
        st.warning("This file contains potentially identifying information "
                   "(e.g. location, device, author, timestamps):\n\n"
                   + "\n".join(f"- {s}" for s in sensitive[:12])
                   + (f"\n- …and {len(sensitive) - 12} more" if len(sensitive) > 12 else ""))
    _render_sections(sections)
    st.download_button("⬇️ Download metadata report (JSON)", data=mc.to_json(sections),
                       file_name=f"{Path(uploaded.name).stem}_metadata.json",
                       mime="application/json")

# --------------------------------------------------------------------------- #
# Option 2 — Remove
# --------------------------------------------------------------------------- #
else:
    if ext not in mc.SUPPORTED_REMOVAL:
        st.error(f"Removing metadata from **{ext or 'this'}** files isn't supported yet. "
                 "You can still use *Check metadata* to view it.")
        st.stop()

    if n_meta == 0:
        st.info("No removable metadata was detected, but you can still produce a cleaned copy.")
    else:
        st.write(f"Found **{n_meta}** metadata field(s) to remove.")

    if st.button("🧹 Remove metadata", type="primary"):
        out = path.with_name(f"{path.stem}_clean{path.suffix}")
        try:
            with st.spinner("Removing metadata…"):
                notes = mc.remove_metadata(path, out)
                after = mc.read_metadata(out, display_name=out.name)
            st.session_state.clean_result = {"path": str(out), "notes": notes, "after": after}
        except Exception as e:
            st.session_state.pop("clean_result", None)
            st.error(f"Couldn't clean this file: {e}")

    result = st.session_state.get("clean_result")
    if result:
        out = Path(result["path"])
        after = result["after"]
        n_after = mc.count_metadata(after)
        st.success("Done! Your cleaned copy is ready to download.")

        a1, a2, a3 = st.columns(3)
        a1.metric("Metadata fields", n_after, delta=n_after - n_meta, delta_color="inverse")
        a2.metric("Privacy-sensitive", len(mc.sensitive_fields(after)),
                  delta=len(mc.sensitive_fields(after)) - len(sensitive), delta_color="inverse")
        a3.metric("New size", f"{out.stat().st_size / 1024:,.1f} KB")

        with st.container(border=True):
            st.markdown("**What was done**")
            st.markdown("\n".join(f"- {n}" for n in result["notes"]))

        st.download_button("⬇️ Download cleaned file", data=out.read_bytes(),
                           file_name=f"{Path(uploaded.name).stem}_clean{path.suffix}",
                           type="primary")

        with st.expander("🔎 Verify: metadata remaining in the cleaned file"):
            _render_sections(after, expand_metadata=True)
