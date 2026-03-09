#!/usr/bin/env python3
"""
PR Review CLI Tool
Analyzes git diffs and provides LLM-powered code reviews.
"""

import os
import sys
import ast
import subprocess
import argparse
import json
from pathlib import Path
from dotenv import load_dotenv
from dataclasses import dataclass, field
from typing import Optional
import openai
# ─────────────────────────────────────────────
# Data Models
# ─────────────────────────────────────────────

load_dotenv()

@dataclass
class FileDiff:
    path: str
    old_code: str
    new_code: str
    additions: int
    deletions: int
    raw_diff: str


@dataclass
class DiffAnalysis:
    is_big_change: bool
    reason: str
    affected_symbols: list[str]  # functions/classes that changed signature
    impacted_files: list[str]  # files that might be affected


# ─────────────────────────────────────────────
# Git Utilities
# ─────────────────────────────────────────────


def run_git(*args) -> str:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",  # ← this line must be there
        errors="replace",  # ← this line must be there
        check=True,
    )
    return result.stdout


def get_diff(base: str = "HEAD") -> str:
    """Get the full unified diff against base."""
    try:
        return run_git("diff", base)
    except subprocess.CalledProcessError:
        # Fallback: staged diff
        return run_git("diff", "--cached")


def get_repo_root() -> Path:
    return Path(run_git("rev-parse", "--show-toplevel").strip())


def parse_diff(raw_diff: str) -> list[FileDiff]:
    """Parse a unified diff into per-file FileDiff objects."""
    files: list[FileDiff] = []
    current_file: Optional[str] = None
    old_lines: list[str] = []
    new_lines: list[str] = []
    hunk_lines: list[str] = []
    additions = 0
    deletions = 0

    def flush():
        nonlocal additions, deletions, old_lines, new_lines, hunk_lines
        if current_file:
            files.append(
                FileDiff(
                    path=current_file,
                    old_code="\n".join(old_lines),
                    new_code="\n".join(new_lines),
                    additions=additions,
                    deletions=deletions,
                    raw_diff="\n".join(hunk_lines),
                )
            )
        old_lines, new_lines, hunk_lines = [], [], []
        additions, deletions = 0, 0

    for line in raw_diff.splitlines():
        if line.startswith("diff --git"):
            flush()
            # Extract file path from "diff --git a/foo b/foo"
            parts = line.split(" b/", 1)
            current_file = parts[1] if len(parts) > 1 else None
        elif line.startswith("---") or line.startswith("+++"):
            hunk_lines.append(line)
        elif line.startswith("@@"):
            hunk_lines.append(line)
        elif line.startswith("-"):
            old_lines.append(line[1:])
            deletions += 1
            hunk_lines.append(line)
        elif line.startswith("+"):
            new_lines.append(line[1:])
            additions += 1
            hunk_lines.append(line)
        else:
            old_lines.append(line[1:] if line.startswith(" ") else line)
            new_lines.append(line[1:] if line.startswith(" ") else line)
            hunk_lines.append(line)

    flush()
    return [f for f in files if f.path]  # filter empty


# ─────────────────────────────────────────────
# AST Impact Analysis
# ─────────────────────────────────────────────


def extract_changed_symbols(old_code: str, new_code: str) -> list[str]:
    """
    Find function/class names whose signatures changed between old and new code.
    Uses AST comparison — Python files only.
    """
    changed = []

    def get_signatures(source: str) -> dict[str, str]:
        sigs: dict[str, str] = {}
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return sigs
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = [a.arg for a in node.args.args]
                sigs[node.name] = f"{node.name}({', '.join(args)})"
            elif isinstance(node, ast.ClassDef):
                sigs[node.name] = f"class {node.name}"
        return sigs

    old_sigs = get_signatures(old_code)
    new_sigs = get_signatures(new_code)

    for name, sig in new_sigs.items():
        if name in old_sigs and old_sigs[name] != sig:
            changed.append(name)
        # New symbol (could be a rename — treat as potentially impactful)

    return changed


def find_usages_in_repo(
    symbols: list[str], repo_root: Path, exclude_file: str
) -> dict[str, list[str]]:
    """
    For each symbol, find Python files in the repo that use it.
    Returns {symbol: [file_path, ...]}
    """
    usages: dict[str, list[str]] = {s: [] for s in symbols}
    py_files = [
        p
        for p in repo_root.rglob("*.py")
        if ".venv" not in p.parts
        and "node_modules" not in p.parts
        and str(p) != str(repo_root / exclude_file)
    ]

    for py_file in py_files:
        try:
            source = py_file.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(source)
        except (SyntaxError, OSError):
            continue

        for node in ast.walk(tree):
            # Detect calls: symbol(...) or obj.symbol(...)
            if isinstance(node, ast.Call):
                func = node.func
                name = None
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                if name and name in usages:
                    rel = str(py_file.relative_to(repo_root))
                    if rel not in usages[name]:
                        usages[name].append(rel)

    return usages


def collect_impacted_file_snippets(
    usages: dict[str, list[str]],
    repo_root: Path,
    symbols: list[str],
) -> str:
    """Read impacted files and extract relevant functions that call changed symbols."""
    snippets: list[str] = []

    all_files = {f for files in usages.values() for f in files}
    for file_path in all_files:
        full_path = repo_root / file_path
        try:
            source = full_path.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(source)
        except (SyntaxError, OSError):
            continue

        relevant_nodes = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                node_src = ast.get_source_segment(source, node)
                if node_src and any(sym in node_src for sym in symbols):
                    relevant_nodes.append(node_src)

        if relevant_nodes:
            snippets.append(f"\n### {file_path}\n" + "\n\n".join(relevant_nodes))

    return "\n".join(snippets)


# ─────────────────────────────────────────────
# LLM Calls
# ─────────────────────────────────────────────


def llm_analyze_diff_size(
    client: openai.OpenAI, file_diffs: list[FileDiff]
) -> DiffAnalysis:
    """Step 2: Ask LLM to classify the change as small/big and identify impacted symbols."""

    diff_summary = "\n\n".join(
        f"### {fd.path} (+{fd.additions}/-{fd.deletions})\n```\n{fd.raw_diff[:3000]}\n```"
        for fd in file_diffs
    )

    prompt = f"""You are a senior code reviewer analyzing a git diff.

Analyze the following diff and respond with a JSON object (no markdown, raw JSON only):
{{
  "is_big_change": <true if the change is large, affects function/class signatures, or likely impacts callers elsewhere in the codebase; false for small isolated changes>,
  "reason": "<brief explanation>",
  "affected_symbols": ["<function or class names whose signature changed>"],
}}

Rules:
- is_big_change = true if: many lines changed, public API changed, function signature changed, logic used by other modules changed
- is_big_change = false if: docstring only, internal variable rename, small bug fix with no signature change, new utility function, etc.
- affected_symbols: only include names that CHANGED (not new additions)

Diff:
{diff_summary}
"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.choices[0].message.content.strip()
    # Strip markdown fences if present
    raw = raw.strip("`").lstrip("json").strip()

    try:
        data = json.loads(raw)
        return DiffAnalysis(
            is_big_change=data.get("is_big_change", False),
            reason=data.get("reason", ""),
            affected_symbols=data.get("affected_symbols", []),
            impacted_files=[],
        )
    except json.JSONDecodeError:
        print(
            f"[warn] Could not parse LLM JSON, defaulting to small change.\nRaw: {raw}"
        )
        return DiffAnalysis(
            is_big_change=False,
            reason="parse error",
            affected_symbols=[],
            impacted_files=[],
        )


def llm_review_small_change(
    client: openai.OpenAI, file_diffs: list[FileDiff]
) -> str:
    """Step 3: Simple review for small, isolated changes."""

    diff_text = "\n\n".join(
        f"### {fd.path}\n**Before:**\n```python\n{fd.old_code[:2000]}\n```\n**After:**\n```python\n{fd.new_code[:2000]}\n```"
        for fd in file_diffs
    )

    prompt = f"""You are a senior engineer doing a thorough PR code review.

The following is a small, self-contained change. Review it and provide:
1. **Summary** of what changed
2. **Potential issues** (bugs, edge cases, performance, security)
3. **Code quality** (readability, naming, style)
4. **Suggestions** (concrete improvements with example code if useful)
5. **Verdict**: ✅ Approve / ⚠️ Approve with suggestions / ❌ Request changes

Tech stack context: Python / Django / React / TypeScript

{diff_text}
"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content


def llm_review_big_change(
    client: openai.OpenAI,
    file_diffs: list[FileDiff],
    impacted_snippets: str,
    analysis: DiffAnalysis,
) -> str:
    """Step 4: Deep review including impacted call sites."""

    diff_text = "\n\n".join(
        f"### Changed: {fd.path}\n```diff\n{fd.raw_diff[:3000]}\n```"
        for fd in file_diffs
    )

    prompt = f"""You are a senior engineer doing a thorough PR code review.

This is a **significant change** that may impact other parts of the codebase.

**Why it's significant:** {analysis.reason}
**Changed symbols (API surface):** {', '.join(analysis.affected_symbols) or 'N/A'}

---

## The Diff
{diff_text}

---

## Code That Uses the Changed Symbols
{impacted_snippets if impacted_snippets else "No Python callers found in repo."}

---

Please provide a comprehensive review covering:
1. **Summary** of what changed and its intent
2. **Breaking changes** — are callers still compatible with new signatures?
3. **Impact analysis** — how each impacted file is affected
4. **Bugs / edge cases** in the changed code
5. **Code quality** (readability, naming, Django/Python idioms)
6. **Migration notes** — what else needs updating?
7. **Verdict**: ✅ Approve / ⚠️ Approve with suggestions / ❌ Request changes

Tech stack context: Python / Django / React / TypeScript
"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content


# ─────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────


def run_review(base: str = "HEAD", api_key: Optional[str] = None):
    api_key = api_key or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("❌ OPENAI_API_KEY not set. Export it or pass --api-key.")
        sys.exit(1)

    client = openai.OpenAI(api_key=api_key)

    # ── Step 1: Get the diff ──────────────────────────────────────────────
    print("🔍 Fetching git diff...")
    raw_diff = get_diff(base)
    if not raw_diff.strip():
        print("✅ No changes detected.")
        return

    file_diffs = parse_diff(raw_diff)
    py_diffs = [fd for fd in file_diffs if fd.path.endswith(".py")]

    print(f"   Found {len(file_diffs)} changed file(s) ({len(py_diffs)} Python)")

    # ── Step 2: Classify the change ──────────────────────────────────────
    print("\n🤖 Analyzing change size with LLM...")
    analysis = llm_analyze_diff_size(client, file_diffs)
    print(
        f"   {'🔴 Big change' if analysis.is_big_change else '🟢 Small change'}: {analysis.reason}"
    )

    # ── Step 3 or 4: Review ──────────────────────────────────────────────
    if not analysis.is_big_change:
        print("\n📝 Running focused review (small change)...")
        review = llm_review_small_change(client, file_diffs)
    else:
        # Step 4: AST impact analysis then deep review
        repo_root = get_repo_root()

        all_impacted: dict[str, list[str]] = {}
        if analysis.affected_symbols and py_diffs:
            print(f"\n🌳 Running AST analysis for: {analysis.affected_symbols}")
            for fd in py_diffs:
                # Also check symbols that changed within each file
                file_symbols = extract_changed_symbols(fd.old_code, fd.new_code)
                symbols_to_check = list(set(analysis.affected_symbols + file_symbols))
                if symbols_to_check:
                    usages = find_usages_in_repo(symbols_to_check, repo_root, fd.path)
                    for sym, files in usages.items():
                        all_impacted.setdefault(sym, []).extend(files)
                    impacted_flat = [f for files in usages.values() for f in files]
                    if impacted_flat:
                        print(f"   Found usages in: {', '.join(set(impacted_flat))}")

        analysis.impacted_files = list(
            {f for files in all_impacted.values() for f in files}
        )

        impacted_snippets = ""
        if all_impacted:
            impacted_snippets = collect_impacted_file_snippets(
                all_impacted, repo_root, analysis.affected_symbols
            )

        print("\n📝 Running deep review (big change + impact analysis)...")
        review = llm_review_big_change(client, file_diffs, impacted_snippets, analysis)

    # ── Output ────────────────────────────────────────────────────────────
    print("\n" + "═" * 60)
    print("  PR REVIEW REPORT")
    print("═" * 60)
    print(review)
    print("═" * 60)

    # Optionally save to file
    output_path = Path("pr_review_output.md")
    output_path.write_text(f"# PR Review\n\n{review}\n", encoding="utf-8")
    print(f"\n💾 Review saved to: {output_path.resolve()}")


# ─────────────────────────────────────────────
# CLI Entry Point
# ─────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="AI-powered PR code review tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  pr-review                        # Review changes vs HEAD
  pr-review --base main            # Review changes vs main branch
  pr-review --base origin/main     # Review changes vs remote main
  pr-review --base abc123          # Review changes vs a specific commit

Environment:
  OPENAI_API_KEY   Your OPENAI API key (required)
        """,
    )
    parser.add_argument(
        "--base",
        default="HEAD",
        help="Git ref to diff against (branch, tag, or commit SHA). Default: HEAD",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="OPENAI API key (overrides OPENAI_API_KEY env var)",
    )
    args = parser.parse_args()
    run_review(base=args.base, api_key=args.api_key)


if __name__ == "__main__":
    main()
