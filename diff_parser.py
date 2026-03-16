from dataclasses import dataclass
from typing import Optional, List


@dataclass
class FileDiff:
    path: str
    old_code: str
    new_code: str
    additions: int
    deletions: int
    raw_diff: str


def parse_diff(raw_diff: str) -> List[FileDiff]:
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
    return [f for f in files if f.path]
