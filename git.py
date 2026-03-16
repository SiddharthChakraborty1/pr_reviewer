import subprocess
from pathlib import Path


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
