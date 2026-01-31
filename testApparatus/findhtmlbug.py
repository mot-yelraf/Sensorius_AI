#!/usr/bin/env python3
"""
count_chars.py

Counts total characters in a file and shows a snippet of 10 characters
before and 10 characters after a given character index.

Usage:
    python count_chars.py <filename> <char_index>

Example:
    python count_chars.py sample.txt 150
"""

import sys
from pathlib import Path

def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <filename> <char_index>")
        sys.exit(1)

    filename = Path(sys.argv[1])
    try:
        char_index = int(sys.argv[2])
    except ValueError:
        print("Error: <char_index> must be an integer.")
        sys.exit(1)

    if not filename.is_file():
        print(f"Error: File '{filename}' does not exist.")
        sys.exit(1)

    # Read file contents
    text = filename.read_text(encoding="utf-8", errors="replace")
    total_chars = len(text)

    if char_index < 0 or char_index >= total_chars:
        print(f"Error: char_index {char_index} is out of range (0 to {total_chars - 1}).")
        sys.exit(1)

    # Calculate snippet range
    start = max(0, char_index - 10)
    end = min(total_chars, char_index + 11)  # +1 for the target char
    snippet = text[start:end]

    print(f"Total characters in file: {total_chars}")
    print(f"Character at index {char_index!r}: {text[char_index]!r}")
    print(f"Snippet (10 before and after):\n{snippet}")

if __name__ == "__main__":
    main()
