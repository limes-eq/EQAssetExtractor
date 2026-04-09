# EQAssetExtractor
Python script for unpacking Everquest assets from s3d and eqg files

```
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
```

## Example args:

### Extract everything
python eq_asset_extractor.py /path/to/eq/ /path/to/output/

### Just build the manifest CSV, no extraction
python eq_asset_extractor.py /path/to/eq/ --list-only --csv assets.csv

### Only process .s3d files, verbose output
python eq_asset_extractor.py /path/to/eq/ /path/to/output/ --extensions s3d --verbose

## Process top-level directory only, overwrite existing files
python eq_asset_extractor.py /path/to/eq/ /path/to/output/ --no-recurse --overwrite
