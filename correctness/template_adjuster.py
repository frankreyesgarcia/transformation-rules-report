import logging
import json
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

        # Helper to map old paths (e.g., /workspace/<project>/...) to the copied project location.
        # When the path points to a directory (not ending in .java), we also remember it so that
        # setSourceOutputDirectory can reuse the same directory for in-place transformation.
        workspace_dir_for_output: str | None = None
        is_in_place_transformation: bool = False

        def _map_input_path(old_path: str) -> str:
            nonlocal workspace_dir_for_output, is_in_place_transformation
            try:
                norm = old_path.strip()

                # Case 1: explicit /workspace/<project_id>/... pattern
                m = re.search(r"/workspace/([^/]+)(/.*)?", norm)
                if m:
                    workspace_proj = m.group(1)
                    rest = m.group(2) or ""
                    # If the workspace project matches the benchmark project_id, map into the copied project root
                    if workspace_proj == project_id:
                        rel = rest.lstrip("/")  # drop leading slash from the "rest"
                        new_abs = os.path.join(project_root, rel.replace("/", os.sep))
                        mapped = new_abs.replace(os.sep, "/")
                        # If this is a directory path (not a specific .java file), remember it for output
                        if not rel.endswith(".java"):
                            workspace_dir_for_output = mapped
                            is_in_place_transformation = True
                        return mapped

                # Case 2: any path that already contains /<project_id>/ somewhere
                marker = f"/{project_id}/"
                if marker in norm:
                    idx = norm.index(marker) + len(marker)
                    relative_part = norm[idx:]
                else:
                    # Fallback: treat whole path (without leading slash) as relative to project root
                    relative_part = norm.lstrip("/")

                new_abs = os.path.join(project_root, relative_part.replace("/", os.sep))
                mapped = new_abs.replace(os.sep, "/")
                if not relative_part.endswith(".java"):
                    workspace_dir_for_output = mapped
                    is_in_place_transformation = True
                return mapped
            except Exception:
                return old_path

        # Walk all .java files in the adjusted template and:
        #  - rewrite launcher.addInputResource("...") to point into the copied project
        #  - rewrite launcher.setSourceOutputDirectory(...) to point to the per-commit transformed dir
        #  - ensure they import SniperJavaPrettyPrinter when they use these APIs
        input_pattern = r'(launcher\.addInputResource\(")([^"]+)("\);)'
        output_literal_pattern = r'(launcher\.setSourceOutputDirectory\(")([^"]+)("\);)'
        output_any_pattern = re.compile(r'(launcher\.setSourceOutputDirectory\()([^)]+)(\);)')

        for root, _, files in os.walk(adjusted_template_path):
            for filename in files:
                if not filename.endswith(".java"):
                    continue
                java_path = os.path.join(root, filename)
                try:
                    with open(java_path, "r", encoding="utf-8") as f:
                        src = f.read()

                    # First, handle the common pattern:
                    #   String projectPath = "/workspace/<project_id>/...";
                    #   launcher.addInputResource(projectPath);
                    # We rewrite the assignment to use the mapped absolute path and
                    # leave the rest of the line untouched.
                    var_assign_pattern = re.compile(
                        r'(String\s+)(\w+)(\s*=\s*")([^"]+)(";\s*)'
                    )

                    def _repl_var_assign(match: re.Match) -> str:
                        prefix = match.group(1)
                        var_name = match.group(2)
                        pre_eq = match.group(3)
                        old_literal = match.group(4)
                        post = match.group(5)
                        new_literal = _map_input_path(old_literal)
                        return f'{prefix}{var_name}{pre_eq}{new_literal}{post}'

                    src = var_assign_pattern.sub(_repl_var_assign, src)

                    def _repl_input(match: re.Match) -> str:
                        old = match.group(2)
                        new = _map_input_path(old)
                        return f'{match.group(1)}{new}{match.group(3)}'

                    def _repl_output_literal(match: re.Match) -> str:
                        # If in-place transformation, use the input directory; otherwise use transformed directory
                        if is_in_place_transformation and workspace_dir_for_output:
                            output_dir = workspace_dir_for_output.replace(os.sep, "/")
                        else:
                            output_dir = transformed_dir_posix
                        return f'{match.group(1)}{output_dir}{match.group(3)}'

                    def _repl_output_any(match: re.Match) -> str:
                        # If in-place transformation, use the input directory; otherwise use transformed directory
                        if is_in_place_transformation and workspace_dir_for_output:
                            output_dir = workspace_dir_for_output.replace(os.sep, "/")
                        else:
                            output_dir = transformed_dir_posix
                        return f'{match.group(1)}"{output_dir}"{match.group(3)}'

                    new_src, count_in = re.subn(input_pattern, _repl_input, src)
                    new_src, count_out_lit = re.subn(output_literal_pattern, _repl_output_literal, new_src)
                    new_src, count_out_any = output_any_pattern.subn(_repl_output_any, new_src)
                    count_out = count_out_lit + count_out_any

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
                            insert_idx = None
                            # Prefer to insert immediately after the Launcher declaration
                            launcher_decl_pattern = re.compile(r"\bLauncher\s+launcher\s*=")
                            for i, line in enumerate(lines):
                                if launcher_decl_pattern.search(line):
                                    insert_idx = i + 1
                                    break
                            if insert_idx is None:
                                # Fallback: insert immediately after the first addInputResource call
                                add_input_pattern = re.compile(r"launcher\.addInputResource\(")
                                for i, line in enumerate(lines):
                                    if add_input_pattern.search(line):
                                        insert_idx = i + 1
                                        break
                            if insert_idx is None:
                                # Next fallback: insert before launcher.buildModel()
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

        # Save in-place transformation flag for later use
        flag_file = os.path.join(combined_commit_folder, "rules", "in-place-flag.json")
        try:
            with open(flag_file, "w", encoding="utf-8") as f:
                json.dump({"is_in_place": is_in_place_transformation}, f)
            if is_in_place_transformation:
                logging.info(
                    f"[{commit_id}] In-place transformation detected - output will be written to input directory"
                )
        except Exception as e:
            logging.warning(f"[{commit_id}] Failed to save in-place flag: {e}")

    def compile_template(self, commit_id: str, combined_commit_folder: str, project_id: str) -> bool:
        """Compile and run adjusted template with Maven on the host.

        For Spoon we may have multiple rule entrypoints (classes with main + Spoon inputs/outputs).
        This method will:
        - detect which rule mains configure inputs/outputs,
        - if there are any, run `mvn compile` once, and then
        - run each such rule main.
        - if there are none, it will not compile nor execute anything.

        Returns True only if compilation succeeds AND all relevant rule mains execute successfully.
        """
        template_name = f"{self.context.engine}-base-template-adjusted"
        template_path = os.path.join(combined_commit_folder, "rules", template_name)

        if not os.path.isdir(template_path):
            logging.warning(
                f"[{commit_id}] Template not found at {template_path} - skipping compilation"
            )
            return False

        logging.info(f"[{commit_id}] Preparing {self.context.engine} template on host at {template_path}")

        overall_success = True
        output_lines: list[str] = []

        try:
            # 1) Discover rule mains that actually configure inputs/outputs
            rule_mains: list[str] = []
            src_root = os.path.join(template_path, "src", "main", "java")
            if os.path.isdir(src_root):
                for root, _, files in os.walk(src_root):
                    for name in files:
                        if not name.endswith(".java"):
                            continue
                        java_path = os.path.join(root, name)
                        try:
                            with open(java_path, "r", encoding="utf-8") as f:
                                src = f.read()
                        except Exception:
                            continue

                        # Must have a main method
                        if "public static void main(String[] args" not in src:
                            continue
                        # And must configure Spoon IO (inputs or outputs)
                        if "launcher.addInputResource(" not in src and "launcher.setSourceOutputDirectory(" not in src:
                            continue

                        # Derive FQCN from path under src/main/java
                        rel = os.path.relpath(java_path, src_root)
                        class_name = rel.replace(os.sep, ".")[:-5]  # strip ".java"
                        rule_mains.append(class_name)

            if not rule_mains:
                # No rules with IO => do NOT compile nor execute anything, just record NO_RULES.
                logging.warning(
                    f"[{commit_id}] No Spoon rule mains with inputs/outputs found to execute - skipping compilation"
                )
                overall_success = False
                output_lines.append(f"[{commit_id}] NO_RULES")

                log_file = os.path.join(combined_commit_folder, f"{self.context.engine}-build.log")
                with open(log_file, "w", encoding="utf-8") as f:
                    f.write("\n".join(output_lines))
                logging.info(f"[{commit_id}] Build log saved to {log_file}")

                return overall_success

            logging.info(f"[{commit_id}] Will execute rule mains: {', '.join(rule_mains)}")

            # 2) Compile once
            logging.info(f"[{commit_id}] Running Maven compile for adjusted template")
            compile_result = subprocess.run(
                ["mvn", "-q", "-DskipTests", "compile"],
                cwd=template_path,
                text=True,
                capture_output=True,
                check=False,
            )
            compile_output = (compile_result.stdout or "") + "\n" + (compile_result.stderr or "")
            output_lines.extend(compile_output.split("\n"))

            if compile_result.returncode != 0:
                logging.warning(
                    f"[{commit_id}] Maven compile had issues (exit code {compile_result.returncode})"
                )
                overall_success = False
            else:
                logging.info(f"[{commit_id}] Maven compile completed successfully")

            # 3) Execute each rule main via Maven exec (uses project's runtime classpath)
            for fqcn in rule_mains:
                logging.info(f"[{commit_id}] Executing rule main {fqcn}")
                exec_result = subprocess.run(
                    [
                        "mvn",
                        "-q",
                        "-DskipTests",
                        "org.codehaus.mojo:exec-maven-plugin:3.5.0:java",
                        f"-Dexec.mainClass={fqcn}",
                    ],
                    cwd=template_path,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                exec_output = (exec_result.stdout or "") + "\n" + (exec_result.stderr or "")
                output_lines.extend(exec_output.split("\n"))

                if exec_result.returncode != 0:
                    logging.warning(
                        f"[{commit_id}] Execution of rule main {fqcn} had issues (exit code {exec_result.returncode})"
                    )
                    overall_success = False
                else:
                    logging.info(f"[{commit_id}] Execution of rule main {fqcn} completed successfully")

            # Prepend summary line
            if overall_success:
                output_lines.insert(0, f"[{commit_id}] Compilation/execution of Spoon rules completed successfully\n")
            else:
                output_lines.insert(0, f"[{commit_id}] Compilation/execution of Spoon rules had warnings/errors\n")

        except Exception as e:
            logging.error(f"[{commit_id}] Compilation/execution error: {e}")
            output_lines = [f"[{commit_id}] Compilation/execution error: {str(e)}"]
            overall_success = False

        # Save compilation log
        log_file = os.path.join(combined_commit_folder, f"{self.context.engine}-build.log")
        with open(log_file, "w", encoding="utf-8") as f:
            f.write("\n".join(output_lines))
        logging.info(f"[{commit_id}] Build log saved to {log_file}")

        # If execution succeeded, check if we need to run diffs/replacement
        if overall_success:
            # Check if this is an in-place transformation
            flag_file = os.path.join(combined_commit_folder, "rules", "in-place-flag.json")
            is_in_place = False
            if os.path.isfile(flag_file):
                try:
                    with open(flag_file, "r", encoding="utf-8") as f:
                        flag_data = json.load(f)
                        is_in_place = flag_data.get("is_in_place", False)
                except Exception as e:
                    logging.warning(f"[{commit_id}] Failed to read in-place flag: {e}")
            
            if not is_in_place:
                # Only run diffs and replacement if NOT in-place transformation
                self._run_diffs(commit_id, combined_commit_folder)
                self._replace_project_files_with_transformed(commit_id, combined_commit_folder, project_id)
            else:
                logging.info(
                    f"[{commit_id}] In-place transformation detected - skipping diffs and file replacement"
                )

        return overall_success

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

    def _replace_project_files_with_transformed(
        self, commit_id: str, combined_commit_folder: str, project_id: str
    ) -> None:
        """Overwrite project files with the transformed versions.

        Uses `rules/breaking-classifier-report.json` to find the real file locations in the
        copied project, and maps them to the corresponding transformed output under `rules/transformed/`.
        """
        rules_root = os.path.join(combined_commit_folder, "rules")
        transformed_root = os.path.join(rules_root, "transformed")
        classifier_report_path = os.path.join(rules_root, "breaking-classifier-report.json")
        project_root = os.path.join(combined_commit_folder, project_id)

        if not os.path.isdir(transformed_root):
            logging.info(f"[{commit_id}] No transformed directory at {transformed_root} - skipping replacement")
            return
        if not os.path.isfile(classifier_report_path):
            logging.info(f"[{commit_id}] No classifier report at {classifier_report_path} - skipping replacement")
            return
        if not os.path.isdir(project_root):
            logging.warning(f"[{commit_id}] Project root not found at {project_root} - skipping replacement")
            return

        try:
            with open(classifier_report_path, "r", encoding="utf-8") as f:
                report = json.load(f)
        except Exception as e:
            logging.warning(f"[{commit_id}] Failed to read classifier report for replacement: {e}")
            return

        errors_by_file = report.get("errorsByFile", [])
        if not isinstance(errors_by_file, list) or not errors_by_file:
            logging.info(f"[{commit_id}] No errorsByFile entries - skipping replacement")
            return

        replaced = 0
        missing = 0

        for entry in errors_by_file:
            if not isinstance(entry, dict):
                continue
            file_path = entry.get("filePath")
            if not isinstance(file_path, str) or not file_path.endswith(".java"):
                continue

            # file_path is typically like "/<project_id>/.../src/main/java/pkg/Foo.java"
            # Build the destination path inside the copied project.
            norm = file_path.strip()
            if norm.startswith(f"/{project_id}/"):
                rel_in_project = norm[len(f"/{project_id}/") :]
            else:
                rel_in_project = norm.lstrip("/")

            dest_path = os.path.join(project_root, rel_in_project.replace("/", os.sep))

            # Map to transformed path: strip module prefix and src/*/java segment if present.
            markers = ("src/main/java/", "src/test/java/")
            package_rel = None
            for m in markers:
                if m in rel_in_project:
                    package_rel = rel_in_project.split(m, 1)[1]
                    break
            if package_rel is None:
                # Fallback: strip up to the last "/java/" segment if present
                if "/java/" in rel_in_project:
                    package_rel = rel_in_project.split("/java/", 1)[1]
                else:
                    package_rel = rel_in_project

            transformed_path = os.path.join(transformed_root, package_rel.replace("/", os.sep))

            if not os.path.isfile(transformed_path):
                missing += 1
                logging.warning(
                    f"[{commit_id}] Transformed file not found for {file_path}: expected {transformed_path}"
                )
                continue

            try:
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                shutil.copy2(transformed_path, dest_path)
                replaced += 1
                logging.info(f"[{commit_id}] Replaced project file {dest_path} with transformed output")
            except Exception as e:
                logging.warning(f"[{commit_id}] Failed to replace {dest_path} with {transformed_path}: {e}")

        logging.info(f"[{commit_id}] Replacement done: replaced={replaced} missing_transformed={missing}")

