#!/usr/bin/env python3
"""
add_age_to_captions_ashell.py
=============================

Pure-Python version for iOS a-Shell (no ExifTool / Perl needed).

Your media lives in `Me/<year>/...` (year-named subdirectories). For each
photo/video the age is computed like this:

    * If the file has a usable EXIF capture date  -> exact age on that
      date, counting your Sept-1 birthday. (Jan-Aug 2020 -> 17,
      Sep-Dec 2020 -> 18, given a 2002-09-01 birth date.)
    * Otherwise -> simple math from the YEAR FOLDER:  <year> - <birth year>
      (e.g. anything in Me/2020/ with no embedded date -> 18).

The folder-year fallback exists because many files have no reliable
embedded date, and after copying to iOS their file timestamps all look
like "today" -- which is what produced the wrong ages before. We only
fall back to the file timestamp if there's no EXIF date AND no year
folder.

That age is prepended to the caption (for JPEGs) and appended to the
filename for every photo and video:

    Me/2026/IMG_1234.jpg  ->  Me/2026/IMG_1234_(24).jpg
    (and its caption becomes:  "Age 24 - <original caption>")

------------------------------------------------------------------------
What gets changed
------------------------------------------------------------------------
* Every photo AND video is RENAMED to include the age:  name_(age).ext
* The caption is EMBEDDED only for JPEG (.jpg/.jpeg), because the pure-
  Python `piexif` library can only write those. HEIC / PNG / videos are
  still renamed, but their caption can't be embedded on iOS without
  ExifTool (use the iSH app for that). Each such file prints a note.

------------------------------------------------------------------------
Setup in a-Shell
------------------------------------------------------------------------
    pip install piexif

------------------------------------------------------------------------
Usage
------------------------------------------------------------------------
    # 1) Reverse the previous (incorrect) run -- preview then apply:
    python add_age_to_captions_ashell.py Me --undo --dry-run
    python add_age_to_captions_ashell.py Me --undo

    # 2) Run correctly -- preview then apply:
    python add_age_to_captions_ashell.py Me --dry-run
    python add_age_to_captions_ashell.py Me

    # Override birth date if ever needed:
    python add_age_to_captions_ashell.py Me --dob 2002-09-01

Idempotent & reversible: re-running skips files already tagged, and
--undo restores original names/captions.
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

DEFAULT_DOB = date(2002, 9, 1)

# Photos + videos we will rename.
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS

# Only these can have a caption embedded by piexif.
CAPTION_EXTS = {".jpg", ".jpeg"}

# Formats piexif can read an EXIF date out of (for accurate, birthday-aware age).
DATE_READABLE_EXTS = {".jpg", ".jpeg", ".tif", ".tiff", ".webp"}

# A 4-digit year, used both to recognize year folders and the age suffix.
YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")

# Filename stems already ending in an age suffix like "_(24)".
AGE_SUFFIX_RE = re.compile(r"_\(\d+\)$")

# Matches a caption this script previously wrote, so --undo can restore the
# original text:  "Age 24"  or  "Age 24 - some original caption"
ADDED_CAPTION_RE = re.compile(r"^Age \d+(?: - (?P<orig>.*))?$", re.DOTALL)


# --------------------------------------------------------------------------
# Year / age helpers
# --------------------------------------------------------------------------

def folder_year(path: Path, root: Path) -> int | None:
    """Walk up from the file to `root`, returning the first year-named folder."""
    for parent in path.parents:
        if YEAR_RE.match(parent.name):
            return int(parent.name)
        if parent == root:
            break
    return None


def age_on(dob: date, on: date) -> int:
    """Exact age in whole completed years on a given date (honors birthday)."""
    years = on.year - dob.year
    if (on.month, on.day) < (dob.month, dob.day):
        years -= 1
    return years


def exif_date(path: Path) -> date | None:
    """Full capture date from EXIF, or None. Used for birthday-accurate age."""
    try:
        exif = piexif.load(str(path))
    except Exception:  # noqa: BLE001
        return None
    for ifd, tag in (
        ("Exif", piexif.ExifIFD.DateTimeOriginal),
        ("Exif", piexif.ExifIFD.DateTimeDigitized),
        ("0th", piexif.ImageIFD.DateTime),
    ):
        raw = exif.get(ifd, {}).get(tag)
        if not raw:
            continue
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        text = text.strip()
        if text.startswith("0000"):
            continue
        try:
            return datetime.strptime(text[:10], "%Y:%m:%d").date()
        except ValueError:
            continue
    return None


def resolve_age(path: Path, root: Path, dob: date) -> tuple[int, str]:
    """Birthday-accurate age from EXIF when possible, else folder-year math."""
    if path.suffix.lower() in DATE_READABLE_EXTS:
        d = exif_date(path)
        if d is not None:
            return age_on(dob, d), f"exif {d}"
    y = folder_year(path, root)
    if y is not None:
        return y - dob.year, f"folder {y}"
    # Last resort: file modification time (unreliable, hence last).
    y = datetime.fromtimestamp(os.path.getmtime(path)).year
    return y - dob.year, f"mtime {y}"


# --------------------------------------------------------------------------
# Caption read/write (piexif, JPEG only)
# --------------------------------------------------------------------------

def read_caption(path: Path) -> str:
    try:
        exif = piexif.load(str(path))
    except Exception:  # noqa: BLE001
        return ""
    raw = exif.get("0th", {}).get(piexif.ImageIFD.ImageDescription)
    if not raw:
        return ""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    return text.split("\x00")[0].strip()


def write_caption(path: Path, caption: str) -> None:
    try:
        exif = piexif.load(str(path))
    except Exception:  # noqa: BLE001
        exif = {"0th": {}, "Exif": {}, "1st": {}, "GPS": {}, "Interop": {}}
    exif.setdefault("0th", {})
    if caption:
        exif["0th"][piexif.ImageIFD.ImageDescription] = caption.encode("utf-8")
    else:
        exif["0th"].pop(piexif.ImageIFD.ImageDescription, None)
    piexif.insert(piexif.dump(exif), str(path))


# --------------------------------------------------------------------------
# Apply / undo for a single file
# --------------------------------------------------------------------------

def apply_file(path: Path, dob: date, root: Path, dry_run: bool) -> str:
    ext = path.suffix.lower()
    if AGE_SUFFIX_RE.search(path.stem):
        return f"skip  (already tagged): {path.name}"

    age, src = resolve_age(path, root, dob)
    if age < 0:
        return f"SKIP  ({src} is before birth date): {path.name}"
    token = str(age)

    new_path = path.with_name(f"{path.stem}_({token}){path.suffix}")
    if new_path.exists() and new_path != path:
        return f"SKIP  (target exists): {new_path.name}"

    can_caption = ext in CAPTION_EXTS
    existing = read_caption(path) if can_caption else ""
    new_caption = f"Age {token}" + (f" - {existing}" if existing else "")

    if dry_run:
        cap = f"caption: {new_caption!r}" if can_caption else "caption: (rename only)"
        return (f"would tag [{src}] -> {token}\n"
                f"          {cap}\n"
                f"          rename : {path.name} -> {new_path.name}")

    if can_caption:
        write_caption(path, new_caption)
    path.rename(new_path)
    note = "" if can_caption else "  (renamed only; caption not embeddable on iOS)"
    return f"done  [{src}] {token}: {path.name} -> {new_path.name}{note}"


def undo_file(path: Path, dry_run: bool) -> str | None:
    ext = path.suffix.lower()
    if not AGE_SUFFIX_RE.search(path.stem):
        return None  # nothing this script added
    orig_stem = AGE_SUFFIX_RE.sub("", path.stem)
    orig_path = path.with_name(f"{orig_stem}{path.suffix}")
    if orig_path.exists() and orig_path != path:
        return f"SKIP  (original name already exists): {orig_path.name}"

    restored = None
    if ext in CAPTION_EXTS:
        m = ADDED_CAPTION_RE.match(read_caption(path))
        if m:
            restored = (m.group("orig") or "").strip()

    if dry_run:
        cap = f"  restore caption -> {restored!r}" if restored is not None else ""
        return f"would undo: {path.name} -> {orig_path.name}{cap}"

    if restored is not None:
        write_caption(path, restored)
    path.rename(orig_path)
    return f"undone: {path.name} -> {orig_path.name}"


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def media_files(root: Path):
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in MEDIA_EXTS:
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
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without modifying anything")
    parser.add_argument("--undo", action="store_true",
                        help="Reverse a previous run (restore names + captions)")
    args = parser.parse_args()

    root = Path(args.root).expanduser()
    if not root.is_dir():
        sys.exit(f"ERROR: '{root}' is not a directory.")

    # Show the subdirectories up front so traversal is visible.
    subdirs = sorted(d.name for d in root.iterdir() if d.is_dir())
    year_dirs = [d for d in subdirs if YEAR_RE.match(d)]
    print(f"Scanning {root}  (DOB: {args.dob}, "
          f"mode: {'UNDO' if args.undo else 'apply'}, dry-run: {args.dry_run})")
    print(f"Subdirectories: {', '.join(subdirs) or '(none)'}")
    print(f"Year folders  : {', '.join(year_dirs) or '(none)'}\n")

    count = 0
    for path in media_files(root):
        if args.undo:
            line = undo_file(path, args.dry_run)
            if line is None:
                continue
        else:
            line = apply_file(path, args.dob, root, args.dry_run)
        count += 1
        print(line)

    verb = "Previewed" if args.dry_run else ("Reversed" if args.undo else "Processed")
    print(f"\n{verb} {count} file(s).")


if __name__ == "__main__":
    main()
