"""
metadata_core.py
----------------
Format-aware engine for READING and REMOVING file metadata.

Supported (read + remove):
  Images  : jpg/jpeg, png, bmp, gif, tif/tiff, webp
  Documents: pdf, docx, xlsx, pptx
  Video   : mp4, mov, m4v, mkv, avi, webm, wmv, 3gp
  Audio   : mp3, flac, ogg, opus, m4a, wav, aac, wma

Read-only fallback for anything else via hachoir (and ExifTool if installed).

Design notes
  * JPEG and PNG are cleaned LOSSLESSLY (metadata segments/chunks are dropped,
    pixel data is copied byte-for-byte). The one exception: a JPEG with an EXIF
    rotation flag is re-encoded (quality 95) so it doesn't appear sideways.
  * Audio/video are remuxed with ffmpeg using stream copy (-c copy): no
    re-encoding, no quality loss. ffmpeg ships inside the imageio-ffmpeg wheel,
    so nothing extra has to be installed.
"""

from __future__ import annotations

import datetime as _dt
import json
import mimetypes
import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

METADATA = "metadata"    # descriptive / identifying info (what gets removed)
TECHNICAL = "technical"  # structural properties (size, codec, pages...)


@dataclass
class Section:
    name: str
    kind: str = METADATA
    items: dict = field(default_factory=dict)


class UnsupportedFormat(Exception):
    pass


IMAGE_EXT = {".jpg", ".jpeg", ".jpe", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp"}
PDF_EXT = {".pdf"}
OFFICE_EXT = {".docx", ".xlsx", ".pptx", ".docm", ".xlsm", ".pptm"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".wmv", ".3gp", ".mpg", ".mpeg"}
AUDIO_EXT = {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".wav", ".aac", ".wma", ".aiff"}
MUTAGEN_STRIP_EXT = {".mp3", ".flac", ".ogg", ".opus"}  # mutagen deletes tags in-place cleanly

SUPPORTED_REMOVAL = IMAGE_EXT | PDF_EXT | OFFICE_EXT | VIDEO_EXT | AUDIO_EXT

# Field names that commonly reveal who / where / when / with-what.
SENSITIVE_PATTERNS = [
    "gps", "location", "iso6709", "latitude", "longitude", "altitude",
    "author", "creator", "artist", "owner", "lastmodifiedby", "last_modified_by",
    "make", "model", "serial", "lens", "bodyserial", "camera",
    "software", "producer", "encoder", "application", "company", "manager",
    "datetime", "date", "created", "creation", "modified", "time",
    "comment", "description", "title", "subject", "keywords", "copyright",
    "user", "host", "xmp", "xml:com.adobe.xmp", "uniqueid", "documentid",
]


def is_sensitive(key: str) -> bool:
    k = key.lower().replace(" ", "").replace("-", "")
    return any(p in k for p in SENSITIVE_PATTERNS)


def category_of(path: str | Path) -> str:
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXT:
        return "Image"
    if ext in PDF_EXT:
        return "PDF document"
    if ext in OFFICE_EXT:
        return "Office document"
    if ext in VIDEO_EXT:
        return "Video"
    if ext in AUDIO_EXT:
        return "Audio"
    return "Other"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _fmt(value, limit: int = 300) -> str:
    """Make any metadata value printable and reasonably short."""
    try:
        if isinstance(value, bytes):
            try:
                text = value.decode("utf-8").strip("\x00 ")
                if text.isprintable():
                    value = text
                else:
                    raise ValueError
            except Exception:
                return f"<binary data, {len(value)} bytes>"
        if isinstance(value, float):
            value = round(value, 6)
        if isinstance(value, (tuple, list)):
            value = ", ".join(_fmt(v, 60) for v in value)
        s = str(value).strip()
    except Exception:
        s = repr(value)
    return s if len(s) <= limit else s[:limit] + f"… ({len(s)} chars)"


def _fix_utf8(s: str) -> str:
    """EXIF text is read as Latin-1; repair values that were really UTF-8 (e.g. 'Â©' -> '©')."""
    try:
        return s.encode("latin-1").decode("utf-8") if not s.isascii() else s
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        exe = shutil.which("ffmpeg")
        if exe:
            return exe
        raise RuntimeError(
            "ffmpeg not found. Run: pip install imageio-ffmpeg (bundles ffmpeg)."
        )


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW on Windows
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", creationflags=flags)


def count_metadata(sections: list[Section]) -> int:
    return sum(len(s.items) for s in sections if s.kind == METADATA)


def sensitive_fields(sections: list[Section]) -> list[str]:
    out = []
    for s in sections:
        if s.kind != METADATA:
            continue
        for k in s.items:
            if is_sensitive(k):
                out.append(f"{s.name} › {k}")
    return out


def to_json(sections: list[Section]) -> str:
    return json.dumps({s.name: {"kind": s.kind, "fields": s.items} for s in sections},
                      indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# READ
# --------------------------------------------------------------------------- #

def read_metadata(path: str | Path, display_name: str | None = None) -> list[Section]:
    path = Path(path)
    ext = path.suffix.lower()
    st = path.stat()
    mime = mimetypes.guess_type(display_name or path.name)[0] or "unknown"

    sections = [Section("File", TECHNICAL, {
        "File name": display_name or path.name,
        "Category": category_of(path),
        "MIME type": mime,
        "Size": f"{st.st_size:,} bytes ({st.st_size / 1024 / 1024:.2f} MB)",
    })]

    readers = []
    if ext in IMAGE_EXT:
        readers.append(_read_image)
    elif ext in PDF_EXT:
        readers.append(_read_pdf)
    elif ext in OFFICE_EXT:
        readers.append(_read_office)
    elif ext in VIDEO_EXT or ext in AUDIO_EXT:
        readers += [_read_ffmpeg]
        if ext in AUDIO_EXT:
            readers.append(_read_mutagen)
        readers.append(_read_hachoir)
    else:
        readers.append(_read_hachoir)

    for reader in readers:
        try:
            sections += reader(path)
        except Exception as e:  # keep going – one failing parser shouldn't hide the rest
            sections.append(Section(f"{reader.__name__.replace('_read_', '').title()} (error)",
                                    TECHNICAL, {"Error": str(e)}))

    if shutil.which("exiftool"):
        try:
            sections += _read_exiftool(path)
        except Exception:
            pass

    return [s for s in sections if s.items]


# ---- images ---------------------------------------------------------------- #

def _read_image(path: Path) -> list[Section]:
    from PIL import ExifTags, Image

    sections = []
    with Image.open(path) as im:
        tech = {
            "Format": im.format,
            "Dimensions": f"{im.width} × {im.height} px",
            "Color mode": im.mode,
            "Frames": getattr(im, "n_frames", 1),
        }
        sections.append(Section("Image properties", TECHNICAL, tech))

        exif = im.getexif()
        base = {}
        for tag, val in exif.items():
            if tag in (0x8769, 0x8825, 0xA005):  # IFD pointers – expanded below
                continue
            name = ExifTags.TAGS.get(tag, f"Tag 0x{tag:04X}")
            if tag in _TIFF_STRUCTURAL:          # image layout, not descriptive
                tech[f"TIFF › {name}"] = _fmt(val, 80)
            else:
                base[name] = _fix_utf8(_fmt(val))
        if base:
            sections.append(Section("EXIF (main)", METADATA, base))

        try:
            sub = exif.get_ifd(0x8769)
            items = {ExifTags.TAGS.get(t, f"Tag 0x{t:04X}"): _fix_utf8(_fmt(v)) for t, v in sub.items()}
            if items:
                sections.append(Section("EXIF (camera settings)", METADATA, items))
        except Exception:
            pass

        try:
            gps = exif.get_ifd(0x8825)
            items = {ExifTags.GPSTAGS.get(t, f"GPS 0x{t:02X}"): _fmt(v) for t, v in gps.items()}
            coords = _gps_to_decimal(gps)
            if coords:
                items["▶ Decimal coordinates"] = f"{coords[0]:.6f}, {coords[1]:.6f}"
                items["▶ Map link"] = f"https://maps.google.com/?q={coords[0]:.6f},{coords[1]:.6f}"
            if items:
                sections.append(Section("GPS location", METADATA, items))
        except Exception:
            pass

        # Text chunks / comments / XMP / ICC and other info entries
        other = {}
        skip = {"exif", "dpi", "resolution", "transparency", "duration", "loop", "background",
                "version", "extension", "progression", "progressive", "jfif",
                "jfif_version", "jfif_unit", "jfif_density", "adobe", "adobe_transform",
                "compression", "interlace", "gamma", "aspect", "chromaticity",
                "srgb", "disposal", "blend", "default_image"}
        for k, v in im.info.items():
            if k in skip:
                continue
            if k == "icc_profile":
                tech["ICC color profile"] = _icc_description(v)  # color data, not personal
            elif k in ("xmp", "XML:com.adobe.xmp"):
                other["XMP packet"] = f"present ({len(v)} bytes)"
                other.update({f"XMP › {a}": b for a, b in _xmp_fields(v).items()})
            else:
                other[str(k)] = _fmt(v)
        if im.format == "TIFF":
            # Descriptive TIFF tags (Artist, Software, DateTime...) are also in EXIF above
            pass
        if other:
            sections.append(Section("Embedded text / profiles", METADATA, other))
    return sections


# Baseline TIFF tags describing pixel layout (width, strips, compression...)
_TIFF_STRUCTURAL = {254, 255, 256, 257, 258, 259, 262, 266, 273, 277, 278, 279, 280, 281,
                    282, 283, 284, 296, 317, 318, 319, 320, 322, 323, 324, 325, 338, 339,
                    340, 341, 513, 514, 529, 530, 531, 532}


def _gps_to_decimal(gps) -> tuple[float, float] | None:
    try:
        def conv(dms, ref):
            d, m, s = (float(x) for x in dms)
            val = d + m / 60 + s / 3600
            return -val if ref in ("S", "W") else val
        return conv(gps[2], gps[1]), conv(gps[4], gps[3])
    except Exception:
        return None


def _icc_description(data: bytes) -> str:
    try:
        from io import BytesIO
        from PIL import ImageCms
        prof = ImageCms.ImageCmsProfile(BytesIO(data))
        return f"{ImageCms.getProfileDescription(prof).strip()} ({len(data)} bytes)"
    except Exception:
        return f"present ({len(data)} bytes)"


def _xmp_fields(raw) -> dict:
    """Pull a few human-readable values out of an XMP packet."""
    text = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else str(raw)
    out = {}
    for tag in ("CreatorTool", "creator", "CreateDate", "ModifyDate", "MetadataDate",
                "Make", "Model", "rights", "title", "description", "DocumentID"):
        m = re.search(rf"{tag}[=>]\s*\"?([^\"<]{{1,200}})", text)
        if m and m.group(1).strip():
            out[tag] = m.group(1).strip()
    return out


# ---- pdf ------------------------------------------------------------------- #

def _read_pdf(path: Path) -> list[Section]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    encrypted = reader.is_encrypted
    if encrypted:
        try:
            reader.decrypt("")
        except Exception:
            pass

    tech = {"Encrypted": "Yes" if encrypted else "No"}
    try:
        tech["Pages"] = len(reader.pages)
    except Exception:
        tech["Pages"] = "unknown (encrypted)"
    tech["PDF version"] = reader.pdf_header.replace("%PDF-", "")
    sections = [Section("PDF properties", TECHNICAL, tech)]

    info = {}
    try:
        meta = reader.metadata or {}
        for k, v in meta.items():
            info[str(k).lstrip("/")] = _fmt(v)
    except Exception:
        pass
    if info:
        sections.append(Section("Document info", METADATA, info))

    try:
        root = reader.trailer["/Root"]
        if "/Metadata" in root:
            raw = root["/Metadata"].get_object().get_data()
            xmp = {"XMP packet": f"present ({len(raw)} bytes)"}
            xmp.update(_xmp_fields(raw))
            sections.append(Section("XMP metadata", METADATA, xmp))
    except Exception:
        pass
    return sections


# ---- office ---------------------------------------------------------------- #

_OFFICE_PROPS = {
    "docProps/core.xml": "Core properties",
    "docProps/app.xml": "Application properties",
    "docProps/custom.xml": "Custom properties",
}


def _read_office(path: Path) -> list[Section]:
    import xml.etree.ElementTree as ET

    sections = []
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        for part, title in _OFFICE_PROPS.items():
            if part not in names:
                continue
            root = ET.fromstring(z.read(part))
            items = {}
            for el in root.iter():
                if el is root:
                    continue
                local = el.tag.split("}")[-1]
                name = el.attrib.get("name")  # custom props: <property name="...">
                text = (el.text or "").strip()
                if name:
                    child_text = "".join((c.text or "") for c in el).strip()
                    items[name] = _fmt(child_text)
                elif text and len(el) == 0 and local not in ("lpstr", "i4", "variant", "bool"):
                    items[local] = _fmt(text)
            if items:
                sections.append(Section(title, METADATA, items))
        extra = {}
        cmt = [n for n in names if re.search(r"comments\d*\.xml$|commentsExtended", n)]
        if cmt:
            extra["Comment parts"] = ", ".join(sorted(cmt))
        if extra:
            sections.append(Section("Other embedded info", TECHNICAL, extra))
    return sections


# ---- audio / video --------------------------------------------------------- #

_FF_TECH_KEYS = {"major_brand", "minor_version", "compatible_brands", "handler_name",
                 "vendor_id", "language", "duration", "_statistics_tags",
                 "number_of_frames", "number_of_bytes", "bps", "_statistics_writing_app",
                 "_statistics_writing_date_utc", "statistics_tags", "rotate"}


def _read_ffmpeg(path: Path) -> list[Section]:
    """Parse `ffmpeg -i` output: container + per-stream tags and stream info."""
    res = _run([ffmpeg_exe(), "-hide_banner", "-i", str(path)])
    lines = res.stderr.splitlines()

    tech = {}
    meta_sections: list[Section] = []
    current: Section | None = None
    in_meta = False
    meta_indent = 0

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())

        if stripped.startswith("Input #"):
            m = re.match(r"Input #\d+, ([^,]+(?:,[^,]+)*?), from", stripped)
            if m:
                tech["Container"] = m.group(1)
            current = Section("Container tags", METADATA)
            meta_sections.append(current)
            in_meta = False
            continue
        if stripped.startswith("Duration:"):
            for part in stripped.split(","):
                if ":" in part:
                    k, v = part.split(":", 1)
                    tech[k.strip().title()] = v.strip()
            in_meta = False
            continue
        m = re.match(r"Stream #\d+:(\d+)(?:\[\w+\])?(?:\((\w+)\))?: (\w+): (.*)", stripped)
        if m:
            idx, _lang, kind, desc = m.groups()
            tech[f"Stream {idx} ({kind})"] = desc[:200]
            current = Section(f"Stream {idx} tags ({kind.lower()})", METADATA)
            meta_sections.append(current)
            in_meta = False
            continue
        if stripped.startswith("Chapter #"):
            current = Section(f"Chapter {stripped.split()[1]}", METADATA,
                              {"Chapter": stripped})
            meta_sections.append(current)
            in_meta = False
            continue
        if stripped == "Metadata:":
            in_meta, meta_indent = True, indent
            continue
        if in_meta and current is not None:
            if indent <= meta_indent:
                in_meta = False
                continue
            if ":" in stripped:
                k, v = stripped.split(":", 1)
                k = k.strip()
                current.items[k] = _fmt(v.strip())

    sections = [Section("Media properties", TECHNICAL, tech)] if tech else []
    # split technical keys (brands, handler names) away from identifying tags
    for s in meta_sections:
        if not s.items:
            continue
        tech_part = {k: v for k, v in s.items.items() if k.lower() in _FF_TECH_KEYS}
        meta_part = {k: v for k, v in s.items.items() if k.lower() not in _FF_TECH_KEYS}
        if meta_part:
            sections.append(Section(s.name, METADATA, meta_part))
        if tech_part:
            sections.append(Section(s.name.replace("tags", "info"), TECHNICAL, tech_part))
    return sections


def _read_mutagen(path: Path) -> list[Section]:
    import mutagen

    f = mutagen.File(str(path))
    if f is None:
        return []
    tech = {}
    if getattr(f, "info", None):
        for attr in ("length", "bitrate", "sample_rate", "channels", "bits_per_sample"):
            if hasattr(f.info, attr):
                tech[attr.replace("_", " ").title()] = _fmt(getattr(f.info, attr))
    tags = {}
    if f.tags:
        for k, v in f.tags.items():
            tags[str(k)] = _fmt(v)
    out = []
    if tags:
        out.append(Section("Audio tags (ID3 / Vorbis / iTunes)", METADATA, tags))
    if tech:
        out.append(Section("Audio stream", TECHNICAL, tech))
    return out


def _read_hachoir(path: Path) -> list[Section]:
    from hachoir.core import config as hconfig
    from hachoir.metadata import extractMetadata
    from hachoir.parser import createParser

    hconfig.quiet = True
    parser = createParser(str(path))
    if not parser:
        return [Section("Parser", TECHNICAL, {"Note": "No structural parser for this format"})]
    with parser:
        meta = extractMetadata(parser)
    if not meta:
        return []
    items = {}
    for item in meta:
        if item.values:
            items[item.description] = _fmt(", ".join(v.text for v in item.values))
    # Split into identifying vs technical. QuickTime writes placeholder values
    # (1904 epoch dates, player settings) that aren't real metadata.
    def placeholder(v: str) -> bool:
        return v.startswith(("1904-01-01 00:00:00", "Play speed", "1970-01-01 00:00:00"))
    m = {k: v for k, v in items.items() if is_sensitive(k) and not placeholder(v)}
    t = {k: v for k, v in items.items() if k not in m}
    out = []
    if m:
        out.append(Section("Structural metadata (hachoir)", METADATA, m))
    if t:
        out.append(Section("Structural properties (hachoir)", TECHNICAL, t))
    return out


def _read_exiftool(path: Path) -> list[Section]:
    res = _run(["exiftool", "-j", "-G1", "-a", "-s", str(path)])
    data = json.loads(res.stdout)[0]
    skip_groups = ("System:", "ExifTool:", "File:")
    items = {k: _fmt(v) for k, v in data.items()
             if k != "SourceFile" and not k.startswith(skip_groups)}
    return [Section("ExifTool (full dump)", METADATA, items)] if items else []


# --------------------------------------------------------------------------- #
# REMOVE
# --------------------------------------------------------------------------- #

def remove_metadata(src: str | Path, dst: str | Path) -> list[str]:
    """Write a metadata-free copy of `src` to `dst`. Returns human-readable notes."""
    src, dst = Path(src), Path(dst)
    ext = src.suffix.lower()
    if ext in {".jpg", ".jpeg", ".jpe"}:
        return _strip_jpeg(src, dst)
    if ext == ".png":
        return _strip_png(src, dst)
    if ext in IMAGE_EXT:
        return _strip_pillow(src, dst)
    if ext in PDF_EXT:
        return _strip_pdf(src, dst)
    if ext in OFFICE_EXT:
        return _strip_office(src, dst)
    if ext in MUTAGEN_STRIP_EXT:
        return _strip_mutagen(src, dst)
    if ext in VIDEO_EXT or ext in AUDIO_EXT:
        return _strip_ffmpeg(src, dst)
    raise UnsupportedFormat(
        f"Removing metadata from '{ext}' files isn't supported. "
        f"Supported: {', '.join(sorted(SUPPORTED_REMOVAL))}"
    )


# ---- JPEG (lossless) ------------------------------------------------------- #

def _strip_jpeg(src: Path, dst: Path) -> list[str]:
    from PIL import Image, ImageOps

    with Image.open(src) as im:
        orientation = im.getexif().get(0x0112, 1)
        if orientation not in (1, None):
            # Removing EXIF would also drop the rotation flag -> bake rotation in.
            fixed = ImageOps.exif_transpose(im)
            kwargs = {"quality": 95, "optimize": True}
            if "icc_profile" in im.info:
                kwargs["icc_profile"] = im.info["icc_profile"]
            fixed.save(dst, "JPEG", **kwargs)
            return ["Image had an EXIF rotation flag, so it was re-encoded at quality 95 "
                    "with the rotation applied to the pixels (otherwise it would display sideways).",
                    "All EXIF, GPS, XMP, IPTC and comments removed."]

    data = src.read_bytes()
    if data[:2] != b"\xff\xd8":
        raise ValueError("Not a valid JPEG file")
    out = bytearray(b"\xff\xd8")
    removed: dict[str, int] = {}
    i = 2
    n = len(data)
    while i < n:
        if data[i] != 0xFF:
            raise ValueError("Corrupt JPEG marker stream")
        while i < n and data[i] == 0xFF:
            i += 1
        marker = data[i]
        i += 1
        if marker == 0xDA:  # Start of Scan: rest is image data
            out += b"\xff\xda" + data[i:]
            break
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            out += bytes((0xFF, marker))
            continue
        seglen = int.from_bytes(data[i:i + 2], "big")
        seg = data[i:i + seglen]
        i += seglen

        keep = True
        label = None
        if marker == 0xFE:
            keep, label = False, "Comment (COM)"
        elif 0xE0 <= marker <= 0xEF:
            sig = seg[2:16]
            if marker == 0xE0 and sig.startswith(b"JFIF\x00"):
                keep = True                       # basic JFIF header – no personal data
            elif marker == 0xE2 and sig.startswith(b"ICC_PROFILE\x00"):
                keep = True                       # color profile – needed for correct colors
            elif marker == 0xEE and sig.startswith(b"Adobe"):
                keep = True                       # color transform flag – needed for CMYK
            else:
                keep = False
                if sig.startswith(b"Exif"):
                    label = "EXIF (incl. GPS, camera, dates)"
                elif b"adobe.com/xap" in seg[:64]:
                    label = "XMP"
                elif sig.startswith(b"Photoshop"):
                    label = "IPTC / Photoshop"
                else:
                    label = f"APP{marker - 0xE0} segment"
        if keep:
            out += bytes((0xFF, marker)) + seg
        else:
            removed[label] = removed.get(label, 0) + 1

    dst.write_bytes(bytes(out))
    notes = ["Lossless clean: pixel data copied byte-for-byte (no re-compression)."]
    notes += [f"Removed: {k}" + (f" ×{v}" if v > 1 else "") for k, v in removed.items()]
    if not removed:
        notes.append("No metadata segments were present.")
    notes.append("Kept: ICC color profile (if any) so colors display correctly.")
    return notes


# ---- PNG (lossless) -------------------------------------------------------- #

_PNG_KEEP = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"gAMA", b"cHRM", b"sRGB",
             b"iCCP", b"sBIT", b"bKGD", b"pHYs", b"acTL", b"fcTL", b"fdAT", b"cICP",
             b"mDCv", b"cLLi"}


def _strip_png(src: Path, dst: Path) -> list[str]:
    data = src.read_bytes()
    sig = b"\x89PNG\r\n\x1a\n"
    if not data.startswith(sig):
        raise ValueError("Not a valid PNG file")
    out = bytearray(sig)
    removed: dict[str, int] = {}
    i = 8
    while i < len(data):
        length = int.from_bytes(data[i:i + 4], "big")
        ctype = data[i + 4:i + 8]
        chunk = data[i:i + 12 + length]
        i += 12 + length
        if ctype in _PNG_KEEP:
            out += chunk
        else:
            name = ctype.decode("latin-1")
            removed[name] = removed.get(name, 0) + 1
        if ctype == b"IEND":
            break
    dst.write_bytes(bytes(out))
    names = {"tEXt": "text", "zTXt": "compressed text", "iTXt": "international text / XMP",
             "eXIf": "EXIF", "tIME": "last-modified time"}
    notes = ["Lossless clean: image data chunks copied unchanged."]
    notes += [f"Removed chunk {k} ({names.get(k, 'ancillary')})" + (f" ×{v}" if v > 1 else "")
              for k, v in removed.items()]
    if not removed:
        notes.append("No metadata chunks were present.")
    return notes


# ---- other images via Pillow ---------------------------------------------- #

def _strip_pillow(src: Path, dst: Path) -> list[str]:
    from PIL import Image, ImageSequence

    def clean(frame: Image.Image) -> Image.Image:
        new = Image.new(frame.mode, frame.size)
        if frame.mode == "P":
            new.putpalette(frame.getpalette())
        new.paste(frame)
        new.info = {k: frame.info[k] for k in ("transparency",) if k in frame.info}
        return new

    with Image.open(src) as im:
        fmt = im.format
        n = getattr(im, "n_frames", 1)
        save_kwargs = {}
        if fmt == "WEBP":
            save_kwargs["lossless"] = True
        if fmt == "TIFF":
            save_kwargs["compression"] = "tiff_lzw"
        if n > 1:
            frames = [clean(f.copy()) for f in ImageSequence.Iterator(im)]
            durations = []
            for f in ImageSequence.Iterator(im):
                durations.append(f.info.get("duration", 100))
            extra = {"save_all": True, "append_images": frames[1:]}
            if fmt in ("GIF", "WEBP"):
                extra.update(duration=durations, loop=im.info.get("loop", 0))
            frames[0].save(dst, fmt, **extra, **save_kwargs)
        else:
            clean(im).save(dst, fmt, **save_kwargs)

    notes = [f"Rebuilt the {fmt} image from pixel data only — all embedded tags, "
             "comments, EXIF and XMP were dropped."]
    if fmt == "WEBP":
        notes.append("WebP saved in lossless mode, so no quality was lost.")
    if fmt == "TIFF":
        notes.append("TIFF saved with lossless LZW compression.")
    if n > 1:
        notes.append(f"All {n} animation frames preserved.")
    return notes


# ---- PDF ------------------------------------------------------------------- #

def _strip_pdf(src: Path, dst: Path) -> list[str]:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import NameObject

    reader = PdfReader(str(src))
    if reader.is_encrypted:
        if not reader.decrypt(""):
            raise ValueError("This PDF is password-protected; it can't be cleaned without the password.")

    writer = PdfWriter(clone_from=reader)   # keeps pages, links, bookmarks, forms
    notes = []

    if reader.metadata:
        notes.append(f"Removed document info: {', '.join(k.lstrip('/') for k in reader.metadata)}")
    writer.metadata = None                   # drops the /Info dictionary entirely

    root = writer._root_object
    for key in ("/Metadata", "/PieceInfo"):
        if NameObject(key) in root:
            del root[NameObject(key)]
            notes.append(f"Removed document-level {key.lstrip('/')} (XMP)" if key == "/Metadata"
                         else f"Removed {key.lstrip('/')} (app-private data)")

    page_hits = 0
    for page in writer.pages:
        for key in ("/Metadata", "/PieceInfo"):
            if NameObject(key) in page:
                del page[NameObject(key)]
                page_hits += 1
    if page_hits:
        notes.append(f"Removed per-page XMP/PieceInfo entries ({page_hits}).")

    with open(dst, "wb") as fh:
        writer.write(fh)
    if not notes:
        notes.append("No document metadata was present.")
    notes.append("File was fully rewritten (not an incremental update), so old metadata can't be recovered.")
    return notes


# ---- Office (docx/xlsx/pptx) ---------------------------------------------- #

_EMPTY_CORE = (b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
               b'<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
               b'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
               b'xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
               b'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"/>')
_EMPTY_APP = (b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
              b'<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
              b'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"/>')
_EMPTY_CUSTOM = (b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                 b'<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties" '
                 b'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"/>')


def _strip_office(src: Path, dst: Path) -> list[str]:
    replacements = {"docProps/core.xml": _EMPTY_CORE,
                    "docProps/app.xml": _EMPTY_APP,
                    "docProps/custom.xml": _EMPTY_CUSTOM}
    notes = []
    fixed_date = (1980, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = replacements.get(info.filename, None)
            if data is not None:
                notes.append(f"Cleared {_OFFICE_PROPS[info.filename].lower()} ({info.filename})")
            else:
                data = zin.read(info.filename)
            zi = zipfile.ZipInfo(info.filename, date_time=fixed_date)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zout.writestr(zi, data)
    notes.append("Reset internal file timestamps inside the package.")
    notes.append("Note: reviewer names in comments / tracked changes live in the document body "
                 "and are not touched — use Office's 'Inspect Document' for those.")
    return notes


# ---- audio via mutagen ----------------------------------------------------- #

def _strip_mutagen(src: Path, dst: Path) -> list[str]:
    import mutagen

    shutil.copyfile(src, dst)
    f = mutagen.File(str(dst))
    if f is None:
        return _strip_ffmpeg(src, dst)
    count = len(f.tags) if f.tags else 0
    f.delete()
    if dst.suffix.lower() == ".mp3":
        try:
            from mutagen.id3 import delete as id3_delete
            id3_delete(str(dst), delete_v1=True, delete_v2=True)
        except Exception:
            pass
    if dst.suffix.lower() == ".flac":
        from mutagen.flac import FLAC
        fl = FLAC(str(dst))
        if fl.pictures:
            fl.clear_pictures()
            fl.save()
    return [f"Removed {count} audio tag(s) (title, artist, album, comments, cover art...).",
            "Audio stream untouched — no re-encoding."]


# ---- audio / video via ffmpeg --------------------------------------------- #

def _strip_ffmpeg(src: Path, dst: Path) -> list[str]:
    base = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(src)]
    tail = ["-map_metadata", "-1", "-map_chapters", "-1", "-c", "copy",
            "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact"]
    ext = dst.suffix.lower()
    if ext in {".mp4", ".mov", ".m4v", ".m4a", ".3gp"}:
        tail += ["-movflags", "+faststart"]
    if ext == ".mp3":
        tail += ["-id3v2_version", "0", "-write_id3v1", "0"]

    attempts = [
        ["-map", "0:v?", "-map", "0:a?", "-map", "0:s?"],   # video, audio, subtitles
        ["-map", "0:v?", "-map", "0:a?"],                   # drop subs if container rejects them
    ]
    err = ""
    for maps in attempts:
        res = _run(base + maps + tail + [str(dst)])
        if res.returncode == 0 and dst.exists() and dst.stat().st_size > 0:
            notes = ["Remuxed with stream copy — video/audio data is untouched (no quality loss).",
                     "Removed all container and per-stream tags (creation date, GPS location, "
                     "device make/model, encoder, title, comments...).",
                     "Removed chapters and data/timecode tracks."]
            if maps is attempts[1]:
                notes.append("Subtitle tracks were dropped (not compatible with this container).")
            return notes
        err = res.stderr.strip()
    raise RuntimeError(f"ffmpeg failed: {err[-500:]}")
