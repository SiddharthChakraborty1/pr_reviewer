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
    affected_symbols: list[str]  # functions/classes/variables that changed
    impacted_files: list[str]  # files that might be affected


# ─────────────────────────────────────────────
# Git Utilities
# ─────────────────────────────────────────────


def run_git(*args) -> str:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
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


def _get_assign_name(target) -> Optional[str]:
    """Extract name string from an assignment target node."""
    if isinstance(target, ast.Name):
        return target.id
    elif isinstance(target, ast.Attribute):
        return target.attr
    return None


def _ast_value_to_str(node) -> str:
    """Serialize an AST value node to a comparable string."""
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def extract_changed_symbols(old_code: str, new_code: str) -> list[str]:
    """
    Find symbols that changed between old and new code. Covers:
      - Function / async function signature changes
      - Class variable additions, removals, renames, value changes
      - Module-level constant additions, removals, renames, value changes

    Returns a flat list of symbol strings, e.g.:
      ["create_user", "UserConfig.MAX_LOGIN_ATTEMPTS", "MAX_RETRIES"]
    """
    changed: list[str] = []

    def parse(source: str) -> Optional[ast.Module]:
        try:
            return ast.parse(source)
        except SyntaxError:
            return None

    old_tree = parse(old_code)
    new_tree = parse(new_code)

    if not old_tree or not new_tree:
        return changed

    # ── 1. Function signature changes ─────────────────────────────────────
    def get_func_sigs(tree: ast.Module) -> dict[str, str]:
        sigs: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = [a.arg for a in node.args.args]
                sigs[node.name] = f"{node.name}({', '.join(args)})"
        return sigs

    old_funcs = get_func_sigs(old_tree)
    new_funcs = get_func_sigs(new_tree)

    for name, sig in new_funcs.items():
        if name in old_funcs and old_funcs[name] != sig:
            changed.append(name)

    # ── 2. Class variable changes ──────────────────────────────────────────
    def get_class_vars(tree: ast.Module) -> dict[str, dict[str, str]]:
        """Returns {class_name: {var_name: value_str}}"""
        result: dict[str, dict[str, str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                vars: dict[str, str] = {}
                for item in node.body:
                    # Simple assignment: VAR = value
                    if isinstance(item, ast.Assign):
                        for target in item.targets:
                            name = _get_assign_name(target)
                            if name:
                                vars[name] = _ast_value_to_str(item.value)
                    # Annotated assignment: VAR: type = value
                    elif isinstance(item, ast.AnnAssign) and item.value:
                        name = _get_assign_name(item.target)
                        if name:
                            vars[name] = _ast_value_to_str(item.value)
                result[node.name] = vars
        return result

    old_class_vars = get_class_vars(old_tree)
    new_class_vars = get_class_vars(new_tree)

    for class_name, new_vars in new_class_vars.items():
        old_vars = old_class_vars.get(class_name, {})
        all_var_names = set(old_vars) | set(new_vars)
        for var_name in all_var_names:
            symbol = f"{class_name}.{var_name}"
            if var_name not in old_vars:
                # New variable added — could be a rename, flag it
                changed.append(symbol)
            elif var_name not in new_vars:
                # Variable removed — definitely a breaking change
                changed.append(symbol)
            elif old_vars[var_name] != new_vars[var_name]:
                # Value changed
                changed.append(symbol)

    # ── 3. Module-level constant changes ──────────────────────────────────
    def get_module_constants(tree: ast.Module) -> dict[str, str]:
        """Returns {constant_name: value_str} for top-level assignments."""
        result: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    name = _get_assign_name(target)
                    # Convention: treat ALL_CAPS names as constants
                    if name and name.isupper():
                        result[name] = _ast_value_to_str(node.value)
            elif isinstance(node, ast.AnnAssign) and node.value:
                name = _get_assign_name(node.target)
                if name and name.isupper():
                    result[name] = _ast_value_to_str(node.value)
        return result

    old_consts = get_module_constants(old_tree)
    new_consts = get_module_constants(new_tree)

    all_const_names = set(old_consts) | set(new_consts)
    for name in all_const_names:
        if name not in old_consts:
            changed.append(name)
        elif name not in new_consts:
            changed.append(name)
        elif old_consts[name] != new_consts[name]:
            changed.append(name)

    return changed


def _collect_aliases(tree: ast.Module) -> dict[str, str]:
    """
    First pass: collect variable aliases for classes.
    e.g. `config = UserConfig` → {"config": "UserConfig"}
    Also handles `from module import SomeClass as alias`.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        # Direct assignment alias: config = UserConfig
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and isinstance(node.value, ast.Name):
                    aliases[target.id] = node.value.id
        # Import alias: from module import SomeClass as alias
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name

    return aliases


def find_usages_in_repo(
    symbols: list[str], repo_root: Path, exclude_file: str
) -> dict[str, list[str]]:
    """
    For each symbol, find Python files in the repo that use it.
    Handles:
      - Function calls: create_user(...)
      - Method calls: obj.create_user(...)
      - Direct attribute access: UserConfig.MAX_LOGIN_ATTEMPTS
      - Alias access: cfg = UserConfig; cfg.MAX_LOGIN_ATTEMPTS
      - Direct name usage: if x > MAX_RETRIES:
      - Imports: from module import MAX_RETRIES

    symbols can be plain names ("create_user", "MAX_RETRIES")
    or qualified ("UserConfig.MAX_LOGIN_ATTEMPTS").

    Returns {symbol: [file_path, ...]}
    """
    usages: dict[str, list[str]] = {s: [] for s in symbols}

    # Split qualified symbols into (class_name, attr_name) pairs
    # e.g. "UserConfig.MAX_LOGIN_ATTEMPTS" → ("UserConfig", "MAX_LOGIN_ATTEMPTS")
    qualified: dict[str, tuple[str, str]] = {}
    plain: set[str] = set()
    for sym in symbols:
        if "." in sym:
            parts = sym.split(".", 1)
            qualified[sym] = (parts[0], parts[1])
        else:
            plain.add(sym)

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

        rel = str(py_file.relative_to(repo_root))

        # Pass 1: collect aliases in this file
        aliases = _collect_aliases(tree)
        # Reverse alias map: original_name → [alias1, alias2, ...]
        reverse_aliases: dict[str, list[str]] = {}
        for alias, original in aliases.items():
            reverse_aliases.setdefault(original, []).append(alias)

        def record(sym: str):
            if rel not in usages[sym]:
                usages[sym].append(rel)

        # Pass 2: walk AST and match usages
        for node in ast.walk(tree):

            # ── Function/method calls (plain symbols) ──────────────────
            if isinstance(node, ast.Call) and plain:
                func = node.func
                name = None
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                if name and name in plain:
                    record(name)

            # ── Attribute access (qualified + alias) ───────────────────
            elif isinstance(node, ast.Attribute):
                attr_name = node.attr
                # Direct: UserConfig.MAX_LOGIN_ATTEMPTS
                if isinstance(node.value, ast.Name):
                    obj_name = node.value.id
                    full = f"{obj_name}.{attr_name}"
                    if full in qualified:
                        record(full)
                    # Alias: cfg.MAX_LOGIN_ATTEMPTS where cfg = UserConfig
                    if obj_name in aliases:
                        original_class = aliases[obj_name]
                        full_via_alias = f"{original_class}.{attr_name}"
                        if full_via_alias in qualified:
                            record(full_via_alias)

            # ── Plain name usage (module-level constants) ──────────────
            elif isinstance(node, ast.Name) and node.id in plain:
                record(node.id)

            # ── Import statements ──────────────────────────────────────
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imported = alias.asname or alias.name
                    if imported in plain:
                        record(imported)

    return usages


def collect_impacted_file_snippets(
    usages: dict[str, list[str]],
    repo_root: Path,
    symbols: list[str],
) -> str:
    """
    Read impacted files and extract relevant code blocks that reference
    changed symbols. Extracts functions, classes, and also module-level
    statements that directly reference the symbol.
    """
    snippets: list[str] = []

    # Flatten symbol names for string matching (handle qualified names)
    # "UserConfig.MAX_LOGIN_ATTEMPTS" → also match "MAX_LOGIN_ATTEMPTS"
    flat_symbols = set()
    for sym in symbols:
        flat_symbols.add(sym)
        if "." in sym:
            flat_symbols.add(sym.split(".", 1)[1])

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
            # Functions and classes that contain a reference to the symbol
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                node_src = ast.get_source_segment(source, node)
                if node_src and any(sym in node_src for sym in flat_symbols):
                    relevant_nodes.append(node_src)

        # Also grab module-level assignments/expressions that reference the symbol
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue  # already handled above
            node_src = ast.get_source_segment(source, node)
            if node_src and any(sym in node_src for sym in flat_symbols):
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
  "is_big_change": <true if the change is large, affects function/class signatures, class variables, module constants, or likely impacts callers elsewhere in the codebase; false for small isolated changes>,
  "reason": "<brief explanation>",
  "affected_symbols": ["<function names, class.variable names, or CONSTANT names that changed>"]
}}

Rules:
- is_big_change = true if: many lines changed, public API changed, function signature changed, class variable renamed or removed, module-level constant renamed or removed, logic used by other modules changed
- is_big_change = false if: docstring only, purely internal change with no external references, new addition that nothing depends on yet
- affected_symbols: include function names, qualified class variables (e.g. "UserConfig.MAX_LOGIN_ATTEMPTS"), and module constants (e.g. "MAX_RETRIES") that changed or were renamed

Diff:
{diff_summary}
"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.choices[0].message.content.strip()
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


def llm_review_small_change(client: openai.OpenAI, file_diffs: list[FileDiff]) -> str:
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
{impacted_snippets if impacted_snippets else "No usages found in repo."}

---

Please provide a comprehensive review covering:
1. **Summary** of what changed and its intent
2. **Breaking changes** — are callers still compatible? Check function calls, attribute accesses, and constant usages
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
        repo_root = get_repo_root()
        all_impacted: dict[str, list[str]] = {}

        if py_diffs:
            print(f"\n🌳 Running AST analysis...")
            for fd in py_diffs:
                # Combine LLM-identified symbols with AST-detected ones
                ast_symbols = extract_changed_symbols(fd.old_code, fd.new_code)
                symbols_to_check = list(set(analysis.affected_symbols + ast_symbols))

                if symbols_to_check:
                    print(f"   Symbols to check: {symbols_to_check}")
                    usages = find_usages_in_repo(symbols_to_check, repo_root, fd.path)
                    for sym, files in usages.items():
                        all_impacted.setdefault(sym, []).extend(files)
                    impacted_flat = [f for files in usages.values() for f in files]
                    if impacted_flat:
                        print(f"   Found usages in: {', '.join(set(impacted_flat))}")
                    else:
                        print(f"   No usages found in other files.")

        analysis.impacted_files = list(
            {f for files in all_impacted.values() for f in files}
        )

        impacted_snippets = ""
        if all_impacted:
            all_symbols = list(all_impacted.keys())
            impacted_snippets = collect_impacted_file_snippets(
                all_impacted,
                repo_root,
                all_symbols,  # pass all symbols, not just LLM ones
            )

        print("\n📝 Running deep review (big change + impact analysis)...")
        review = llm_review_big_change(client, file_diffs, impacted_snippets, analysis)

    # ── Output ────────────────────────────────────────────────────────────
    print("\n" + "═" * 60)
    print("  PR REVIEW REPORT")
    print("═" * 60)
    print(review)
    print("═" * 60)

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
