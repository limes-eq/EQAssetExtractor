# EQAssetExtractor
Python script for unpacking Everquest assets from s3d and eqg files

**Args**:

# Extract everything
python eq_unpack.py /path/to/eq/ /path/to/output/

# Just build the manifest CSV, no extraction
python eq_unpack.py /path/to/eq/ --list-only --csv assets.csv

# Only process .s3d files, verbose output
python eq_unpack.py /path/to/eq/ /path/to/output/ --extensions s3d --verbose

# Process top-level directory only, overwrite existing files
python eq_unpack.py /path/to/eq/ /path/to/output/ --no-recurse --overwrite
