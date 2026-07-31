"""Audit project modules for PEP 257 docstring structure.

Importing creates the ``pep257_audit/`` output directory; running the script
writes JSON and text reports there. Recommendations are heuristic and require
review for behavioral accuracy.
"""
import ast
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", "__pycache__", "node_modules", "venv", ".venv", "build", "dist", "tmp", "ota_packages"}

RESULT_DIR = ROOT / "pep257_audit"
RESULT_DIR.mkdir(exist_ok=True)


def is_skipped_dir(path: Path) -> bool:
    parts = {p for p in path.parts}
    return bool(parts & SKIP_DIRS)


def get_public_api(tree: ast.AST):
    pub = {"functions": [], "classes": []}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            if not node.name.startswith("_"):
                pub["functions"].append(node.name)
        elif isinstance(node, ast.ClassDef):
            if not node.name.startswith("_"):
                pub["classes"].append(node.name)
    return pub


def analyze_module(path: Path):
    try:
        src = path.read_text(encoding="utf-8")
    except Exception as e:
        return {"path": str(path), "error": f"read_error: {e}"}
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return {"path": str(path), "error": f"syntax_error: {e}"}

    doc = ast.get_docstring(tree)
    pub = get_public_api(tree)

    issues = []
    suggestion = None

    if not doc:
        issues.append("missing_module_docstring")
    else:
        # Check structure: first non-empty line, then blank line, then context
        lines = doc.splitlines()
        # find first non-empty line index
        first_i = None
        for i, l in enumerate(lines):
            if l.strip():
                first_i = i
                break
        if first_i is None:
            issues.append("empty_docstring")
        else:
            # Check whether there's content after first line and whether second line is blank when there is
            after = lines[first_i+1:]
            if after:
                if after[0].strip() != "":
                    issues.append("missing_blank_line_after_summary")
            # If first line is long, warn
            if len(lines[first_i]) > 200:
                issues.append("summary_too_long")

    # Build a suggested docstring following the user's required template
    summary = f"{path.stem.replace('_', ' ').capitalize()} module."
    context_lines = []
    # Important behaviors/invariants/side-effects
    if pub["classes"] or pub["functions"]:
        context_lines.append("Provides the following public API:")
        for c in pub["classes"]:
            context_lines.append(f"- Class: {c}")
        for f in pub["functions"]:
            context_lines.append(f"- Function: {f}()")
    else:
        context_lines.append("No public classes or functions are exported at module level.")

    # Check for suspicious top-level I/O or global state
    suspicious = []
    # crude heuristics: look for open(, sqlite3.connect, requests, mqtt
    lower = src.lower()
    if "open(" in lower:
        suspicious.append("may perform file I/O at import time")
    if "sqlite3.connect(" in lower or "sqlite3." in lower:
        suspicious.append("may access SQLite DB at import time")
    if "requests." in lower or "httpx" in lower:
        suspicious.append("may perform network I/O at import time")
    if "threading." in lower or "multiprocessing." in lower:
        suspicious.append("may create threads/processes at import time")

    if suspicious:
        context_lines.append("")
        context_lines.append("Surprising concerns:")
        for s in suspicious:
            context_lines.append(f"- {s}")

    suggestion = "\n".join([summary, "", "\n".join(context_lines)])

    return {
        "path": str(path.relative_to(ROOT)),
        "doc_present": bool(doc),
        "issues": issues,
        "public_api": pub,
        "suggested_docstring": suggestion,
    }


def main():
    results = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        # filter out skip dirs in-place to avoid walking them
        rel = Path(dirpath).relative_to(ROOT)
        if is_skipped_dir(rel):
            dirnames[:] = []
            continue
        # also skip hidden top-level folders like .venv
        parts = rel.parts
        if any(p.startswith(".") for p in parts if p):
            continue
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            fpath = Path(dirpath) / fn
            # ignore tests? include tests as they are code too
            # analyze
            res = analyze_module(fpath)
            results.append(res)

    out_json = RESULT_DIR / "pep257_audit_results.json"
    out_txt = RESULT_DIR / "pep257_audit_report.txt"
    out_json.write_text(json.dumps(results, indent=2), encoding="utf-8")

    lines = []
    for r in results:
        lines.append(f"Module: {r.get('path')}")
        if r.get("error"):
            lines.append(f"  ERROR: {r.get('error')}")
            lines.append("")
            continue
        lines.append(f"  Doc present: {r.get('doc_present')}")
        if r.get("issues"):
            lines.append(f"  Issues: {', '.join(r.get('issues'))}")
        lines.append("  Suggested module docstring:")
        s = r.get('suggested_docstring') or ''
        for line in s.splitlines():
            lines.append(f"    {line}")
        lines.append("")

    out_txt.write_text("\n".join(lines), encoding="utf-8")

    print(f"Audit complete. Results written to: {out_json} and {out_txt}")


if __name__ == '__main__':
    main()
