# llm/base.py
from abc import ABC, abstractmethod
from typing import Any, List


class BaseLLMClient(ABC):
    @abstractmethod
    def analyze_diff_size(self, file_diffs: List[Any]) -> Any:
        pass

    @abstractmethod
    def review_small_change(self, file_diffs: List[Any]) -> str:
        pass

    @abstractmethod
    def review_big_change(self, file_diffs: List[Any], impacted_snippets: str, analysis: Any) -> str:
        pass
