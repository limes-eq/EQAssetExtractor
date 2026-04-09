#!/usr/bin/env python3
"""
eq_asset_extractor.py — EverQuest PFS Archive Unpacker
Supports .s3d, .eqg, .pfs, and .pak archive formats.

Based on the PFS file format as documented in:
  https://github.com/martinlindhe/eqformat_pfs/blob/master/format_pfs.md
  and derived from the EQZip C# source by Shendare (Jon D. Jackson, CC0).

Usage:
    python eq_asset_extractor.py <input_dir> <output_dir> [options]

Options:
    --list-only         Build the CSV manifest without extracting any files.
    --csv <path>        Path for the output CSV file.
                        Defaults to <output_dir>/assets.csv
    --extensions <exts> Comma-separated list of extensions to process.
                        Defaults to: s3d,eqg,pfs,pak
    --no-recurse        Do not search sub-directories of input_dir.
    --verbose           Print every asset entry as it is processed.
    --overwrite         Overwrite existing extracted files (default: skip).

Exit codes:
    0  Success
    1  Bad arguments / input directory not found
    2  One or more archives failed to parse (others still processed)
"""

import argparse
import csv
import os
import struct
import sys
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional, Tuple


# ---------------------------------------------------------------------------
# PFS format constants
# ---------------------------------------------------------------------------

PFS_MAGIC = b"PFS "                     # bytes 4-7 of every PFS file
DIRECTORY_CRC = 0x61580AC9              # sentinel CRC that marks the name directory entry
MAX_INFLATE_BLOCK = 8192                # EQ client hard limit; blocks larger than this crash the client

# EQ uses a non-standard CRC-32 with polynomial 0x04C11DB7 (big-endian / MSB-first).
# Build the lookup table once at import time.
_EQ_CRC_POLY = 0x04C11DB7

def _build_eq_crc_table() -> list:
    table = []
    for i in range(256):
        crc = i << 24
        for _ in range(8):
            if crc & 0x80000000:
                crc = ((crc << 1) ^ _EQ_CRC_POLY) & 0xFFFFFFFF
            else:
                crc = (crc << 1) & 0xFFFFFFFF
        table.append(crc)
    return table

_EQ_CRC_TABLE = _build_eq_crc_table()


def eq_crc32(name: str) -> int:
    """
    Compute the EQ-specific CRC-32 of a filename.

    The game lowercases the name, then CRCs it including the null terminator.
    The initial CRC value is 0.
    """
    data = (name.lower() + "\x00").encode("latin-1", errors="replace")
    crc = 0
    for byte in data:
        i = ((crc >> 24) ^ byte) & 0xFF
        crc = ((crc << 8) ^ _EQ_CRC_TABLE[i]) & 0xFFFFFFFF
    return crc


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PFSEntry:
    """One file stored inside a PFS archive."""
    crc: int
    offset: int                         # absolute byte offset of compressed data inside archive
    expanded_size: int


@dataclass
class AssetRecord:
    """One row in the output CSV."""
    archive_file: str                   # basename of the .s3d/.eqg/… file
    archive_path: str                   # full path to the archive
    asset_name: str                     # filename as stored inside the archive
    compressed_size: int                # total bytes on disk (sum of all compressed blocks)
    expanded_size: int                  # uncompressed size in bytes
    extracted_to: str                   # output path, or "" when --list-only


# ---------------------------------------------------------------------------
# Low-level PFS reader
# ---------------------------------------------------------------------------

class PFSError(Exception):
    pass


def _read_u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _decompress_blocks(archive_data: bytes, block_offset: int, expanded_size: int) -> Tuple[bytes, int]:
    """
    Inflate one or more zlib-compressed blocks starting at *block_offset*.

    Each block has the layout:
        u32  deflate_length   (compressed byte count for this block)
        u32  inflate_length   (uncompressed byte count for this block; <= 8192)
        <deflate_length bytes of raw zlib-deflate data (no header)>

    Returns (uncompressed_bytes, total_compressed_bytes_consumed).
    Raises PFSError on decompression trouble.
    """
    result = bytearray()
    compressed_total = 0
    pos = block_offset

    while len(result) < expanded_size:
        if pos + 8 > len(archive_data):
            raise PFSError(f"Unexpected end of file at block offset {pos:#x}")

        deflate_len = _read_u32(archive_data, pos)
        inflate_len = _read_u32(archive_data, pos + 4)
        pos += 8
        compressed_total += 8

        if deflate_len == 0 or inflate_len == 0:
            break

        chunk = archive_data[pos: pos + deflate_len]
        if len(chunk) < deflate_len:
            raise PFSError(f"Truncated compressed block at {pos:#x}")

        try:
            # EQ uses raw deflate (wbits = -15 skips the zlib wrapper)
            decompressed = zlib.decompress(chunk, wbits=-15)
        except zlib.error:
            # Fall back to regular zlib in case of wrapped stream
            try:
                decompressed = zlib.decompress(chunk)
            except zlib.error as exc:
                raise PFSError(f"zlib error at block offset {pos:#x}: {exc}") from exc

        result.extend(decompressed[:inflate_len])
        pos += deflate_len
        compressed_total += deflate_len

    return bytes(result[:expanded_size]), compressed_total


def parse_pfs(archive_path: Path) -> Iterator[Tuple[str, bytes, int]]:
    """
    Parse a PFS archive and yield ``(filename, raw_bytes, compressed_size)``
    for every embedded file (the directory meta-entry is excluded).

    Raises ``PFSError`` on malformed input.
    """
    data = archive_path.read_bytes()

    if len(data) < 8:
        raise PFSError("File too small to be a valid PFS archive")

    # Header: u32 ofs_entries, u32 magic
    ofs_entries = _read_u32(data, 0)

    magic = data[4:8]
    if magic != PFS_MAGIC:
        raise PFSError(f"Bad PFS magic: {magic!r} (expected {PFS_MAGIC!r})")

    if ofs_entries + 4 > len(data):
        raise PFSError(f"Entry table offset {ofs_entries:#x} is beyond file size {len(data):#x}")

    # Directory entry table
    num_entries = _read_u32(data, ofs_entries)
    entries: List[PFSEntry] = []

    pos = ofs_entries + 4
    for _ in range(num_entries):
        if pos + 12 > len(data):
            raise PFSError("Entry table truncated")
        crc = _read_u32(data, pos)
        ofs_file = _read_u32(data, pos + 4)
        exp_size = _read_u32(data, pos + 8)
        entries.append(PFSEntry(crc=crc, offset=ofs_file, expanded_size=exp_size))
        pos += 12

    # Find the filename-directory entry (sentinel CRC)
    dir_entry: Optional[PFSEntry] = None
    data_entries: List[PFSEntry] = []
    for e in entries:
        if e.crc == DIRECTORY_CRC:
            dir_entry = e
        else:
            data_entries.append(e)

    if dir_entry is None:
        raise PFSError("No directory entry found (missing sentinel CRC 0x61580AC9)")

    # Decompress the filename directory
    dir_bytes, _ = _decompress_blocks(data, dir_entry.offset, dir_entry.expanded_size)

    # Parse filename directory:
    #   u32  count
    #   then for each entry:
    #     u32  filename_length   (byte count of the name that follows)
    #     <filename_length bytes>  (the filename; may include a null terminator)
    #
    # Note: there is NO separate "entry_length" prefix — the wiki and multiple
    # open-source parsers confirm the layout is simply (u32 len, <len bytes>).
    if len(dir_bytes) < 4:
        raise PFSError("Directory block too small")

    dir_count = struct.unpack_from("<I", dir_bytes, 0)[0]

    # Build a CRC → filename lookup so we can match entries by CRC rather than
    # relying on positional ordering, which varies across archive versions.
    crc_to_name: dict = {}
    dpos = 4
    for _ in range(dir_count):
        if dpos + 4 > len(dir_bytes):
            break
        fname_len = struct.unpack_from("<I", dir_bytes, dpos)[0]
        dpos += 4
        raw_name = dir_bytes[dpos: dpos + fname_len]
        # The name may include a null terminator — strip it before storing.
        name = raw_name.rstrip(b"\x00").decode("latin-1", errors="replace")
        dpos += fname_len

        if name:
            crc_to_name[eq_crc32(name)] = name

    for idx, entry in enumerate(data_entries):
        name = crc_to_name.get(entry.crc, f"unknown_{idx:04d}")
        raw_bytes, comp_size = _decompress_blocks(data, entry.offset, entry.expanded_size)
        yield name, raw_bytes, comp_size


# ---------------------------------------------------------------------------
# Archive scanner
# ---------------------------------------------------------------------------

SUPPORTED_EXTENSIONS = {"s3d", "eqg", "pfs", "pak"}


def find_archives(input_dir: Path, extensions: set, recurse: bool) -> List[Path]:
    """Return all archive files under *input_dir* matching *extensions*."""
    archives: List[Path] = []
    if recurse:
        for ext in extensions:
            archives.extend(input_dir.rglob(f"*.{ext}"))
            archives.extend(input_dir.rglob(f"*.{ext.upper()}"))
    else:
        for ext in extensions:
            archives.extend(input_dir.glob(f"*.{ext}"))
            archives.extend(input_dir.glob(f"*.{ext.upper()}"))
    # Deduplicate (case-insensitive paths on Windows)
    seen = set()
    unique: List[Path] = []
    for p in archives:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return sorted(unique)


# ---------------------------------------------------------------------------
# Main extraction logic
# ---------------------------------------------------------------------------

def process_archives(
    archives: List[Path],
    output_dir: Optional[Path],
    list_only: bool,
    overwrite: bool,
    verbose: bool,
) -> Tuple[List[AssetRecord], int]:
    """
    Iterate archives, optionally extract files, and collect AssetRecords.

    Returns (records, error_count).
    """
    records: List[AssetRecord] = []
    error_count = 0

    for archive_path in archives:
        archive_name = archive_path.name
        print(f"  Processing: {archive_path}")

        try:
            asset_iter = parse_pfs(archive_path)
        except PFSError as exc:
            print(f"    [ERROR] Could not parse archive: {exc}", file=sys.stderr)
            error_count += 1
            continue

        # Determine per-archive output sub-directory
        if output_dir is not None and not list_only:
            arc_out_dir = output_dir / archive_path.stem
            arc_out_dir.mkdir(parents=True, exist_ok=True)
        else:
            arc_out_dir = None

        try:
            for asset_name, raw_bytes, comp_size in asset_iter:
                extracted_to = ""

                if arc_out_dir is not None:
                    dest = arc_out_dir / asset_name
                    # Prevent path traversal
                    if not dest.resolve().is_relative_to(arc_out_dir.resolve()):
                        print(f"    [SKIP] Suspicious path: {asset_name}", file=sys.stderr)
                        continue
                    if overwrite or not dest.exists():
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(raw_bytes)
                    extracted_to = str(dest)

                rec = AssetRecord(
                    archive_file=archive_name,
                    archive_path=str(archive_path),
                    asset_name=asset_name,
                    compressed_size=comp_size,
                    expanded_size=len(raw_bytes),
                    extracted_to=extracted_to,
                )
                records.append(rec)

                if verbose:
                    status = f"→ {extracted_to}" if extracted_to else "(listed)"
                    print(f"    {asset_name:40s}  {len(raw_bytes):>10,} B  {status}")

        except PFSError as exc:
            print(f"    [ERROR] While reading entries: {exc}", file=sys.stderr)
            error_count += 1

    return records, error_count


# ---------------------------------------------------------------------------
# CSV writer
# ---------------------------------------------------------------------------

CSV_FIELDNAMES = [
    "archive_file",
    "archive_path",
    "asset_name",
    "compressed_size",
    "expanded_size",
    "extracted_to",
]


def write_csv(records: List[AssetRecord], csv_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        for rec in records:
            writer.writerow({
                "archive_file": rec.archive_file,
                "archive_path": rec.archive_path,
                "asset_name": rec.asset_name,
                "compressed_size": rec.compressed_size,
                "expanded_size": rec.expanded_size,
                "extracted_to": rec.extracted_to,
            })
    print(f"\nCSV written: {csv_path}  ({len(records):,} rows)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eq_asset_extractor.py",
        description="Unpack EverQuest .s3d / .eqg / .pfs / .pak archives.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Exit codes:")[1].strip() if "Exit codes:" in __doc__ else "",
    )
    parser.add_argument(
        "input_dir",
        metavar="INPUT_DIR",
        help="Directory containing EQ archive files to process.",
    )
    parser.add_argument(
        "output_dir",
        metavar="OUTPUT_DIR",
        nargs="?",
        default=None,
        help=(
            "Directory where extracted assets will be written. "
            "Required unless --list-only is set."
        ),
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Build the CSV manifest without extracting any files.",
    )
    parser.add_argument(
        "--csv",
        metavar="PATH",
        default=None,
        help="Path for the output CSV file. Defaults to <output_dir>/assets.csv",
    )
    parser.add_argument(
        "--extensions",
        metavar="EXTS",
        default=",".join(sorted(SUPPORTED_EXTENSIONS)),
        help=(
            "Comma-separated list of file extensions to process "
            f"(default: {','.join(sorted(SUPPORTED_EXTENSIONS))})."
        ),
    )
    parser.add_argument(
        "--no-recurse",
        action="store_true",
        help="Do not search sub-directories of INPUT_DIR.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print every asset entry as it is processed.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing extracted files (default: skip).",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # --- Validate arguments ---
    input_dir = Path(args.input_dir)
    if not input_dir.is_dir():
        print(f"[ERROR] Input directory not found: {input_dir}", file=sys.stderr)
        return 1

    list_only: bool = args.list_only

    if args.output_dir is None and not list_only:
        print(
            "[ERROR] OUTPUT_DIR is required unless --list-only is set.",
            file=sys.stderr,
        )
        parser.print_usage(sys.stderr)
        return 1

    output_dir = Path(args.output_dir) if args.output_dir else None

    if output_dir is not None and not list_only:
        output_dir.mkdir(parents=True, exist_ok=True)

    # CSV path
    if args.csv:
        csv_path = Path(args.csv)
    elif output_dir:
        csv_path = output_dir / "assets.csv"
    else:
        csv_path = input_dir / "assets.csv"

    extensions = {e.strip().lower().lstrip(".") for e in args.extensions.split(",")}
    unknown = extensions - SUPPORTED_EXTENSIONS
    if unknown:
        print(
            f"[WARNING] Unrecognised extension(s) requested: {', '.join(sorted(unknown))}. "
            "They will be scanned but may not parse correctly.",
            file=sys.stderr,
        )

    recurse = not args.no_recurse

    # --- Discover archives ---
    print(f"\nScanning: {input_dir}")
    archives = find_archives(input_dir, extensions, recurse)
    if not archives:
        print("No matching archive files found.")
        return 0

    print(
        f"Found {len(archives)} archive(s) with extension(s): "
        f"{', '.join('.' + e for e in sorted(extensions))}\n"
    )
    if list_only:
        print("Mode: LIST ONLY (no files will be extracted)\n")
    else:
        print(f"Extracting to: {output_dir}\n")

    # --- Process ---
    records, error_count = process_archives(
        archives=archives,
        output_dir=output_dir,
        list_only=list_only,
        overwrite=args.overwrite,
        verbose=args.verbose,
    )

    # --- Summary ---
    total_expanded = sum(r.expanded_size for r in records)
    total_compressed = sum(r.compressed_size for r in records)
    print(
        f"\n{'─' * 60}\n"
        f"Archives processed : {len(archives) - error_count:,}  "
        f"(errors: {error_count})\n"
        f"Total assets found : {len(records):,}\n"
        f"Compressed size    : {total_compressed:,} bytes\n"
        f"Expanded size      : {total_expanded:,} bytes"
    )

    # --- Write CSV ---
    write_csv(records, csv_path)

    return 2 if error_count else 0


if __name__ == "__main__":
    sys.exit(main())