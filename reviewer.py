#!/usr/bin/env python3
"""Orchestrator for the PR review tool — uses helper modules for tasks."""

import os
import sys
import json
from pathlib import Path
from dotenv import load_dotenv
from typing import Optional
import openai

load_dotenv()

from git import get_diff, get_repo_root
from diff_parser import parse_diff, FileDiff
from ast_analyzer import (
    DiffAnalysis,
    extract_changed_symbols,
    find_usages_in_repo,
    collect_impacted_file_snippets,
)
from llm.openai import (
    llm_analyze_diff_size,
    llm_review_small_change,
    llm_review_big_change,
)

# Import CLI helper to parse arguments (keeps main in this module)
from cli import parse_cli_args


def run_review(base: str = "HEAD", api_key: Optional[str] = None):
    api_key = api_key or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("❌ OPENAI_API_KEY not set. Export it or pass --api-key.")
        sys.exit(1)

    client = openai.OpenAI(api_key=api_key)

    print("🔍 Fetching git diff...")
    raw_diff = get_diff(base)
    if not raw_diff.strip():
        print("✅ No changes detected.")
        return

    file_diffs = parse_diff(raw_diff)
    py_diffs = [fd for fd in file_diffs if fd.path.endswith(".py")]

    print(f"   Found {len(file_diffs)} changed file(s) ({len(py_diffs)} Python)")

    print("\n🤖 Analyzing change size with LLM...")
    analysis = llm_analyze_diff_size(client, file_diffs)
    print(f"   {'🔴 Big change' if analysis.is_big_change else '🟢 Small change'}: {analysis.reason}")

    if not analysis.is_big_change:
        print("\n📝 Running focused review (small change)...")
        review = llm_review_small_change(client, file_diffs)
    else:
        repo_root = get_repo_root()
        all_impacted: dict[str, list[str]] = {}

        if py_diffs:
            print(f"\n🌳 Running AST analysis...")
            for fd in py_diffs:
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

        analysis.impacted_files = list({f for files in all_impacted.values() for f in files})

        impacted_snippets = ""
        if all_impacted:
            all_symbols = list(all_impacted.keys())
            impacted_snippets = collect_impacted_file_snippets(all_impacted, repo_root, all_symbols)

        print("\n📝 Running deep review (big change + impact analysis)...")
        review = llm_review_big_change(client, file_diffs, impacted_snippets, analysis)

    print("\n" + "═" * 60)
    print("  PR REVIEW REPORT")
    print("═" * 60)
    print(review)
    print("═" * 60)

    output_path = Path("pr_review_output.md")
    output_path.write_text(f"# PR Review\n\n{review}\n", encoding="utf-8")
    print(f"\n💾 Review saved to: {output_path.resolve()}")


def main():
    args = parse_cli_args()
    run_review(base=args.base, api_key=args.api_key)


if __name__ == "__main__":
    main()

