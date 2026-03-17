# PR Review CLI

pypi -> https://pypi.org/project/git-ai-pr-review/0.1.0/

An AI-powered code review tool that analyzes your git diff and routes it through the right level of LLM scrutiny.

## How it works

```
git diff → LLM classifies change size
              │
              ├── Small change ──→ Focused review (old vs new code)
              │
              └── Big change  ──→ AST analysis finds all callers
                                   └──→ Deep review (diff + impacted files)
```

## Install

```bash
pip install anthropic

# Run directly
python pr_review.py

# Or install as a CLI command
pip install -e .
pr-review
```

## Usage

```bash
# Set your API key
export ANTHROPIC_API_KEY=sk-ant-...

# Review changes vs HEAD (default — unstaged/staged changes)
pr-review

# Review feature branch against main
pr-review --base main

# Review against remote main
pr-review --base origin/main

# Review against a specific commit
pr-review --base abc1234
```

## Output

- Prints the review to stdout
- Saves a `pr_review_output.md` file in the current directory

## What the LLM looks for

**Small change review:**
- Summary of what changed
- Bugs & edge cases
- Code quality & naming
- Concrete improvement suggestions
- Final verdict (approve / approve with suggestions / request changes)

**Big change review (+ AST impact analysis):**
- Everything above, plus:
- Breaking change detection (are callers still compatible?)
- Per-file impact analysis of all call sites found via AST
- Migration notes — what else needs updating

## Notes

- AST analysis is Python-only (uses the `ast` stdlib module)
- Skips `.venv` and `node_modules` directories
- The diff size LLM call uses `claude-opus-4-6` for accuracy
- Reviews are also saved to `pr_review_output.md`
