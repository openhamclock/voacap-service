#!/usr/bin/env python3
"""
patch_csv.py - Comment out CSV lines that match patterns in a filter file.
 
Usage: python3 patch_csv.py <input.csv> <comments.txt> <output.csv>
 
Lines in the CSV that START WITH any string listed in comments.txt
will be prefixed with "#,notsupported,".
 
Example:
  comments.txt contains: 4,90,
  CSV line: 4,90,samples/sample.90,"IONCAP Rhombic..."
  Output:   #,notsupported,4,90,samples/sample.90,"IONCAP Rhombic..."
"""
# LICENSE_BEGIN
# Copyright (c) 2026 David Strickland KR8X
# SPDX-License-Identifier: AGPL-3.0-or-later
# LICENSE_END
 
import sys
 
def load_patterns(comments_file):
    """Load match patterns from comments.txt, one per line, stripping blanks."""
    with open(comments_file, 'r') as f:
        patterns = [line.rstrip('\n') for line in f if line.strip()]
    return patterns
 
def process_csv(input_file, comments_file, output_file):
    patterns = load_patterns(comments_file)
    print(f"Loaded {len(patterns)} pattern(s): {patterns}")
 
    matched = 0
    total = 0
 
    with open(input_file, 'r') as fin, open(output_file, 'w') as fout:
        for line in fin:
            total += 1
            stripped = line.rstrip('\n')
 
            # Check if line starts with any pattern
            hit = any(stripped.startswith(p) for p in patterns)
 
            if hit:
                fout.write(f"#,notsupported,{stripped}\n")
                matched += 1
            else:
                fout.write(line)
 
    print(f"Done. {matched}/{total} line(s) commented out -> {output_file}")
 
if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python3 comment_csv.py <input.csv> <comments.txt> <output.csv>")
        sys.exit(1)
 
    input_csv   = sys.argv[1]
    comments    = sys.argv[2]
    output_csv  = sys.argv[3]
 
    process_csv(input_csv, comments, output_csv)