import json
from typing import List

import openai

from ast_analyzer import DiffAnalysis
from diff_parser import FileDiff
from llm.prompts import (
    generate_analyze_diff_prompt,
    generate_big_change_prompt,
    generate_small_change_prompt,
)


def llm_analyze_diff_size(
    client: openai.OpenAI, file_diffs: List[FileDiff]
) -> DiffAnalysis:
    diff_summary = "\n\n".join(
        f"### {fd.path} (+{fd.additions}/-{fd.deletions})\n```\n{fd.raw_diff[:3000]}\n```"
        for fd in file_diffs
    )

    prompt = generate_analyze_diff_prompt(diff_summary)

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    content = response.choices[0].message.content or ""
    raw = content.strip()
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


def llm_review_small_change(client: openai.OpenAI, file_diffs: List[FileDiff]) -> str:
    diff_text = "\n\n".join(
        f"### {fd.path}\n**Before:**\n```python\n{fd.old_code[:2000]}\n```\n**After:**\n```python\n{fd.new_code[:2000]}\n```"
        for fd in file_diffs
    )

    prompt = generate_small_change_prompt(diff_text)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content or ""


def llm_review_big_change(
    client: openai.OpenAI,
    file_diffs: List[FileDiff],
    impacted_snippets: str,
    analysis: DiffAnalysis,
) -> str:
    diff_text = "\n\n".join(
        f"### Changed: {fd.path}\n```diff\n{fd.raw_diff[:3000]}\n```"
        for fd in file_diffs
    )

    prompt = generate_big_change_prompt(analysis, diff_text, impacted_snippets)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content or ""
