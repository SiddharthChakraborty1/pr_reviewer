"""Prompt generation helpers for LLM calls.

Each function returns a fully-formed prompt string given the required
pieces of context (diff summaries, analysis, impacted snippets, etc.).
This keeps prompt text centralized and easier to modify or test.
"""

from typing import Any


def generate_analyze_diff_prompt(diff_summary: str) -> str:
    return f"""You are a senior code reviewer analyzing a git diff.

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


def generate_small_change_prompt(diff_text: str) -> str:
    return f"""You are a senior engineer doing a thorough PR code review.

The following is a small, self-contained change. Review it and provide:
1. **Summary** of what changed
2. **Potential issues** (bugs, edge cases, performance, security)
3. **Code quality** (readability, naming, style)
4. **Suggestions** (concrete improvements with example code if useful)
5. **Verdict**: ✅ Approve / ⚠️ Approve with suggestions / ❌ Request changes

Tech stack context: Python / Django / React / TypeScript

{diff_text}
"""


def generate_big_change_prompt(
    analysis: Any, diff_text: str, impacted_snippets: str
) -> str:
    reason = getattr(analysis, "reason", "")
    affected = getattr(analysis, "affected_symbols", []) or []
    affected_list = ", ".join(affected) if affected else "N/A"

    return f"""You are a senior engineer doing a thorough PR code review.

This is a **significant change** that may impact other parts of the codebase.

**Why it's significant:** {reason}
**Changed symbols (API surface):** {affected_list}

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
