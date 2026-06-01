#!/usr/bin/env python3
"""
add_age_to_captions.py
======================

Walk a directory tree of personal photos/videos (organized into
year-named subdirectories), read each file's capture date from its
embedded metadata, compute your age on that date, prepend the age to the
file's caption, and rename the file so the age is part of the filename:

    Me/2024/IMG_1234.jpg  ->  Me/2024/IMG_1234_(1).jpg
    (and its caption becomes:  "Age 1 — <original caption>")

Both images and videos are supported.

------------------------------------------------------------------------
Requirements
------------------------------------------------------------------------
ExifTool must be installed and on your PATH (it handles dates + captions
for both photos AND videos, which is why we use it):

    macOS:    brew install exiftool
    Debian:   sudo apt-get install libimage-exiftool-perl
    Windows:  download the .exe from https://exiftool.org and add to PATH

No third-party Python packages are needed — only the standard library.

------------------------------------------------------------------------
Usage
------------------------------------------------------------------------
    # Preview what would happen, change nothing (recommended first run):
    python add_age_to_captions.py Me --dry-run

    # Actually update captions + rename files:
    python add_age_to_captions.py Me

    # Override the date of birth if needed (default is below):
    python add_age_to_captions.py Me --dob 2023-09-01

The script is idempotent: files that already carry an "_(age)" suffix are
skipped, so it is safe to re-run.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

# Your date of birth (can be overridden with --dob YYYY-MM-DD).
DEFAULT_DOB = date(2023, 9, 1)

# Which file types to treat as images vs. videos. Add more if you need them.
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp"}

# Metadata fields to look at when figuring out *when* the file was captured,
# in priority order. The first one that holds a valid date wins. FileModifyDate
# is the last resort (it's the filesystem timestamp, not a "real" capture date).
DATE_TAGS = [
    "DateTimeOriginal",
    "CreateDate",
    "CreationDate",
    "MediaCreateDate",
    "TrackCreateDate",
    "FileModifyDate",
]

# Metadata fields to read an existing caption from, in priority order.
CAPTION_READ_TAGS = [
    "Caption-Abstract",   # IPTC
    "ImageDescription",   # EXIF
    "Description",         # XMP / QuickTime
    "Title",
]

# Matches a filename stem that already ends in an age suffix like "_(2)"
# so we don't process a file twice.
AGE_SUFFIX_RE = re.compile(r"_\(\d+\)$")


# --------------------------------------------------------------------------
# Age helpers
# --------------------------------------------------------------------------

def age_on(dob: date, on: date) -> int:
    """Return age in whole completed years on a given date."""
    years = on.year - dob.year
    if (on.month, on.day) < (dob.month, dob.day):
        years -= 1
    return years


def age_token(years: int) -> str:
    """Filesystem-safe age string, e.g. '0', '1', '2'."""
    return str(years)


# --------------------------------------------------------------------------
# ExifTool helpers
# --------------------------------------------------------------------------

def require_exiftool() -> None:
    if shutil.which("exiftool") is None:
        sys.exit(
            "ERROR: exiftool not found on PATH.\n"
            "Install it first — see the header of this script for instructions."
        )


def read_metadata(path: Path) -> dict:
    """Return a dict of the metadata tags we care about for one file."""
    cmd = ["exiftool", "-json", "-charset", "filename=UTF8"]
    cmd += [f"-{tag}" for tag in DATE_TAGS + CAPTION_READ_TAGS]
    cmd.append(str(path))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        return {}
    try:
        return json.loads(result.stdout)[0]
    except (json.JSONDecodeError, IndexError):
        return {}


def parse_exif_date(value: str) -> date | None:
    """Parse an ExifTool date string ('YYYY:MM:DD HH:MM:SS[+TZ]') -> date."""
    if not value:
        return None
    value = value.strip()
    if value.startswith("0000"):  # ExifTool's "empty" date
        return None
    # Strip a trailing timezone like '+01:00' and any sub-second part.
    datepart = value.split(" ")[0]
    try:
        return datetime.strptime(datepart, "%Y:%m:%d").date()
    except ValueError:
        return None


def pick_capture_date(meta: dict) -> tuple[date | None, str | None]:
    """Return (date, tag_name) for the first usable capture date."""
    for tag in DATE_TAGS:
        d = parse_exif_date(str(meta.get(tag, "")))
        if d is not None:
            return d, tag
    return None, None


def pick_existing_caption(meta: dict) -> str:
    for tag in CAPTION_READ_TAGS:
        val = meta.get(tag)
        if val:
            return str(val).strip()
    return ""


def write_caption(path: Path, caption: str, is_video: bool) -> bool:
    """Write the caption to the appropriate metadata fields for the type."""
    if is_video:
        tag_args = [
            f"-QuickTime:Description={caption}",
            f"-XMP-dc:Description={caption}",
        ]
    else:
        tag_args = [
            f"-EXIF:ImageDescription={caption}",
            f"-IPTC:Caption-Abstract={caption}",
            f"-XMP-dc:Description={caption}",
        ]
    cmd = ["exiftool", "-overwrite_original", "-m", "-charset", "filename=UTF8"]
    cmd += tag_args
    cmd.append(str(path))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"    ! caption write warning: {result.stderr.strip()}")
        return False
    return True


# --------------------------------------------------------------------------
# Main processing
# --------------------------------------------------------------------------

def process_file(path: Path, dob: date, dry_run: bool) -> str:
    """Process a single media file. Returns a one-line status string."""
    ext = path.suffix.lower()
    is_video = ext in VIDEO_EXTS

    # Skip files we've already tagged on a previous run.
    if AGE_SUFFIX_RE.search(path.stem):
        return f"skip  (already tagged): {path.name}"

    meta = read_metadata(path)
    capture_date, date_tag = pick_capture_date(meta)
    if capture_date is None:
        return f"SKIP  (no capture date found): {path.name}"

    years = age_on(dob, capture_date)
    if years < 0:
        return f"SKIP  (dated before DOB: {capture_date}): {path.name}"
    token = age_token(years)

    # Build the new caption: age prepended to whatever was there before.
    existing = pick_existing_caption(meta)
    new_caption = f"Age {token}" + (f" — {existing}" if existing else "")

    # Build the new filename: original stem + "_(age)" + original extension.
    new_path = path.with_name(f"{path.stem}_({token}){path.suffix}")
    if new_path.exists() and new_path != path:
        return f"SKIP  (target exists): {new_path.name}"

    if dry_run:
        return (
            f"would tag [{capture_date} via {date_tag}] -> {token}\n"
            f"          caption: {new_caption!r}\n"
            f"          rename : {path.name} -> {new_path.name}"
        )

    write_caption(path, new_caption, is_video)
    path.rename(new_path)
    return f"done  [{capture_date}] {token}: {path.name} -> {new_path.name}"


def iter_media(root: Path):
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in (IMAGE_EXTS | VIDEO_EXTS):
            yield p


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", help="Top directory to scan (e.g. 'Me')")
    parser.add_argument("--dob", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
                        default=DEFAULT_DOB,
                        help="Date of birth as YYYY-MM-DD (default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without modifying anything")
    args = parser.parse_args()

    require_exiftool()

    root = Path(args.root).expanduser()
    if not root.is_dir():
        sys.exit(f"ERROR: '{root}' is not a directory.")

    print(f"Scanning {root}  (DOB: {args.dob}, dry-run: {args.dry_run})\n")

    count = 0
    for path in iter_media(root):
        count += 1
        print(process_file(path, args.dob, args.dry_run))

    print(f"\n{'Previewed' if args.dry_run else 'Processed'} {count} media file(s).")


if __name__ == "__main__":
    main()
