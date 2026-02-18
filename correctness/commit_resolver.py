import json
import logging
import sys
from typing import List, Optional


class CommitResolver:
    """Resolves commit IDs from a main JSON file and validates specific commits."""

    def __init__(self, main_json_path: str) -> None:
        self.main_json_path = main_json_path

    def _read_commits(self) -> List[str]:
        with open(self.main_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return list(data.keys())
        if isinstance(data, list):
            return [item.get("commitId") for item in data if isinstance(item, dict) and item.get("commitId")]
        logging.error("Unsupported main JSON structure: expected dict or list")
        sys.exit(1)

    def resolve(self, specific_commit: Optional[str] = None) -> List[str]:
        commits = self._read_commits()
        if specific_commit:
            if specific_commit in commits:
                logging.info(f"Processing specific commit: {specific_commit}")
                return [specific_commit]
            logging.error(
                f"Specific commit '{specific_commit}' not found in main JSON. Available commitIds: {len(commits)}"
            )
            sys.exit(2)
        logging.info(f"Found {len(commits)} commitIds in main JSON")
        return commits
