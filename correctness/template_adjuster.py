import logging
import os
import re
import shutil
import hashlib
import subprocess
from dataclasses import dataclass


@dataclass
class TemplateAdjusterContext:
    engine: str


class TemplateAdjuster:
    """Handles per-commit template copying and path adjustments, plus compilation."""

    def __init__(self, context: TemplateAdjusterContext) -> None:
        self.context = context

    def prepare_adjusted_template(
        self,
        commit_id: str,
        combined_commit_folder: str,
        project_id: str,
    ) -> None:
        """Create a per-commit copy of the template and update its input resources
        so that they point to the copied project inside the combined output."""
        base_template_name = f"{self.context.engine}-base-template"
        base_template_path = os.path.join(combined_commit_folder, "rules", base_template_name)

        if not os.path.isdir(base_template_path):
            logging.warning(
                f"[{commit_id}] Base template not found at {base_template_path} - skipping template adjustment"
            )
            return

        adjusted_template_name = f"{base_template_name}-adjusted"
        adjusted_template_path = os.path.join(combined_commit_folder, "rules", adjusted_template_name)

        # Copy original template to adjusted location (overwrite if exists)
        shutil.copytree(base_template_path, adjusted_template_path, dirs_exist_ok=True)

        # Project root inside the combined output
        project_root = os.path.join(combined_commit_folder, project_id)

        # Directory where transformed sources will be written for this commit
        # (shared across templates) -> put it directly under rules/
        transformed_dir = os.path.join(combined_commit_folder, "rules", "transformed")
        os.makedirs(transformed_dir, exist_ok=True)
        # Use POSIX-style path in Java source
        transformed_dir_posix = transformed_dir.replace(os.sep, "/")

        # Helper to map old paths to the copied project location
        def _map_input_path(old_path: str) -> str:
            try:
                norm = old_path.strip()
                marker = f"/{project_id}/"
                if marker in norm:
                    idx = norm.index(marker) + len(marker)
                    relative_part = norm[idx:]
                else:
                    relative_part = norm.lstrip("/")
                new_abs = os.path.join(project_root, relative_part.replace("/", os.sep))
                return new_abs.replace(os.sep, "/")
            except Exception:
                return old_path

        # Walk all .java files in the adjusted template and:
        #  - rewrite launcher.addInputResource("...") to point into the copied project
        #  - rewrite launcher.setSourceOutputDirectory("...") to point to the per-commit transformed dir
        #  - ensure they import SniperJavaPrettyPrinter when they use these APIs
        input_pattern = r'(launcher\.addInputResource\(")([^"]+)("\);)'
        output_pattern = r'(launcher\.setSourceOutputDirectory\(")([^"]+)("\);)'

        for root, _, files in os.walk(adjusted_template_path):
            for filename in files:
                if not filename.endswith(".java"):
                    continue
                java_path = os.path.join(root, filename)
                try:
                    with open(java_path, "r", encoding="utf-8") as f:
                        src = f.read()

                    def _repl_input(match: re.Match) -> str:
                        old = match.group(2)
                        new = _map_input_path(old)
                        return f'{match.group(1)}{new}{match.group(3)}'

                    def _repl_output(match: re.Match) -> str:
                        return f'{match.group(1)}{transformed_dir_posix}{match.group(3)}'

                    new_src, count_in = re.subn(input_pattern, _repl_input, src)
                    new_src, count_out = re.subn(output_pattern, _repl_output, new_src)

                    if count_in > 0 or count_out > 0:
                        # If the file uses input/output configuration, make sure it imports SniperJavaPrettyPrinter
                        if "SniperJavaPrettyPrinter" not in new_src:
                            lines = new_src.splitlines()
                            insert_idx = 0
                            # Find the last package/import line to insert after
                            for i, line in enumerate(lines):
                                stripped = line.strip()
                                if stripped.startswith("package ") or stripped.startswith("import "):
                                    insert_idx = i + 1
                            lines.insert(
                                insert_idx,
                                "import spoon.support.sniper.SniperJavaPrettyPrinter;",
                            )
                            new_src = "\n".join(lines)

                        # Also ensure Sniper printer is configured on the launcher environment
                        if "setPrettyPrinterCreator" not in new_src:
                            lines = new_src.splitlines()
                            injector_block = [
                                "        launcher.getEnvironment().setCommentEnabled(true);",
                                "        // Force Sniper Pretty Printer",
                                "        launcher.getEnvironment().setPrettyPrinterCreator(",
                                "            () -> new SniperJavaPrettyPrinter(launcher.getEnvironment())",
                                "        );",
                            ]
                            # Try to insert before launcher.buildModel(), otherwise before last closing brace
                            insert_idx = None
                            for i, line in enumerate(lines):
                                if "launcher.buildModel()" in line:
                                    insert_idx = i
                                    break
                            if insert_idx is None:
                                # Fallback: before last closing brace
                                for i in range(len(lines) - 1, -1, -1):
                                    if lines[i].strip() == "}":
                                        insert_idx = i
                                        break
                            if insert_idx is None:
                                insert_idx = len(lines)
                            lines[insert_idx:insert_idx] = injector_block
                            new_src = "\n".join(lines)

                        with open(java_path, "w", encoding="utf-8") as f:
                            f.write(new_src)
                        logging.info(
                            f"[{commit_id}] Adjusted {count_in} input resources and "
                            f"{count_out} output directories in {java_path}"
                        )
                except Exception as e:
                    logging.warning(f"[{commit_id}] Failed to adjust inputs/outputs in {java_path}: {e}")

    def compile_template(self, commit_id: str, combined_commit_folder: str) -> bool:
        """Compile and run adjusted template with Maven on the host.

        Returns True if compilation and execution were successful.
        """
        template_name = f"{self.context.engine}-base-template-adjusted"
        template_path = os.path.join(combined_commit_folder, "rules", template_name)

        if not os.path.isdir(template_path):
            logging.warning(
                f"[{commit_id}] Template not found at {template_path} - skipping compilation"
            )
            return False

        logging.info(f"[{commit_id}] Compiling & executing {self.context.engine} template on host at {template_path}")

        compile_success = False
        output_lines: list[str] = []
        try:
            # First, compile and then run Main via the Maven exec plugin
            result = subprocess.run(
                [
                    "mvn",
                    "-q",
                    "-DskipTests",
                    "compile",
                    "org.codehaus.mojo:exec-maven-plugin:3.5.0:java",
                    "-Dexec.mainClass=github.chains.Main",
                ],
                cwd=template_path,
                text=True,
                capture_output=True,
                check=False,
            )
            output = (result.stdout or "") + "\n" + (result.stderr or "")
            output_lines = output.split("\n")
            compile_success = result.returncode == 0

            if not compile_success:
                logging.warning(
                    f"[{commit_id}] Maven compilation/execution had issues (exit code {result.returncode})"
                )
                output_lines.insert(0, f"[{commit_id}] Compilation/execution completed with warnings/errors\n")
            else:
                logging.info(f"[{commit_id}] Maven compilation/execution completed successfully")
                output_lines.insert(0, f"[{commit_id}] Compilation/execution completed successfully\n")
        except Exception as e:
            logging.error(f"[{commit_id}] Compilation error: {e}")
            output_lines = [f"[{commit_id}] Compilation error: {str(e)}"]
            compile_success = False

        # Save compilation log
        log_file = os.path.join(combined_commit_folder, f"{self.context.engine}-build.log")
        with open(log_file, "w", encoding="utf-8") as f:
            f.write("\n".join(output_lines))
        logging.info(f"[{commit_id}] Build log saved to {log_file}")

        # If execution succeeded and we have transformed files, run diffs vs originals
        if compile_success:
            self._run_diffs(commit_id, combined_commit_folder)

        return compile_success

    def _run_diffs(self, commit_id: str, combined_commit_folder: str) -> None:
        """Run diff -w -t between original and transformed Java files and store results under rules/diffs."""
        rules_root = os.path.join(combined_commit_folder, "rules")
        original_root = os.path.join(rules_root, "original")
        transformed_root = os.path.join(rules_root, "transformed")

        if not os.path.isdir(original_root) or not os.path.isdir(transformed_root):
            logging.info(
                f"[{commit_id}] Skipping diffs; original ({original_root}) "
                f"or transformed ({transformed_root}) directory is missing"
            )
            return

        # Index originals by filename (supporting multiple files; skip ambiguous matches)
        originals_by_name: dict[str, list[str]] = {}
        for root, _, files in os.walk(original_root):
            for name in files:
                if not name.endswith(".java"):
                    continue
                path = os.path.join(root, name)
                originals_by_name.setdefault(name, []).append(path)

        diffs_root = os.path.join(rules_root, "diffs")

        for root, _, files in os.walk(transformed_root):
            for name in files:
                if not name.endswith(".java"):
                    continue
                transformed_path = os.path.join(root, name)
                candidates = originals_by_name.get(name) or []

                if len(candidates) == 0:
                    logging.info(
                        f"[{commit_id}] No original found for transformed file {transformed_path}; skipping diff"
                    )
                    continue
                if len(candidates) > 1:
                    logging.warning(
                        f"[{commit_id}] Multiple originals found for {name}; skipping diff to avoid ambiguity"
                    )
                    continue

                original_path = candidates[0]

                try:
                    result = subprocess.run(
                        ["diff", "-w", "-t", original_path, transformed_path],
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    # diff exit code: 0 = no diff, 1 = differences, >1 = error
                    if result.returncode > 1:
                        logging.warning(
                            f"[{commit_id}] diff failed for {original_path} vs {transformed_path}: {result.stderr}"
                        )
                        continue

                    if not result.stdout.strip():
                        # No visible differences (ignoring whitespace)
                        continue

                    # Store diffs flattened (no subdirectories) to keep the folder easy to scan.
                    # Add a hash to avoid collisions when multiple files share the same basename.
                    base = os.path.basename(transformed_path)
                    h = hashlib.sha1(
                        f"{original_path}|{transformed_path}".encode("utf-8")
                    ).hexdigest()[:2]
                    diff_path = os.path.join(diffs_root, f"{base}.{h}.diff")
                    os.makedirs(diffs_root, exist_ok=True)
                    with open(diff_path, "w", encoding="utf-8") as f:
                        f.write(result.stdout)

                    logging.info(
                        f"[{commit_id}] Stored diff for {original_path} vs {transformed_path} at {diff_path}"
                    )
                except Exception as e:
                    logging.warning(
                        f"[{commit_id}] Error while running diff for {original_path} vs {transformed_path}: {e}"
                    )

