import json
import logging
import os
import re
from typing import Tuple


class BenchmarkMetadataLoader:
    """Loads per-commit benchmark metadata and extracts the project ID and Docker image."""

    def __init__(self, benchmark_folder: str) -> None:
        self.benchmark_folder = benchmark_folder

    def load_metadata(self, commit_id: str) -> Tuple[str, str]:
        """Load project_id and docker_image from benchmark metadata."""
        metadata_path = os.path.join(self.benchmark_folder, f"{commit_id}.json")
        if not os.path.isfile(metadata_path):
            logging.error(f"[{commit_id}] Missing benchmark metadata JSON file: {metadata_path}")
            raise FileNotFoundError(f"Commit JSON file not found for commit {commit_id} at {metadata_path}")
        try:
            with open(metadata_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            project_id = data.get("project")
            if not project_id or not isinstance(project_id, str):
                logging.error(f"[{commit_id}] 'project' field missing or invalid in {metadata_path}")
                raise ValueError(f"Invalid or missing 'project' field in commit JSON for {commit_id}")
            
            breaking_cmd = data.get("breakingUpdateReproductionCommand")
            if not breaking_cmd or not isinstance(breaking_cmd, str):
                logging.error(f"[{commit_id}] 'breakingUpdateReproductionCommand' missing or invalid in {metadata_path}")
                raise ValueError(f"Invalid or missing 'breakingUpdateReproductionCommand' in commit JSON for {commit_id}")
            
            # Extract Docker image from command: "docker run <image>"
            match = re.search(r'docker run\s+(\S+)', breaking_cmd)
            if not match:
                logging.error(f"[{commit_id}] Could not extract Docker image from command: {breaking_cmd}")
                raise ValueError(f"Could not parse Docker image from breakingUpdateReproductionCommand")
            
            docker_image = match.group(1).strip()
            return project_id.strip(), docker_image
        except (json.JSONDecodeError, ValueError) as e:
            logging.error(f"[{commit_id}] Failed to parse metadata {metadata_path}: {e}")
            raise RuntimeError(f"Failed to parse commit JSON for {commit_id}: {e}")

    def load_project_id(self, commit_id: str) -> str:
        """Backward compatibility method."""
        project_id, _ = self.load_metadata(commit_id)
        return project_id
