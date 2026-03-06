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

        # Also record the original input resources found anywhere in the base template
        try:
            base_template_name = f"{self.context.engine}-base-template"
            base_src_root = os.path.join(
                combined_commit_folder,
                "rules",
                base_template_name,
                "src",
                "main",
                "java",
            )
            original_inputs = []
            if os.path.isdir(base_src_root):
                for root, _, files in os.walk(base_src_root):
                    for name in files:
                        if not name.endswith(".java"):
                            continue
                        java_path = os.path.join(root, name)
                        try:
                            with open(java_path, "r", encoding="utf-8") as f:
                                src = f.read()
                        except Exception:
                            continue

                        if self.context.engine == "spoon":
                            matches = re.findall(r'\w+\.addInputResource\("([^"]+)"\);', src)
                        else:
                            # For non-Spoon engines we don't try to infer original inputs
                            # from code; these will instead be reported via input-mapping.json
                            # if available.
                            matches = []
                        original_inputs.extend(matches)
            if original_inputs:
                # Preserve order but remove duplicates
                seen = set()
                deduped = []
                for path in original_inputs:
                    if path not in seen:
                        seen.add(path)
                        deduped.append(path)
                status_data["originalTemplateInputs"] = deduped
        except Exception as e:
            logging.warning(f"[{commit_id}] Failed to read original template inputs: {e}")

        # And record the adjusted input resources from anywhere in the adjusted template
        try:
            adjusted_template_name = f"{self.context.engine}-base-template-adjusted"
            adjusted_src_root = os.path.join(
                combined_commit_folder,
                "rules",
                adjusted_template_name,
                "src",
                "main",
                "java",
            )
            adjusted_inputs = []
            if os.path.isdir(adjusted_src_root):
                for root, _, files in os.walk(adjusted_src_root):
                    for name in files:
                        if not name.endswith(".java"):
                            continue
                        java_path = os.path.join(root, name)
                        try:
                            with open(java_path, "r", encoding="utf-8") as f:
                                src = f.read()
                        except Exception:
                            continue

                        if self.context.engine == "spoon":
                            matches = re.findall(
                                r'\w+\.addInputResource\("([^"]+)"\);', src
                            )
                        else:
                            # For non-Spoon engines adjusted inputs will be derived from
                            # the dedicated input-mapping.json if present.
                            matches = []
                        adjusted_inputs.extend(matches)
            if adjusted_inputs:
                seen = set()
                deduped = []
                for path in adjusted_inputs:
                    if path not in seen:
                        seen.add(path)
                        deduped.append(path)
                status_data["adjustedTemplateInputs"] = deduped
        except Exception as e:
            logging.warning(f"[{commit_id}] Failed to read adjusted template inputs: {e}")

        # For JavaParser (and other non-Spoon engines), also record explicit
        # original/adjusted input mappings, if the template adjuster produced them.
        if self.context.engine != "spoon":
            try:
                mapping_path = os.path.join(
                    combined_commit_folder,
                    "rules",
                    "input-mapping.json",
                )
                if os.path.isfile(mapping_path):
                    with open(mapping_path, "r", encoding="utf-8") as f:
                        mapping_data = json.load(f)
                    mappings = mapping_data.get("mappings", [])
                    if isinstance(mappings, list) and mappings:
                        # Expose flattened original/adjusted lists for convenience
                        originals = []
                        adjusted = []
                        for entry in mappings:
                            if not isinstance(entry, dict):
                                continue
                            o = entry.get("original")
                            a = entry.get("adjusted")
                            if isinstance(o, str):
                                originals.append(o)
                            if isinstance(a, str):
                                adjusted.append(a)

                        if originals:
                            seen = set()
                            deduped = []
                            for p in originals:
                                if p not in seen:
                                    seen.add(p)
                                    deduped.append(p)
                            status_data["originalTemplateInputs"] = deduped

                        if adjusted:
                            seen = set()
                            deduped = []
                            for p in adjusted:
                                if p not in seen:
                                    seen.add(p)
                                    deduped.append(p)
                            status_data["adjustedTemplateInputs"] = deduped
            except Exception as e:
                logging.warning(f"[{commit_id}] Failed to read explicit input mappings: {e}")

        # Derive failure category:
        # - If rule compilation failed, check if it's NO_RULES, otherwise use RULES_COMPILE_ERROR
        # - If rule compilation succeeded, derive from post-modification test log
        failure_category = None

        # 1) If rule compilation failed, check Spoon build log for NO_RULES marker
        if not compile_success:
            try:
                build_log_path = os.path.join(
                    combined_commit_folder,
                    f"{self.context.engine}-build.log",
                )
                if os.path.isfile(build_log_path):
                    with open(build_log_path, "r", encoding="utf-8") as f:
                        build_log_text = f.read()
                    if f"[{commit_id}] NO_RULES" in build_log_text:
                        failure_category = "NO_RULES"
                    else:
                        # Rule compilation failed but it's not NO_RULES
                        failure_category = "RULES_COMPILE_ERROR"
                else:
                    # Rule compilation failed but no build log found
                    failure_category = "RULES_COMPILE_ERROR"
            except Exception as e:
                logging.warning(f"[{commit_id}] Failed to inspect build log for NO_RULES: {e}")
                # Rule compilation failed but couldn't check build log
                failure_category = "RULES_COMPILE_ERROR"

        # 2) If rule compilation succeeded, try to derive from post-modification test log (if present)
        if failure_category is None:
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

                    for pattern, name in failure_patterns:
                        if pattern.search(log_text):
                            failure_category = name
                            break
            except Exception as e:
                logging.warning(f"[{commit_id}] Failed to derive failure category from test log: {e}")

        if failure_category is None:
            failure_category = "UNKNOWN"

        status_data["failureCategory"] = failure_category

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

