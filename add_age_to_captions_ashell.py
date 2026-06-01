#!/usr/bin/env python3
"""
add_age_to_captions_ashell.py
=============================

Pure-Python version for iOS a-Shell (no ExifTool / Perl needed).

Walk a directory tree of personal photos (organized into year-named
subdirectories), read each JPEG's capture date from its EXIF data,
compute your age on that date, prepend the age to the photo's caption
(EXIF ImageDescription), and rename the file so the age is in the name:

    Me/2024/IMG_1234.jpg  ->  Me/2024/IMG_1234_(1).jpg
    (and its caption becomes:  "Age 1 - <original caption>")

------------------------------------------------------------------------
IMPORTANT LIMITATIONS (because this avoids ExifTool)
------------------------------------------------------------------------
This uses the pure-Python `piexif` library, which only supports JPEG and
TIFF. The following are SKIPPED (you'll see a SKIP line for each):
    * HEIC / HEIF  (the default iPhone photo format!)
    * PNG
    * All videos (.mp4, .mov, ...)
If you need those too, run the ExifTool version under the iSH app instead.

------------------------------------------------------------------------
Setup in a-Shell
------------------------------------------------------------------------
    pip install piexif

------------------------------------------------------------------------
Usage
------------------------------------------------------------------------
    # Preview only, change nothing (recommended first run):
    python add_age_to_captions_ashell.py Me --dry-run

    # Apply for real:
    python add_age_to_captions_ashell.py Me

    # Override date of birth if needed:
    python add_age_to_captions_ashell.py Me --dob 2023-09-01

The script is idempotent: files already ending in "_(age)" are skipped,
so it's safe to re-run.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

try:
    import piexif
except ImportError:
    sys.exit("ERROR: piexif not installed. In a-Shell run:  pip install piexif")


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

DEFAULT_DOB = date(2023, 9, 1)

# Formats piexif can actually read/write.
SUPPORTED_EXTS = {".jpg", ".jpeg", ".tif", ".tiff"}

# Formats we explicitly recognize as media but cannot handle here.
UNSUPPORTED_MEDIA_EXTS = {
    ".heic", ".heif", ".png", ".webp",
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp",
}

# Filename stems already ending in an age suffix like "_(2)".
AGE_SUFFIX_RE = re.compile(r"_\(\d+\)$")


# --------------------------------------------------------------------------
# Age helpers
# --------------------------------------------------------------------------

def age_on(dob: date, on: date) -> int:
    """Age in whole completed years on a given date."""
    years = on.year - dob.year
    if (on.month, on.day) < (dob.month, dob.day):
        years -= 1
    return years


# --------------------------------------------------------------------------
# EXIF helpers (piexif)
# --------------------------------------------------------------------------

def _to_text(value) -> str:
    """EXIF string values come back as bytes; decode them leniently."""
    if isinstance(value, bytes):
        return value.split(b"\x00")[0].decode("utf-8", "replace").strip()
    return str(value).strip()


def get_capture_date(exif: dict, path: Path) -> tuple[date | None, str]:
    """First usable capture date from EXIF, falling back to file mtime."""
    candidates = [
        ("Exif", piexif.ExifIFD.DateTimeOriginal, "DateTimeOriginal"),
        ("Exif", piexif.ExifIFD.DateTimeDigitized, "DateTimeDigitized"),
        ("0th", piexif.ImageIFD.DateTime, "DateTime"),
    ]
    for ifd, tag, name in candidates:
        raw = exif.get(ifd, {}).get(tag)
        if not raw:
            continue
        text = _to_text(raw)
        if text.startswith("0000"):
            continue
        try:
            return datetime.strptime(text[:10], "%Y:%m:%d").date(), name
        except ValueError:
            continue
    # Last resort: filesystem modification time.
    return date.fromtimestamp(os.path.getmtime(path)), "FileModifyDate"


def get_existing_caption(exif: dict) -> str:
    raw = exif.get("0th", {}).get(piexif.ImageIFD.ImageDescription)
    return _to_text(raw) if raw else ""


# --------------------------------------------------------------------------
# Main processing
# --------------------------------------------------------------------------

def process_file(path: Path, dob: date, dry_run: bool) -> str:
    """Process a single file. Returns a one-line status string."""
    ext = path.suffix.lower()

    if AGE_SUFFIX_RE.search(path.stem):
        return f"skip  (already tagged): {path.name}"

    if ext in UNSUPPORTED_MEDIA_EXTS:
        return f"SKIP  (format not supported by piexif): {path.name}"
    if ext not in SUPPORTED_EXTS:
        return f"skip  (not a media file): {path.name}"

    try:
        exif = piexif.load(str(path))
    except Exception as e:  # noqa: BLE001 - report and move on
        return f"SKIP  (could not read EXIF: {e}): {path.name}"

    capture_date, date_src = get_capture_date(exif, path)
    years = age_on(dob, capture_date)
    if years < 0:
        return f"SKIP  (dated before DOB: {capture_date}): {path.name}"
    token = str(years)

    existing = get_existing_caption(exif)
    new_caption = f"Age {token}" + (f" - {existing}" if existing else "")

    new_path = path.with_name(f"{path.stem}_({token}){path.suffix}")
    if new_path.exists() and new_path != path:
        return f"SKIP  (target exists): {new_path.name}"

    if dry_run:
        return (
            f"would tag [{capture_date} via {date_src}] -> {token}\n"
            f"          caption: {new_caption!r}\n"
            f"          rename : {path.name} -> {new_path.name}"
        )

    # Write the caption back into the EXIF, then rename the file.
    exif.setdefault("0th", {})
    exif["0th"][piexif.ImageIFD.ImageDescription] = new_caption.encode("utf-8")
    try:
        piexif.insert(piexif.dump(exif), str(path))
    except Exception as e:  # noqa: BLE001
        return f"SKIP  (could not write EXIF: {e}): {path.name}"

    path.rename(new_path)
    return f"done  [{capture_date}] {token}: {path.name} -> {new_path.name}"


def iter_media(root: Path):
    for p in sorted(root.rglob("*")):
        if p.is_file():
            yield p


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("root", help="Top directory to scan (e.g. 'Me')")
    parser.add_argument(
        "--dob",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
        default=DEFAULT_DOB,
        help="Date of birth as YYYY-MM-DD (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would change without modifying anything",
    )
    args = parser.parse_args()

    root = Path(args.root).expanduser()
    if not root.is_dir():
        sys.exit(f"ERROR: '{root}' is not a directory.")

    print(f"Scanning {root}  (DOB: {args.dob}, dry-run: {args.dry_run})\n")

    handled = 0
    for path in iter_media(root):
        line = process_file(path, args.dob, args.dry_run)
        # Only count/print real media files, keep the noise down.
        if line.startswith("skip  (not a media file)"):
            continue
        handled += 1
        print(line)

    print(f"\n{'Previewed' if args.dry_run else 'Processed'} {handled} media file(s).")


if __name__ == "__main__":
    main()
