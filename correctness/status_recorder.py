import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Dict, Any


@dataclass
class StatusRecorderContext:
    engine: str


class CompilationStatusRecorder:
    """Persists per-commit compilation and classifier metadata into a central JSON file."""

    def __init__(self, context: StatusRecorderContext) -> None:
        self.context = context

    def save_status(
        self,
        commit_id: str,
        combined_commit_folder: str,
        compile_success: bool,
    ) -> None:
        """Save compilation status to central JSON file.

        Structure per commit:
        {
          "compile_success": bool,
          "errorsByFileCount"?: int,
          "errorFiles"?: [str],
          "originalTemplateInputs"?: [str]
        }
        """
        status_data: Dict[str, Any] = {
            "compile_success": compile_success
        }

        # Optionally enrich with number of files and file names in breaking-classifier-report.json
        try:
            classifier_report_path = os.path.join(
                combined_commit_folder, "rules", "breaking-classifier-report.json"
            )
            if os.path.isfile(classifier_report_path):
                with open(classifier_report_path, "r", encoding="utf-8") as f:
                    report = json.load(f)
                errors_by_file = report.get("errorsByFile", [])
                if isinstance(errors_by_file, list):
                    status_data["errorsByFileCount"] = len(errors_by_file)
                    file_names = []
                    for entry in errors_by_file:
                        if isinstance(entry, dict):
                            path = entry.get("filePath")
                            if isinstance(path, str):
                                file_names.append(path)
                    if file_names:
                        status_data["errorFiles"] = file_names
        except Exception as e:
            logging.warning(f"[{commit_id}] Failed to read breaking-classifier-report.json: {e}")

        # Also record the original input resources found in the base template Main.java
        try:
            base_template_name = f"{self.context.engine}-base-template"
            main_java_path = os.path.join(
                combined_commit_folder,
                "rules",
                base_template_name,
                "src",
                "main",
                "java",
                "github",
                "chains",
                "Main.java",
            )
            if os.path.isfile(main_java_path):
                with open(main_java_path, "r", encoding="utf-8") as f:
                    main_src = f.read()
                matches = re.findall(r'launcher\.addInputResource\("([^"]+)"\);', main_src)
                if matches:
                    status_data["originalTemplateInputs"] = matches
        except Exception as e:
            logging.warning(f"[{commit_id}] Failed to read original template inputs: {e}")

        # Central status file in combined_output_folder root
        central_status_file = os.path.join(os.path.dirname(combined_commit_folder), "compilation-results.json")

        try:
            # Read existing data if file exists
            if os.path.isfile(central_status_file):
                with open(central_status_file, "r", encoding="utf-8") as f:
                    all_status = json.load(f)
            else:
                all_status = {}

            # Update with current commit
            all_status[commit_id] = status_data

            # Write back to file
            with open(central_status_file, "w", encoding="utf-8") as f:
                json.dump(all_status, f, indent=2)

            logging.info(f"[{commit_id}] Compilation status saved to {central_status_file}")
        except Exception as e:
            logging.error(f"[{commit_id}] Failed to save compilation status: {e}")

