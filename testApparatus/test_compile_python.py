"""Repository-wide Python compile smoke test.

This test walks the project tree and compiles Python modules to catch syntax errors introduced by broad edits.
"""

import os
import sys
import traceback
import compileall

def compile_folder(folder_path: str):
    print(f"\n📁 Checking Python files in: {folder_path}")
    error_count = 0
    file_count = 0

    for root, _, files in os.walk(folder_path):
        for fname in files:
            if fname.endswith(".py"):
                file_path = os.path.join(root, fname)
                file_count += 1
                try:
                    with open(file_path, "rb") as f:
                        source = f.read()
                        compile(source, file_path, "exec")
                except SyntaxError as se:
                    error_count += 1
                    print(f"❌ Syntax error in: {file_path}")
                    print(f"   ↳ {se.msg} (line {se.lineno}, offset {se.offset})")
                except Exception as e:
                    error_count += 1
                    print(f"❌ Error in: {file_path}")
                    print(f"   ↳ {type(e).__name__}: {e}")
                    traceback.print_exc()

    print(f"\n🔎 Scanned {file_count} file(s), found {error_count} error(s).")
    return error_count


def test_python_sources_compile():
    """Compile owned Python sources without traversing installed environments."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    errors = sum(compile_folder(str(root / name)) for name in (
        "sensorius", "testApparatus", "platform_installers",
    ))
    compile((root / "Sensorius.py").read_bytes(), str(root / "Sensorius.py"), "exec")
    assert errors == 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage:\n  python check_syntax_errors.py /path/to/folder")
        sys.exit(1)

    folder = sys.argv[1]
    if not os.path.isdir(folder):
        print(f"❌ Folder not found: {folder}")
        sys.exit(1)

    exit_code = compile_folder(folder)
    sys.exit(1 if exit_code > 0 else 0)
