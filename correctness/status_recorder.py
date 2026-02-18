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
          "rule_compile_success": bool,
          "errorsByFileCount"?: int,
          "errorFiles"?: [str],
          "originalTemplateInputs"?: [str],
          "adjustedTemplateInputs"?: [str],
          "failureCategory"?: str
        }
        """
        status_data: Dict[str, Any] = {
            "rule_compile_success": compile_success
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

        # And record the adjusted input resources from the adjusted template Main.java
        try:
            adjusted_template_name = f"{self.context.engine}-base-template-adjusted"
            adjusted_main_path = os.path.join(
                combined_commit_folder,
                "rules",
                adjusted_template_name,
                "src",
                "main",
                "java",
                "github",
                "chains",
                "Main.java",
            )
            if os.path.isfile(adjusted_main_path):
                with open(adjusted_main_path, "r", encoding="utf-8") as f:
                    adjusted_src = f.read()
                adjusted_matches = re.findall(
                    r'launcher\.addInputResource\("([^"]+)"\);', adjusted_src
                )
                if adjusted_matches:
                    status_data["adjustedTemplateInputs"] = adjusted_matches
        except Exception as e:
            logging.warning(f"[{commit_id}] Failed to read adjusted template inputs: {e}")

        # Derive failure category from post-modification test log (if present)
        try:
            test_log_path = os.path.join(
                combined_commit_folder,
                "after-modifications.log",
            )
            if os.path.isfile(test_log_path):
                with open(test_log_path, "r", encoding="utf-8") as f:
                    log_text = f.read()

                # Ordered patterns: first match wins
                failure_patterns = [
                    # JAVA_VERSION_FAILURE
                    (
                        re.compile(
                            r"(?i)(class file has wrong version (\d+\.\d+), should be (\d+\.\d+))"
                        ),
                        "JAVA_VERSION_FAILURE",
                    ),
                    # TEST_FAILURE
                    (
                        re.compile(
                            r"(?i)(\[ERROR] Tests run:|There are test failures|There were test failures|"
                            r"Failed to execute goal org\.apache\.maven\.plugins:maven-surefire-plugin)"
                        ),
                        "TEST_FAILURE",
                    ),
                    # WERROR_FAILURE
                    (
                        re.compile(
                            r"(?i)(warnings found and -Werror specified)"
                        ),
                        "WERROR_FAILURE",
                    ),
                    # COMPILATION_FAILURE
                    (
                        re.compile(
                            r"(?i)(COMPILATION ERROR|Failed to execute goal io\.takari\.maven\.plugins:takari-lifecycle-plugin.*?:compile)"
                            r"|Exit code: COMPILATION_ERROR"
                        ),
                        "COMPILATION_FAILURE",
                    ),
                    # BUILD_SUCCESS
                    (
                        re.compile(r"(?i)(BUILD SUCCESS)"),
                        "BUILD_SUCCESS",
                    ),
                    # ENFORCER_FAILURE
                    (
                        re.compile(
                            r"(?i)(Failed to execute goal org\.apache\.maven\.plugins:maven-enforcer-plugin|"
                            r"Failed to execute goal org\.jenkins-ci\.tools:maven-hpi-plugin)"
                        ),
                        "ENFORCER_FAILURE",
                    ),
                    # DEPENDENCY_RESOLUTION_FAILURE
                    (
                        re.compile(
                            r"(?i)(Could not resolve dependencies|\[ERROR] Some problems were encountered while processing the POMs|"
                            r"\[ERROR] .*?The following artifacts could not be resolved)"
                        ),
                        "DEPENDENCY_RESOLUTION_FAILURE",
                    ),
                    # DEPENDENCY_LOCK_FAILURE
                    (
                        re.compile(
                            r"(?i)(Failed to execute goal se\.vandmo:dependency-lock-maven-plugin:.*?:check)"
                        ),
                        "DEPENDENCY_LOCK_FAILURE",
                    ),
                ]

                failure_category = None
                for pattern, name in failure_patterns:
                    if pattern.search(log_text):
                        failure_category = name
                        break

                if failure_category is None:
                    failure_category = "UNKNOWN"

                status_data["failureCategory"] = failure_category
        except Exception as e:
            logging.warning(f"[{commit_id}] Failed to derive failure category from test log: {e}")

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

