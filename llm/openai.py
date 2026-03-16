import json
from typing import List
import openai

from diff_parser import FileDiff
from ast_analyzer import DiffAnalysis


def llm_analyze_diff_size(client: openai.OpenAI, file_diffs: List[FileDiff]) -> DiffAnalysis:
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
        print(f"[warn] Could not parse LLM JSON, defaulting to small change.\nRaw: {raw}")
        return DiffAnalysis(is_big_change=False, reason="parse error", affected_symbols=[], impacted_files=[])


def llm_review_small_change(client: openai.OpenAI, file_diffs: List[FileDiff]) -> str:
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
    file_diffs: List[FileDiff],
    impacted_snippets: str,
    analysis: DiffAnalysis,
) -> str:
    diff_text = "\n\n".join(
        f"### Changed: {fd.path}\n```diff\n{fd.raw_diff[:3000]}\n```" for fd in file_diffs
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
