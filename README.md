# 🗂️ Metadata Inspector & Scrubber

A local Streamlit app to **view** and **remove** hidden metadata (GPS location, camera/phone model, author, timestamps, software, comments…) from images, documents, video and audio files. Everything runs on your own PC — files never leave your machine.

## Features

| Option | What it does |
|---|---|
| 🔍 **Check metadata** | Shows all metadata grouped by type (EXIF, GPS, PDF info, video tags…), flags privacy-sensitive fields, gives a map link for GPS photos, and lets you export a JSON report. |
| 🧹 **Remove metadata** | Creates a cleaned copy, shows a before/after count, lists exactly what was removed, and re-scans the cleaned file so you can verify it. Your original is never modified. |

### Supported formats

| Type | Formats | How it's cleaned |
|---|---|---|
| Images | JPG, PNG | **Lossless** — metadata segments dropped, pixels copied byte-for-byte |
| Images | BMP, GIF (incl. animated), TIFF, WebP | Rebuilt from pixel data (lossless compression for TIFF/WebP) |
| Documents | PDF | Info dictionary + XMP removed; file fully rewritten (old data not recoverable) |
| Documents | DOCX, XLSX, PPTX | Core/app/custom properties cleared (author, company, last-modified-by…) |
| Video | MP4, MOV, M4V, MKV, AVI, WebM, WMV, 3GP | Remuxed with ffmpeg **stream copy** — no re-encoding, no quality loss |
| Audio | MP3, FLAC, OGG, M4A, WAV, AAC, WMA | Tags and cover art removed; audio untouched |

Any other file type can still be opened in *Check metadata* (best effort).

## Setup & run

Requires **Python 3.9+**. ffmpeg is bundled automatically via the `imageio-ffmpeg` package — nothing else to install.

**Easiest:** double-click `run_windows.bat` (Windows) or run `./run_mac_linux.sh` (macOS/Linux). The first run creates a virtual environment and installs dependencies.

**Manual:**

```bash
python -m venv .venv
# Windows:   .venv\Scripts\activate
# mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

Your browser opens at `http://localhost:8501`. Click **Browse files**, pick a file, choose an option.

## Project structure

```
metadata_tool/
├── app.py               # Streamlit UI
├── metadata_core.py     # Read/remove engine (usable without the UI)
├── requirements.txt
├── run_windows.bat
├── run_mac_linux.sh
└── .streamlit/config.toml   # upload limit (4 GB), theme
```

The engine can also be used from Python directly:

```python
import metadata_core as mc
sections = mc.read_metadata("photo.jpg")
print(mc.to_json(sections))
mc.remove_metadata("photo.jpg", "photo_clean.jpg")
```

## Notes & limitations

- **JPEGs with a rotation flag** are re-encoded at quality 95 with the rotation baked in; otherwise removing EXIF would make them display sideways. All other JPEGs are cleaned losslessly.
- **ICC color profiles** are kept, since they're needed for correct colors and contain no personal data.
- **Office documents:** names in comments and tracked changes live in the document body and aren't touched — use Office's *File → Info → Inspect Document* for those.
- **Password-protected PDFs** can be viewed but not cleaned.
- **HEIC/HEIF** (iPhone photos) isn't supported. Convert to JPG first, or add the `pillow-heif` package.
- **Optional:** if [ExifTool](https://exiftool.org) is installed and on your PATH, the app adds a full ExifTool dump to the *Check* view automatically.
- Upload limit is 4 GB; change `maxUploadSize` in `.streamlit/config.toml` if needed.
