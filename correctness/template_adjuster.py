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
    # If True, add Sniper printer when missing (never remove or change if already present)
    add_printer: bool = True


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
        # (shared across templates) -> put it directly under rules/.
        # For Spoon this is the main output. For JavaParser we currently keep
        # transformations in-place, so this directory is unused for now.
        transformed_dir = os.path.join(combined_commit_folder, "rules", "transformed")
        os.makedirs(transformed_dir, exist_ok=True)
        # Use POSIX-style path in Java source
        transformed_dir_posix = transformed_dir.replace(os.sep, "/")

        # Helper to map old paths (e.g., /workspace/<project>/...) to the copied project location.
        # When the path points to a directory (not ending in .java), we also remember it so that
        # setSourceOutputDirectory can reuse the same directory for in-place transformation.
        workspace_dir_for_output: str | None = None
        is_in_place_transformation: bool = False
        # When True, the rule processes whole directories (not specific files). Skip SniperJavaPrettyPrinter
        # to avoid Spoon bugs (StringIndexOutOfBoundsException) with empty/problematic files in dirs.
        uses_directory_input: bool = False

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
                        mapped = new_abs.replace(os.sep, "/").rstrip("/")
                        # If this is a directory path (not a specific .java file), remember it for output
                        if not rel.endswith(".java"):
                            # When baseDir is project root (rel empty), Spoon needs src/main/java as output
                            # so files go to src/main/java/com/... and Maven finds them
                            if not rel or rel == "":
                                workspace_dir_for_output = (
                                    os.path.join(project_root, "src", "main", "java")
                                    .replace(os.sep, "/")
                                )
                            else:
                                workspace_dir_for_output = mapped
                            is_in_place_transformation = True
                        return mapped
                    # Case 1c: /workspace/spoon-base-template/... - resources bundled WITH the rule
                    # Map to the adjusted template location (rules/spoon-base-template-adjusted/...)
                    if workspace_proj == "spoon-base-template":
                        template_rest = rest.lstrip("/")
                        new_abs = os.path.join(adjusted_template_path, template_rest.replace("/", os.sep))
                        return new_abs.replace(os.sep, "/").rstrip("/")
                    # Case 1b: /workspace/X/... where X != project_id and not spoon-base-template
                    # (e.g. /workspace/other/target/spooned). Treat as output path.
                    # Return the correct output directory (no trailing slash to avoid "//" in concatenations).
                    if is_in_place_transformation and workspace_dir_for_output:
                        return workspace_dir_for_output.replace(os.sep, "/").rstrip("/")
                    return transformed_dir_posix.rstrip("/")

                # Case 2: any path that already contains /<project_id>/ somewhere
                marker = f"/{project_id}/"
                if marker in norm:
                    idx = norm.index(marker) + len(marker)
                    relative_part = norm[idx:]
                else:
                    # Fallback: treat whole path (without leading slash) as relative to project root
                    relative_part = norm.lstrip("/")

                new_abs = os.path.join(project_root, relative_part.replace("/", os.sep))
                mapped = new_abs.replace(os.sep, "/").rstrip("/")
                if not relative_part.endswith(".java"):
                    # When path is project root only, use src/main/java for Spoon output
                    if not relative_part or relative_part.strip() == "":
                        workspace_dir_for_output = (
                            os.path.join(project_root, "src", "main", "java")
                            .replace(os.sep, "/")
                        )
                    else:
                        workspace_dir_for_output = mapped
                    is_in_place_transformation = True
                return mapped
            except Exception:
                return old_path

        if self.context.engine == "spoon":
            # POLICY: Only adjust input/output paths and add Sniper printer if missing.
            # Never modify rule logic, structure, or any code beyond path literals.
            #
            # Walk all .java files in the adjusted template and:
            #  - rewrite <var>.addInputResource("...") to point into the copied project
            #  - rewrite <var>.setSourceOutputDirectory(...) to point to the per-commit transformed dir
            #  - rewrite any string literal containing /workspace/<project_id>/ (Paths.get, Files.write, etc.)
            #  - add SniperJavaPrettyPrinter import + setPrettyPrinterCreator only if not already present
            #    and only in files that have a Launcher (Main, etc.) — never in Processors
            #  - <var> can be launcher, spoon, or any other Launcher variable name
            input_pattern = re.compile(r'(\w+\.addInputResource\(")([^"]+)("\);)')
            # Generic: any string literal with /workspace/<project_id>/ (process("..."), etc.)
            generic_workspace_pattern = re.compile(
                r'(")(/workspace/' + re.escape(project_id) + r'[^"]*)(")'
            )
            output_literal_pattern = re.compile(r'(\w+\.setSourceOutputDirectory\(")([^"]+)("\);)')
            # Specific pattern for setSourceOutputDirectory(new File("...")) - [^)]+ fails with nested )
            output_file_pattern = re.compile(
                r'(\w+\.setSourceOutputDirectory\(new\s+File\(")([^"]*)("\)\))'
            )
            output_any_pattern = re.compile(r'(\w+\.setSourceOutputDirectory\()([^)]+)(\);)')

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

                        def _is_path_literal(s: str) -> bool:
                            """True if the string looks like a file/dir path, not arbitrary code."""
                            s = s.strip()
                            if s.startswith("/workspace/") or f"/{project_id}/" in s:
                                return True
                            # Path-like: contains path separators and looks like a path (not Java code)
                            if ".java" in s or s.endswith("/") or "/src/" in s or "/main/" in s:
                                return True
                            # Reject Java code snippets (generics, method calls) - e.g. "(java.util.SortedMap<...>)"
                            if "(" in s and ")" in s and "<" in s:
                                return False
                            return False

                        def _repl_var_assign(match: re.Match) -> str:
                            nonlocal uses_directory_input
                            prefix = match.group(1)
                            var_name = match.group(2)
                            pre_eq = match.group(3)
                            old_literal = match.group(4)
                            post = match.group(5)
                            if not _is_path_literal(old_literal):
                                return match.group(0)  # leave non-path strings unchanged
                            if not old_literal.rstrip().endswith(".java"):
                                uses_directory_input = True
                            new_literal = _map_input_path(old_literal)
                            return f'{prefix}{var_name}{pre_eq}{new_literal}{post}'

                        src, count_var_assign = var_assign_pattern.subn(_repl_var_assign, src)

                        def _repl_input(match: re.Match) -> str:
                            nonlocal uses_directory_input
                            old = match.group(2)
                            if not old.rstrip().endswith(".java"):
                                uses_directory_input = True
                            new = _map_input_path(old)
                            return f'{match.group(1)}{new}{match.group(3)}'

                        def _repl_generic_workspace(match: re.Match) -> str:
                            nonlocal uses_directory_input
                            old = match.group(2)
                            if not old.rstrip().endswith(".java"):
                                uses_directory_input = True
                            new = _map_input_path(old)
                            return f'{match.group(1)}{new}{match.group(3)}'

                        def _repl_output_literal(match: re.Match) -> str:
                            # If in-place transformation, use the input directory; otherwise use transformed directory
                            if is_in_place_transformation and workspace_dir_for_output:
                                output_dir = workspace_dir_for_output.replace(os.sep, "/").rstrip("/")
                            else:
                                output_dir = transformed_dir_posix.rstrip("/")
                            return f'{match.group(1)}{output_dir}{match.group(3)}'

                        def _repl_output_file(match: re.Match) -> str:
                            """Replace path inside setSourceOutputDirectory(new File(\"...\"))."""
                            if is_in_place_transformation and workspace_dir_for_output:
                                output_dir = workspace_dir_for_output.replace(os.sep, "/").rstrip("/")
                            else:
                                output_dir = transformed_dir_posix.rstrip("/")
                            return f'{match.group(1)}{output_dir}{match.group(3)}'

                        def _repl_output_any(match: re.Match) -> str:
                            # Only preserve when the argument is clearly an input parameter (e.g. inputDir in
                            # process(String inputDir)) - each process() call needs its own output dir.
                            # Variables like outputPath, projectPath are assigned from workspace paths and
                            # must be replaced so the template adjuster can fix them.
                            arg = match.group(2).strip()
                            input_param_names = {"inputdir", "inputpath", "sourcedir", "sourcepath", "path"}
                            if (
                                re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", arg)
                                and arg.lower() in input_param_names
                            ):
                                return match.group(0)  # preserve input parameter
                            # If in-place transformation, use the input directory; otherwise use transformed directory
                            if is_in_place_transformation and workspace_dir_for_output:
                                output_dir = workspace_dir_for_output.replace(os.sep, "/").rstrip("/")
                            else:
                                output_dir = transformed_dir_posix.rstrip("/")
                            # Preserve new File("...") structure when present; otherwise use string literal
                            file_match = re.match(r'new\s+File\s*\(\s*"([^"]*)"\s*\)', arg)
                            if file_match:
                                return f'{match.group(1)}new File("{output_dir}"){match.group(3)}'
                            return f'{match.group(1)}"{output_dir}"{match.group(3)}'

                        new_src, count_in = re.subn(input_pattern, _repl_input, src)
                        new_src, count_generic = generic_workspace_pattern.subn(_repl_generic_workspace, new_src)
                        count_in += count_generic
                        new_src, count_out_lit = re.subn(output_literal_pattern, _repl_output_literal, new_src)
                        new_src, count_out_file = output_file_pattern.subn(_repl_output_file, new_src)
                        new_src, count_out_any = output_any_pattern.subn(_repl_output_any, new_src)
                        count_out = count_out_lit + count_out_file + count_out_any

                        if count_var_assign > 0 or count_in > 0 or count_out > 0:
                            # SniperJavaPrettyPrinter preserves imports and formatting (avoids corruption in noclasspath).
                            # Only inject when add_printer=True and into files that have a Launcher (Main, etc.).
                            # Processors don't have launcher. Never remove or change if already present.
                            has_launcher = bool(
                                re.search(r"\bLauncher\s+\w+\s*=", new_src)
                                or re.search(r"\w+\.addInputResource\s*\(", new_src)
                            )
                            if self.context.add_printer and has_launcher:
                                lines = new_src.splitlines()
                                if "SniperJavaPrettyPrinter" not in new_src:
                                    insert_idx = 0
                                    for i, line in enumerate(lines):
                                        stripped = line.strip()
                                        if stripped.startswith("package ") or stripped.startswith("import "):
                                            insert_idx = i + 1
                                    lines.insert(
                                        insert_idx,
                                        "import spoon.support.sniper.SniperJavaPrettyPrinter;",
                                    )
                                    new_src = "\n".join(lines)

                            # Also ensure Sniper printer is configured on the launcher environment (only if not already present)
                            if self.context.add_printer and has_launcher and "setPrettyPrinterCreator" not in new_src:
                                lines = new_src.splitlines()
                                # Detect Launcher variable name (launcher, spoon, or any other)
                                launcher_var_match = re.search(r"\bLauncher\s+(\w+)\s*=", new_src)
                                launcher_var = launcher_var_match.group(1) if launcher_var_match else "launcher"
                                injector_block = [
                                    f"        {launcher_var}.getEnvironment().setCommentEnabled(true);",
                                    "        // Force Sniper Pretty Printer",
                                    f"        {launcher_var}.getEnvironment().setPrettyPrinterCreator(",
                                    f"            () -> new SniperJavaPrettyPrinter({launcher_var}.getEnvironment())",
                                    "        );",
                                ]
                                insert_idx = None
                                # Prefer to insert immediately after the Launcher declaration
                                launcher_decl_pattern = re.compile(r"\bLauncher\s+\w+\s*=")
                                for i, line in enumerate(lines):
                                    if launcher_decl_pattern.search(line):
                                        insert_idx = i + 1
                                        break
                                if insert_idx is None:
                                    # Fallback: insert immediately after the first addInputResource call
                                    add_input_pattern = re.compile(r"\w+\.addInputResource\(")
                                    for i, line in enumerate(lines):
                                        if add_input_pattern.search(line):
                                            insert_idx = i + 1
                                            break
                                if insert_idx is None:
                                    # Next fallback: insert before buildModel() call
                                    for i, line in enumerate(lines):
                                        if ".buildModel()" in line:
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
                                f"[{commit_id}] Adjusted {count_var_assign} var assignments, "
                                f"{count_in} input resources, and {count_out} output directories in {java_path}"
                            )
                    except Exception as e:
                        logging.warning(f"[{commit_id}] Failed to adjust inputs/outputs in {java_path}: {e}")
        elif self.context.engine == "javaparser":
            # For JavaParser templates we only adjust input paths (where the project
            # sources are read). Transformations are kept in-place for now.
            #
            # Typical patterns:
            #   Path sourceRoot = Paths.get("/workspace/<project_id>/src");
            #   Files.walk(sourceRoot)...
            #   Paths.get("/workspace/<project_id>/.../Foo.java");
            #   File file = new File("/workspace/<project_id>/.../Foo.java");
            paths_get_pattern = re.compile(r'(Paths\.get\(")([^"]+)("\))')
            file_ctor_pattern = re.compile(r'(new\s+File\(")([^"]+)("\))')
            workspace_literal_pattern = re.compile(r'"(/workspace/[^"]*)"')

            # Track mappings from original -> adjusted input paths to be stored for reporting.
            input_mappings: dict[str, str] = {}

            for root, _, files in os.walk(adjusted_template_path):
                for filename in files:
                    if not filename.endswith(".java"):
                        continue
                    java_path = os.path.join(root, filename)
                    try:
                        with open(java_path, "r", encoding="utf-8") as f:
                            src = f.read()

                        def _repl_paths_get(match: re.Match) -> str:
                            old_literal = match.group(2)
                            new_literal = _map_input_path(old_literal)
                            if old_literal != new_literal:
                                input_mappings[old_literal] = new_literal
                            return f'{match.group(1)}{new_literal}{match.group(3)}'

                        new_src, count_paths = paths_get_pattern.subn(_repl_paths_get, src)

                        def _repl_file_ctor(match: re.Match) -> str:
                            old_literal = match.group(2)
                            new_literal = _map_input_path(old_literal)
                            if old_literal != new_literal:
                                input_mappings[old_literal] = new_literal
                            return f'{match.group(1)}{new_literal}{match.group(3)}'

                        new_src, count_files = file_ctor_pattern.subn(_repl_file_ctor, new_src)

                        def _repl_workspace_literal(match: re.Match) -> str:
                            old_literal = match.group(1)
                            new_literal = _map_input_path(old_literal)
                            if old_literal != new_literal:
                                input_mappings[old_literal] = new_literal
                            # Preserve quotes around the literal
                            return f'"{new_literal}"'

                        new_src, count_ws = workspace_literal_pattern.subn(_repl_workspace_literal, new_src)

                        # Reuse the String literal assignment mapping as well, if present.
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
                            if old_literal != new_literal:
                                input_mappings[old_literal] = new_literal
                            return f'{prefix}{var_name}{pre_eq}{new_literal}{post}'

                        new_src, count_vars = var_assign_pattern.subn(_repl_var_assign, new_src)

                        if count_paths > 0 or count_files > 0 or count_ws > 0 or count_vars > 0:
                            with open(java_path, "w", encoding="utf-8") as f:
                                f.write(new_src)
                            logging.info(
                                f"[{commit_id}] Adjusted {count_paths} Paths.get(...), "
                                f"{count_files} new File(...), "
                                f"{count_ws} workspace string literals and "
                                f"{count_vars} String path assignments in {java_path}"
                            )
                    except Exception as e:
                        logging.warning(f"[{commit_id}] Failed to adjust JavaParser inputs in {java_path}: {e}")

            # Persist the collected input mappings so that status reporting can include
            # both the original and adjusted JavaParser inputs.
            if input_mappings:
                mapping_file = os.path.join(combined_commit_folder, "rules", "input-mapping.json")
                try:
                    os.makedirs(os.path.dirname(mapping_file), exist_ok=True)
                    serialized = [
                        {"original": orig, "adjusted": adjusted}
                        for orig, adjusted in sorted(input_mappings.items())
                    ]
                    with open(mapping_file, "w", encoding="utf-8") as f:
                        json.dump(
                            {
                                "engine": self.context.engine,
                                "mappings": serialized,
                            },
                            f,
                            indent=2,
                        )
                    logging.info(
                        f"[{commit_id}] Saved {len(serialized)} JavaParser input mapping(s) to {mapping_file}"
                    )
                except Exception as e:
                    logging.warning(f"[{commit_id}] Failed to save JavaParser input mappings: {e}")

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

                        # Engine-specific detection of "rule mains":
                        #  - Spoon: must have Spoon import AND Launcher API (addInputResource/setSourceOutputDirectory).
                        #    Variable name can be launcher, spoon, or any other (e.g. Launcher spoon = new Launcher()).
                        #  - JavaParser: mains that use JavaParser APIs (StaticJavaParser / JavaParser).
                        if self.context.engine == "spoon":
                            has_spoon_import = "import spoon." in src or "import spoon;" in src
                            has_launcher_api = (
                                "addInputResource(" in src or "setSourceOutputDirectory(" in src
                            )
                            if not (has_spoon_import and has_launcher_api):
                                continue
                        elif self.context.engine == "javaparser":
                            if "StaticJavaParser" not in src and "JavaParser" not in src:
                                continue
                        else:
                            continue

                        # Derive FQCN from path under src/main/java
                        rel = os.path.relpath(java_path, src_root)
                        class_name = rel.replace(os.sep, ".")[:-5]  # strip ".java"
                        rule_mains.append(class_name)

            if not rule_mains:
                # No rules => do NOT compile nor execute anything, just record NO_RULES.
                logging.warning(
                    f"[{commit_id}] No {self.context.engine} rule mains found to execute - skipping compilation"
                )
                overall_success = False
                output_lines.append(f"[{commit_id}] NO_RULES")

                log_file = os.path.join(combined_commit_folder, f"{self.context.engine}-build.log")
                with open(log_file, "w", encoding="utf-8") as f:
                    f.write("\n".join(output_lines))
                logging.info(f"[{commit_id}] Build log saved to {log_file}")

                return overall_success

            logging.info(f"[{commit_id}] Will execute rule mains: {', '.join(rule_mains)}")

            if self.context.engine == "spoon":
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
            elif self.context.engine == "javaparser":
                # JavaParser pipeline:
                #  1) Snapshot original project files (before any transformation).
                #  2) Run tests on the adjusted template (mvn test)
                #  3) For each detected rule main, run:
                #       mvn clean compile exec:java -Dexec.mainClass=<fqcn>
                self._snapshot_project_files_for_diff(
                    commit_id, combined_commit_folder, project_id, snapshot_subdir="original"
                )

                logging.info(f"[{commit_id}] Running Maven test for adjusted JavaParser template")
                test_result = subprocess.run(
                    ["mvn", "-q", "test"],
                    cwd=template_path,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                test_output = (test_result.stdout or "") + "\n" + (test_result.stderr or "")
                output_lines.extend(test_output.split("\n"))

                if test_result.returncode != 0:
                    logging.warning(
                        f"[{commit_id}] Maven test had issues for JavaParser template (exit code {test_result.returncode})"
                    )
                    overall_success = False
                else:
                    logging.info(f"[{commit_id}] Maven test for JavaParser template completed successfully")

                for fqcn in rule_mains:
                    logging.info(f"[{commit_id}] Executing JavaParser rule main {fqcn}")
                    exec_result = subprocess.run(
                        [
                            "mvn",
                            "-q",
                            "clean",
                            "compile",
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
                            f"[{commit_id}] Execution of JavaParser rule main {fqcn} had issues (exit code {exec_result.returncode})"
                        )
                        overall_success = False
                    else:
                        logging.info(f"[{commit_id}] Execution of JavaParser rule main {fqcn} completed successfully")
            else:
                logging.warning(f"[{commit_id}] Unsupported engine {self.context.engine} - skipping compilation/execution")
                overall_success = False

            # Prepend summary line
            if overall_success:
                output_lines.insert(
                    0,
                    f"[{commit_id}] Compilation/execution of {self.context.engine} rules completed successfully\n",
                )
            else:
                output_lines.insert(
                    0,
                    f"[{commit_id}] Compilation/execution of {self.context.engine} rules had warnings/errors\n",
                )

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
            if self.context.engine == "spoon":
                # Spoon: preserve comportamiento previo basado en rules/original y rules/transformed.
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
                    self._replace_project_files_with_transformed(
                        commit_id, combined_commit_folder, project_id
                    )
                else:
                    logging.info(
                        f"[{commit_id}] In-place transformation detected - skipping diffs and file replacement"
                    )
            elif self.context.engine == "javaparser":
                # JavaParser: snapshot de archivos transformados (después de aplicar reglas)
                # y diffs por archivo usando el mismo conjunto de rutas del classifier.
                self._snapshot_project_files_for_diff(
                    commit_id, combined_commit_folder, project_id, snapshot_subdir="transformed"
                )
                self._run_diffs(commit_id, combined_commit_folder)

        return overall_success

    def _snapshot_project_files_for_diff(
        self,
        commit_id: str,
        combined_commit_folder: str,
        project_id: str,
        snapshot_subdir: str,
    ) -> None:
        """Copy the current version of the project files referenced in
        rules/breaking-classifier-report.json into rules/<snapshot_subdir>/.

        This allows computing per-file diffs between original and transformed
        versions regardless of whether the transformation was in-place or not.
        """
        rules_root = os.path.join(combined_commit_folder, "rules")
        classifier_report_path = os.path.join(rules_root, "breaking-classifier-report.json")
        project_root = os.path.join(combined_commit_folder, project_id)
        snapshot_root = os.path.join(rules_root, snapshot_subdir)

        if not os.path.isfile(classifier_report_path):
            logging.info(
                f"[{commit_id}] No classifier report at {classifier_report_path} - "
                f"skipping snapshot to {snapshot_subdir}"
            )
            return
        if not os.path.isdir(project_root):
            logging.warning(
                f"[{commit_id}] Project root not found at {project_root} - "
                f"skipping snapshot to {snapshot_subdir}"
            )
            return

        try:
            with open(classifier_report_path, "r", encoding="utf-8") as f:
                report = json.load(f)
        except Exception as e:
            logging.warning(
                f"[{commit_id}] Failed to read classifier report for snapshot: {e}"
            )
            return

        errors_by_file = report.get("errorsByFile", [])
        if not isinstance(errors_by_file, list) or not errors_by_file:
            logging.info(f"[{commit_id}] No errorsByFile entries - skipping snapshot to {snapshot_subdir}")
            return

        snapped = 0
        for entry in errors_by_file:
            if not isinstance(entry, dict):
                continue
            file_path = entry.get("filePath")
            if not isinstance(file_path, str) or not file_path.endswith(".java"):
                continue

            # file_path is typically like "/<project_id>/.../src/main/java/pkg/Foo.java"
            norm = file_path.strip()
            if norm.startswith(f"/{project_id}/"):
                rel_in_project = norm[len(f"/{project_id}/") :]
            else:
                rel_in_project = norm.lstrip("/")

            src_path = os.path.join(project_root, rel_in_project.replace("/", os.sep))
            if not os.path.isfile(src_path):
                logging.warning(
                    f"[{commit_id}] Source file not found for snapshot {snapshot_subdir}: expected {src_path}"
                )
                continue

            # For JavaParser originals we flatten into the snapshot root to avoid
            # duplicated directory structures (only keep the filename). For all
            # other cases we preserve the relative path.
            if self.context.engine == "javaparser" and snapshot_subdir == "original":
                dest_rel = os.path.basename(rel_in_project)
            else:
                dest_rel = rel_in_project.replace("/", os.sep)

            dest_path = os.path.join(snapshot_root, dest_rel)
            try:
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                shutil.copy2(src_path, dest_path)
                snapped += 1
            except Exception as e:
                logging.warning(
                    f"[{commit_id}] Failed to snapshot {src_path} to {dest_path} "
                    f"for {snapshot_subdir}: {e}"
                )

        logging.info(
            f"[{commit_id}] Snapshot to {snapshot_subdir} completed: files_copied={snapped}"
        )

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
        if not isinstance(errors_by_file, list):
            errors_by_file = []

        replaced = 0
        missing = 0

        if errors_by_file:
            # Use errorsByFile to get file paths
            entries_to_process = errors_by_file
        else:
            # Fallback when errorsByFile is empty: use transformed output and match by path
            # Walk rules/transformed/ and for each .java, find the corresponding project file
            # by matching the package path (e.g. org/pkg/File.java) under src/main/java or src/test/java
            logging.info(f"[{commit_id}] errorsByFile empty - using transformed files and matching by input path")
            entries_to_process = []
            for root, _, files in os.walk(transformed_root):
                for name in files:
                    if not name.endswith(".java"):
                        continue
                    transformed_path = os.path.join(root, name)
                    rel_to_transformed = os.path.relpath(transformed_path, transformed_root)
                    package_rel = rel_to_transformed.replace(os.sep, "/")
                    # Find project file with same path under src/main/java or src/test/java
                    dest_path = None
                    for proot, _, pfiles in os.walk(project_root):
                        for pname in pfiles:
                            if pname != name:
                                continue
                            pfull = os.path.join(proot, pname)
                            prel = os.path.relpath(pfull, project_root).replace(os.sep, "/")
                            if (
                                ("src/main/java" in prel or "src/test/java" in prel)
                                and prel.endswith(package_rel)
                            ):
                                dest_path = pfull
                                break
                        if dest_path:
                            break
                    if dest_path:
                        entries_to_process.append({"filePath": dest_path, "_transformed_path": transformed_path})
                    else:
                        missing += 1
                        logging.warning(
                            f"[{commit_id}] No project file found for transformed {package_rel}"
                        )

        for entry in entries_to_process:
            if not isinstance(entry, dict):
                continue
            # Fallback case: entry has dest_path and transformed_path directly
            if "_transformed_path" in entry:
                dest_path = entry.get("filePath")  # actually the full dest path
                transformed_path = entry.get("_transformed_path")
            else:
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
                    f"[{commit_id}] Transformed file not found: expected {transformed_path}"
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

