import argparse
from dotenv import load_dotenv

load_dotenv()


def parse_cli_args():
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
  OPENAI_API_KEY   Your OPENAI_API key (required)
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
    return parser.parse_args()
