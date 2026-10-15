"""
metadata_edit.py
----------------
EDIT (write) metadata for supported formats.

  get_editable(path)            -> EditSpec with the current values of editable fields
  write_metadata(src, dst, d)   -> writes an edited copy; returns (notes, diff)

`d` maps field name -> desired value. An empty value deletes the field;
a field that existed but is missing from `d` is also deleted.

  JPEG  : EXIF + GPS, rewritten LOSSLESSLY (only the EXIF segment is replaced)
  PNG   : text chunks (Title, Author, ...), lossless; custom keys allowed
  TIFF/WebP : EXIF + GPS via Pillow (lossless re-save)
  PDF   : document info (Title, Author, dates...); custom keys allowed
  Office: core/app properties (Author, Company, Last Modified By...)
  Audio/Video: container tags via ffmpeg stream copy; custom keys allowed
"""

from __future__ import annotations

import datetime as dt
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import metadata_core as mc


@dataclass
class EditSpec:
    fields: dict                                  # field -> current value ("" = not set)
    hints: dict = field(default_factory=dict)     # field -> format hint
    allow_custom: bool = False                    # can the user add new field names?
    note: str = ""


JPEG_EXT = {".jpg", ".jpeg", ".jpe"}
EXIF_RESAVE_EXT = {".tif", ".tiff", ".webp"}
EDITABLE_EXT = (JPEG_EXT | EXIF_RESAVE_EXT | {".png"} | mc.PDF_EXT | mc.OFFICE_EXT
                | mc.VIDEO_EXT | mc.AUDIO_EXT)


def get_editable(path: str | Path) -> EditSpec:
    path = Path(path)
    ext = path.suffix.lower()
    if ext in JPEG_EXT or ext in EXIF_RESAVE_EXT:
        return _exif_spec(path)
    if ext == ".png":
        return _png_spec(path)
    if ext in mc.PDF_EXT:
        return _pdf_spec(path)
    if ext in mc.OFFICE_EXT:
        return _office_spec(path)
    if ext in mc.VIDEO_EXT or ext in mc.AUDIO_EXT:
        return _av_spec(path)
    raise mc.UnsupportedFormat(
        f"Editing metadata of '{ext}' files isn't supported. "
        f"Supported: {', '.join(sorted(EDITABLE_EXT))}")


def write_metadata(src: str | Path, dst: str | Path, desired: dict) -> tuple[list[str], list[tuple]]:
    src, dst = Path(src), Path(dst)
    ext = src.suffix.lower()
    before = get_editable(src).fields
    desired = {k.strip(): ("" if v is None else str(v).strip()) for k, v in desired.items() if k and k.strip()}
    # fields removed from the table entirely count as deletions
    for k in before:
        desired.setdefault(k, "")

    if ext in JPEG_EXT or ext in EXIF_RESAVE_EXT:
        notes = _write_exif(src, dst, desired)
    elif ext == ".png":
        notes = _write_png(src, dst, desired)
    elif ext in mc.PDF_EXT:
        notes = _write_pdf(src, dst, desired)
    elif ext in mc.OFFICE_EXT:
        notes = _write_office(src, dst, desired)
    else:
        notes = _write_av(src, dst, desired, before)

    after = get_editable(dst).fields
    diff = []
    for k in dict.fromkeys(list(before) + list(after)):
        b, a = before.get(k, ""), after.get(k, "")
        if b != a:
            diff.append((k, b or "—", a or "—"))
    return notes, diff


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #

def _parse_datetime(value: str, field_name: str) -> dt.datetime:
    v = value.strip().replace("T", " ").rstrip("Z").strip()
    v = re.sub(r"^(\d{4}):(\d{2}):(\d{2})", r"\1-\2-\3", v)   # EXIF style YYYY:MM:DD
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(v, fmt)
        except ValueError:
            pass
    raise ValueError(f"'{field_name}': couldn't read date '{value}'. Use YYYY-MM-DD HH:MM:SS.")


def _parse_coord(value: str, field_name: str, limit: float) -> float:
    try:
        f = float(value)
    except ValueError:
        raise ValueError(f"'{field_name}' must be a decimal number, e.g. 32.834722") from None
    if not -limit <= f <= limit:
        raise ValueError(f"'{field_name}' must be between -{limit} and {limit}.")
    return f


# --------------------------------------------------------------------------- #
# EXIF (JPEG / TIFF / WebP)
# --------------------------------------------------------------------------- #

EXIF_IFD, GPS_IFD = 0x8769, 0x8825
GPS_LAT, GPS_LON = "GPS Latitude", "GPS Longitude"
DATE_HINT = "YYYY-MM-DD HH:MM:SS"

# (label, ifd (None = main), tag, hint)
_EXIF_FIELDS = [
    ("Artist", None, 0x013B, "Author / photographer"),
    ("Copyright", None, 0x8298, "e.g. © 2026 Your Name"),
    ("ImageDescription", None, 0x010E, "Caption / title"),
    ("UserComment", EXIF_IFD, 0x9286, "Free-text comment"),
    ("Make", None, 0x010F, "Camera maker, e.g. Apple"),
    ("Model", None, 0x0110, "Camera model, e.g. iPhone 15 Pro"),
    ("Software", None, 0x0131, "Software used"),
    ("DateTime", None, 0x0132, f"{DATE_HINT} (last modified)"),
    ("DateTimeOriginal", EXIF_IFD, 0x9003, f"{DATE_HINT} (when taken)"),
    ("DateTimeDigitized", EXIF_IFD, 0x9004, DATE_HINT),
    ("LensModel", EXIF_IFD, 0xA434, "Lens name"),
    ("BodySerialNumber", EXIF_IFD, 0xA431, "Camera serial number"),
]
_DATE_TAGS = {0x0132, 0x9003, 0x9004}


def _decode_user_comment(v) -> str:
    if isinstance(v, str):
        return v.strip("\x00 ")
    if not isinstance(v, (bytes, bytearray)):
        return str(v)
    head, body = bytes(v[:8]), bytes(v[8:])
    if head.startswith(b"UNICODE"):
        enc = "utf-16-be" if body[:1] == b"\x00" else "utf-16-le"
        return body.decode(enc, "ignore").strip("\x00 ")
    if head.startswith((b"ASCII", b"\x00" * 8)):
        return body.decode("latin-1", "ignore").strip("\x00 ")
    return bytes(v).decode("utf-8", "ignore").strip("\x00 ")


def _encode_user_comment(s: str) -> bytes:
    try:
        return b"ASCII\x00\x00\x00" + s.encode("ascii")
    except UnicodeEncodeError:
        return b"UNICODE\x00" + s.encode("utf-16-be")


def _exif_spec(path: Path) -> EditSpec:
    from PIL import Image

    with Image.open(path) as im:
        exif = im.getexif()
        sub = exif.get_ifd(EXIF_IFD) if EXIF_IFD in exif else {}
        gps = exif.get_ifd(GPS_IFD) if GPS_IFD in exif else {}
    fields, hints = {}, {}
    for label, ifd, tag, hint in _EXIF_FIELDS:
        src = sub if ifd == EXIF_IFD else exif
        val = src.get(tag, "")
        if tag == 0x9286:
            val = _decode_user_comment(val) if val else ""
        fields[label] = mc._fix_utf8(mc._fmt(val)) if val != "" else ""
        hints[label] = hint
    coords = mc._gps_to_decimal(gps) if gps else None
    fields[GPS_LAT] = f"{coords[0]:.6f}" if coords else ""
    fields[GPS_LON] = f"{coords[1]:.6f}" if coords else ""
    hints[GPS_LAT] = "Decimal degrees, + north / − south (e.g. 32.834722)"
    hints[GPS_LON] = "Decimal degrees, + east / − west (e.g. -97.081944)"
    note = ("JPEG edits are lossless — only the EXIF block is rewritten."
            if path.suffix.lower() in JPEG_EXT else
            "The image is re-saved losslessly with the new EXIF.")
    return EditSpec(fields, hints, False, note + " Clear both GPS fields to remove location.")


def _to_dms(value: float):
    from PIL.TiffImagePlugin import IFDRational
    value = abs(value)
    d = int(value)
    m_float = (value - d) * 60
    m = int(m_float)
    s = round((m_float - m) * 60 * 10000)
    return (IFDRational(d, 1), IFDRational(m, 1), IFDRational(s, 10000))


def _apply_exif_changes(exif, desired: dict) -> list[str]:
    """Mutate a PIL Exif object according to `desired`. Returns notes."""
    notes = []
    sub = dict(exif.get_ifd(EXIF_IFD)) if EXIF_IFD in exif else {}
    gps = dict(exif.get_ifd(GPS_IFD)) if GPS_IFD in exif else {}

    for label, ifd, tag, _ in _EXIF_FIELDS:
        if label not in desired:
            continue
        val = desired[label]
        target = sub if ifd == EXIF_IFD else exif
        if not val:
            if tag in target:
                del target[tag]
            continue
        if tag in _DATE_TAGS:
            val = _parse_datetime(val, label).strftime("%Y:%m:%d %H:%M:%S")
        if tag == 0x9286:
            val = _encode_user_comment(val)
        elif not val.isascii():
            val = val.encode("utf-8")   # EXIF text is ASCII; UTF-8 bytes keep ©, accents, etc.
        target[tag] = val

    lat_s, lon_s = desired.get(GPS_LAT, ""), desired.get(GPS_LON, "")
    if lat_s or lon_s:
        if not (lat_s and lon_s):
            raise ValueError("Enter both GPS Latitude and GPS Longitude (or clear both to remove GPS).")
        lat = _parse_coord(lat_s, GPS_LAT, 90)
        lon = _parse_coord(lon_s, GPS_LON, 180)
        old = mc._gps_to_decimal(gps) if gps else None
        if not old or abs(old[0] - lat) > 5e-7 or abs(old[1] - lon) > 5e-7:
            gps = {0: b"\x02\x03\x00\x00",
                   1: "N" if lat >= 0 else "S", 2: _to_dms(lat),
                   3: "E" if lon >= 0 else "W", 4: _to_dms(lon)}
            notes.append(f"GPS location set to {lat:.6f}, {lon:.6f}.")
    elif gps:
        gps = {}
        notes.append("GPS location removed.")

    for tag, ifd in ((EXIF_IFD, sub), (GPS_IFD, gps)):
        if ifd:
            exif[tag] = ifd
        elif tag in exif:
            del exif[tag]
    return notes


def _split_jpeg(data: bytes):
    """Return ([(marker, segment_bytes_incl_length)], bytes_from_SOS_on)."""
    if data[:2] != b"\xff\xd8":
        raise ValueError("Not a valid JPEG file")
    segs, i, n = [], 2, len(data)
    while i < n:
        if data[i] != 0xFF:
            raise ValueError("Corrupt JPEG marker stream")
        while i < n and data[i] == 0xFF:
            i += 1
        marker = data[i]
        if marker == 0xDA:
            return segs, data[i - 1:]
        i += 1
        if marker in (0x01,) or 0xD0 <= marker <= 0xD7:
            segs.append((marker, b""))
            continue
        seglen = int.from_bytes(data[i:i + 2], "big")
        segs.append((marker, data[i:i + seglen]))
        i += seglen
    raise ValueError("JPEG has no image data")


def _write_exif(src: Path, dst: Path, desired: dict) -> list[str]:
    from PIL import Image

    ext = src.suffix.lower()
    with Image.open(src) as im:
        exif = im.getexif()
        if ext in EXIF_RESAVE_EXT:
            # keep only descriptive main-IFD tags; Pillow writes the layout tags itself
            for tag in list(exif):
                if tag in mc._TIFF_STRUCTURAL:
                    del exif[tag]
        notes = _apply_exif_changes(exif, desired)

        if ext in JPEG_EXT:
            exif_bytes = exif.tobytes()
            if not exif_bytes.startswith(b"Exif\x00\x00"):
                exif_bytes = b"Exif\x00\x00" + exif_bytes
            segs, rest = _split_jpeg(src.read_bytes())
            new_seg = b""
            if len(exif) > 0:
                if len(exif_bytes) + 2 > 0xFFFF:
                    raise ValueError("EXIF data is too large for a JPEG segment (64 KB).")
                new_seg = b"\xff\xe1" + (len(exif_bytes) + 2).to_bytes(2, "big") + exif_bytes
            out, inserted = bytearray(b"\xff\xd8"), False
            for marker, seg in segs:
                if marker == 0xE1 and seg[2:8] == b"Exif\x00\x00":
                    continue                                    # old EXIF block
                if not inserted and marker != 0xE0:
                    out += new_seg
                    inserted = True
                out += bytes((0xFF, marker)) + seg
            if not inserted:
                out += new_seg
            out += rest
            dst.write_bytes(bytes(out))
            notes.insert(0, "Lossless edit: only the EXIF block was rewritten; image data is untouched.")
            return notes

        if getattr(im, "n_frames", 1) > 1:
            raise mc.UnsupportedFormat("Editing animated / multi-page images isn't supported.")
        kwargs = {"exif": exif}
        if "icc_profile" in im.info:
            kwargs["icc_profile"] = im.info["icc_profile"]
        if im.format == "WEBP":
            kwargs["lossless"] = True
            notes.insert(0, "WebP re-saved in lossless mode with the new EXIF.")
        else:
            kwargs["compression"] = "tiff_lzw"
            notes.insert(0, "TIFF re-saved with lossless LZW compression and the new EXIF.")
        im.save(dst, im.format, **kwargs)
    return notes


# --------------------------------------------------------------------------- #
# PNG text chunks
# --------------------------------------------------------------------------- #

_PNG_STD = {
    "Title": "Short title", "Author": "Creator's name", "Description": "Longer description",
    "Copyright": "Copyright notice", "Creation Time": "e.g. 2026-10-15 17:00",
    "Software": "Software used", "Comment": "Free text", "Source": "Device used",
}
_PNG_XMP = "XML:com.adobe.xmp"


def _png_spec(path: Path) -> EditSpec:
    from PIL import Image

    with Image.open(path) as im:
        im.load()
        text = {k: v for k, v in getattr(im, "text", {}).items() if k != _PNG_XMP}
    fields = {k: "" for k in _PNG_STD}
    fields.update({k: str(v) for k, v in text.items()})
    return EditSpec(fields, dict(_PNG_STD), True,
                    "PNG text chunks — lossless edit. You can add your own keywords as new rows.")


def _png_chunk(ctype: bytes, data: bytes) -> bytes:
    return len(data).to_bytes(4, "big") + ctype + data + zlib.crc32(ctype + data).to_bytes(4, "big")


def _write_png(src: Path, dst: Path, desired: dict) -> list[str]:
    data = src.read_bytes()
    sig = b"\x89PNG\r\n\x1a\n"
    if not data.startswith(sig):
        raise ValueError("Not a valid PNG file")

    new_chunks = []
    for key, val in desired.items():
        if not val:
            continue
        try:
            kbytes = key.encode("latin-1")
        except UnicodeEncodeError:
            raise ValueError(f"PNG keyword '{key}' must use basic Latin characters.") from None
        if not 1 <= len(kbytes) <= 79:
            raise ValueError(f"PNG keyword '{key}' must be 1–79 characters.")
        try:
            new_chunks.append(_png_chunk(b"tEXt", kbytes + b"\x00" + val.encode("latin-1")))
        except UnicodeEncodeError:
            new_chunks.append(_png_chunk(b"iTXt", kbytes + b"\x00\x00\x00\x00\x00" + val.encode("utf-8")))

    out, i, inserted = bytearray(sig), 8, False
    while i < len(data):
        length = int.from_bytes(data[i:i + 4], "big")
        ctype = data[i + 4:i + 8]
        chunk = data[i:i + 12 + length]
        i += 12 + length
        if ctype in (b"tEXt", b"zTXt", b"iTXt"):
            if chunk[8:8 + len(_PNG_XMP) + 1] != _PNG_XMP.encode() + b"\x00":
                continue                                     # old text chunk – replaced
        if ctype == b"IDAT" and not inserted:
            out += b"".join(new_chunks)
            inserted = True
        out += chunk
        if ctype == b"IEND":
            break
    dst.write_bytes(bytes(out))
    return ["Lossless edit: text chunks rewritten; image data untouched.",
            f"{len(new_chunks)} text field(s) written."]


# --------------------------------------------------------------------------- #
# PDF document info
# --------------------------------------------------------------------------- #

_PDF_STD = {
    "Title": "Document title", "Author": "Author name", "Subject": "Subject",
    "Keywords": "Comma-separated keywords", "Creator": "App that made the original",
    "Producer": "PDF library / converter", "CreationDate": DATE_HINT, "ModDate": DATE_HINT,
}
_PDF_DATES = {"CreationDate", "ModDate"}


def _pdf_date_to_text(v: str) -> str:
    m = re.match(r"D?:?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?", str(v))
    if not m:
        return str(v)
    y, mo, d, h, mi, s = (g or "00" for g in m.groups())
    mo, d = (mo if mo != "00" else "01"), (d if d != "00" else "01")
    return f"{y}-{mo}-{d} {h}:{mi}:{s}"


def _pdf_spec(path: Path) -> EditSpec:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("This PDF is password-protected.")
    fields = {k: "" for k in _PDF_STD}
    for k, v in (reader.metadata or {}).items():
        key = str(k).lstrip("/")
        fields[key] = _pdf_date_to_text(v) if key in _PDF_DATES else str(v)
    return EditSpec(fields, dict(_PDF_STD), True,
                    "You can add custom properties as new rows (letters, digits, _ and - only).")


def _write_pdf(src: Path, dst: Path, desired: dict) -> list[str]:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import NameObject

    reader = PdfReader(str(src))
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("This PDF is password-protected; it can't be edited without the password.")

    info = {}
    for key, val in desired.items():
        if not val:
            continue
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", key):
            raise ValueError(f"PDF property name '{key}' may only contain letters, digits, _ and -.")
        if key in _PDF_DATES:
            val = _parse_datetime(val, key).strftime("D:%Y%m%d%H%M%S")
        info[f"/{key}"] = val

    writer = PdfWriter(clone_from=reader)
    writer.metadata = None
    if info:
        writer.add_metadata(info)
    notes = [f"Document info updated ({len(info)} field(s) set)."]
    root = writer._root_object
    if NameObject("/Metadata") in root:
        del root[NameObject("/Metadata")]
        notes.append("Old XMP metadata removed so PDF viewers don't show stale values.")
    with open(dst, "wb") as fh:
        writer.write(fh)
    return notes


# --------------------------------------------------------------------------- #
# Office (docx / xlsx / pptx)
# --------------------------------------------------------------------------- #

NS = {
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
    "dcmitype": "http://purl.org/dc/dcmitype/",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}
EP = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
VT = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
for _p, _u in NS.items():
    ET.register_namespace(_p, _u)
ET.register_namespace("vt", VT)

ISO_HINT = "YYYY-MM-DD HH:MM:SS (UTC)"
_CORE_FIELDS = [  # label, ns, tag, hint
    ("Title", "dc", "title", ""), ("Subject", "dc", "subject", ""),
    ("Author", "dc", "creator", "Original author"), ("Keywords", "cp", "keywords", "Tags"),
    ("Comments", "dc", "description", ""), ("Last Modified By", "cp", "lastModifiedBy", ""),
    ("Category", "cp", "category", ""), ("Created", "dcterms", "created", ISO_HINT),
    ("Modified", "dcterms", "modified", ISO_HINT),
]
_APP_FIELDS = [("Company", "Company", ""), ("Manager", "Manager", "")]


def _office_spec(path: Path) -> EditSpec:
    fields, hints = {}, {}
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        core = ET.fromstring(z.read("docProps/core.xml")) if "docProps/core.xml" in names else None
        app = ET.fromstring(z.read("docProps/app.xml")) if "docProps/app.xml" in names else None
    for label, ns, tag, hint in _CORE_FIELDS:
        el = core.find(f"{{{NS[ns]}}}{tag}") if core is not None else None
        val = (el.text or "").strip() if el is not None else ""
        if tag in ("created", "modified") and val:
            val = val.replace("T", " ").rstrip("Z")
        fields[label], hints[label] = val, hint
    for label, tag, hint in _APP_FIELDS:
        el = app.find(f"{{{EP}}}{tag}") if app is not None else None
        fields[label] = (el.text or "").strip() if el is not None else ""
        hints[label] = hint
    return EditSpec(fields, hints, False, "Shown in File → Info / Properties in Office.")


def _write_office(src: Path, dst: Path, desired: dict) -> list[str]:
    with zipfile.ZipFile(src) as zin:
        names = set(zin.namelist())
        if "docProps/core.xml" not in names:
            raise ValueError("This document has no properties part (docProps/core.xml) to edit.")
        core = ET.fromstring(zin.read("docProps/core.xml"))
        app = ET.fromstring(zin.read("docProps/app.xml")) if "docProps/app.xml" in names else None

        changed = 0
        for label, ns, tag, _ in _CORE_FIELDS:
            if label not in desired:
                continue
            val = desired[label]
            qname = f"{{{NS[ns]}}}{tag}"
            el = core.find(qname)
            if not val:
                if el is not None:
                    core.remove(el)
                    changed += 1
                continue
            if tag in ("created", "modified"):
                val = _parse_datetime(val, label).strftime("%Y-%m-%dT%H:%M:%SZ")
            if el is None:
                el = ET.SubElement(core, qname)
                if tag in ("created", "modified"):
                    el.set(f"{{{NS['xsi']}}}type", "dcterms:W3CDTF")
            if el.text != val:
                el.text = val
                changed += 1

        new_parts = {"docProps/core.xml": ET.tostring(core, encoding="UTF-8", xml_declaration=True)}

        # app.xml is edited as text so its default-namespace layout stays exactly as Office wrote it
        if app is not None:
            from xml.sax.saxutils import escape
            xml = zin.read("docProps/app.xml").decode("utf-8")
            for label, tag, _ in _APP_FIELDS:
                if label not in desired:
                    continue
                val = desired[label]
                current = app.find(f"{{{EP}}}{tag}")
                cur_text = (current.text or "").strip() if current is not None else ""
                if val == cur_text and (current is not None or not val):
                    continue
                pattern = rf"<{tag}\s*/>|<{tag}>.*?</{tag}>"
                repl = f"<{tag}>{escape(val)}</{tag}>" if val else ""
                if re.search(pattern, xml, flags=re.S):
                    xml = re.sub(pattern, lambda _m: repl, xml, count=1, flags=re.S)
                elif val:
                    xml = xml.replace("</Properties>", repl + "</Properties>")
                changed += 1
            new_parts["docProps/app.xml"] = xml.encode("utf-8")
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                zout.writestr(info, new_parts.get(info.filename, None) or zin.read(info.filename))
    return [f"Document properties updated ({changed} change(s)). Document content untouched."]


# --------------------------------------------------------------------------- #
# Audio / video (ffmpeg)
# --------------------------------------------------------------------------- #

_AV_STD = {
    "title": "", "artist": "", "album": "", "album_artist": "", "genre": "",
    "date": "YYYY or YYYY-MM-DD", "comment": "", "description": "", "copyright": "",
    "composer": "", "track": "e.g. 3 or 3/12",
}
_VIDEO_EXTRA = {
    "creation_time": "YYYY-MM-DD HH:MM:SS (UTC)",
    "location": "ISO 6709, e.g. +32.8343-097.0819/ (MP4/MOV)",
}
_AV_SKIP = {"encoder"}  # ffmpeg rewrites this itself


def _av_std(path: Path) -> dict:
    std = dict(_AV_STD)
    if path.suffix.lower() in mc.VIDEO_EXT:
        std.update(_VIDEO_EXTRA)
    return std


def _av_spec(path: Path) -> EditSpec:
    std = _av_std(path)
    fields = {k: "" for k in std}
    for s in mc._read_ffmpeg(path):
        if s.name == "Container tags":
            for k, v in s.items.items():
                if k.lower() not in _AV_SKIP:
                    fields[k] = v
    if fields.get("creation_time"):
        fields["creation_time"] = re.sub(r"\.\d+Z?$", "", fields["creation_time"].replace("T", " ")).rstrip("Z")
    return EditSpec(fields, std, True,
                    "Video/audio data is copied as-is (no re-encoding). You can add custom tags as new rows.")


def _write_av(src: Path, dst: Path, desired: dict, before: dict) -> list[str]:
    std = _av_std(src)
    meta_args, custom = [], False
    # Deleting a tag also deletes its language-tagged twins (e.g. location -> location-eng)
    for key, val in list(desired.items()):
        if not val:
            for other in before:
                if re.fullmatch(rf"{re.escape(key)}-[a-z]{{3}}", other):
                    desired[other] = ""
    for key, val in desired.items():
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", key):
            raise ValueError(f"Tag name '{key}' may only contain letters, digits, _ and -.")
        if val == before.get(key, "") and val:
            continue                                   # unchanged – kept by -map_metadata 0
        if key == "creation_time" and val:
            val = _parse_datetime(val, key).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
        meta_args += ["-metadata", f"{key}={val}"]
        if key not in std and val:
            custom = True

    ext = dst.suffix.lower()
    base = [mc.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(src)]
    tail = ["-map_metadata", "0", "-c", "copy"] + meta_args
    if ext in {".mp4", ".mov", ".m4v", ".m4a", ".3gp"} and custom:
        tail += ["-movflags", "use_metadata_tags"]
    if ext == ".mp3":
        tail += ["-id3v2_version", "3"]

    attempts = [["-map", "0"],
                ["-map", "0:v?", "-map", "0:a?", "-map", "0:s?"],
                ["-map", "0:v?", "-map", "0:a?"]]
    err = ""
    for maps in attempts:
        res = mc._run(base + maps + tail + [str(dst)])
        if res.returncode == 0 and dst.exists() and dst.stat().st_size > 0:
            notes = ["Tags rewritten with stream copy — audio/video data untouched (no quality loss)."]
            if maps is not attempts[0]:
                notes.append("Some non-media tracks (data/timecode) couldn't be kept in this container.")
            if custom and ext in {".mp4", ".mov", ".m4v", ".m4a"}:
                notes.append("Custom tags stored as QuickTime metadata keys (shown by most players/ffprobe).")
            return notes
        err = res.stderr.strip()
    raise RuntimeError(f"ffmpeg failed: {err[-500:]}")
