import logging
import os
import shutil
import json
from dataclasses import dataclass

from .metadata_loader import BenchmarkMetadataLoader
from .docker_ops import DockerOps


@dataclass
class ProcessingContext:
    benchmark_folder: str
    rules_folder: str
    projects_output_folder: str
    combined_output_folder: str
    engine: str


class CommitProcessor:
    """Coordinates per-commit processing: metadata, docker extraction, rule copying."""

    def __init__(self, context: ProcessingContext) -> None:
        self.context = context
        self.metadata_loader = BenchmarkMetadataLoader(context.benchmark_folder)
        self.docker = DockerOps()

    def process(self, commit_id: str) -> None:
        logging.info(f"[{commit_id}] ==== Processing commit ====")

        # Load project ID - will raise FileNotFoundError if JSON is missing
        project_id = self.metadata_loader.load_project_id(commit_id)
        logging.info(f"[{commit_id}] Loaded project ID: {project_id}")

        # Clean combined output folder if it exists
        combined_commit_folder = os.path.join(self.context.combined_output_folder, commit_id)
        if os.path.exists(combined_commit_folder):
            logging.info(f"[{commit_id}] Removing existing combined folder: {combined_commit_folder}")
            shutil.rmtree(combined_commit_folder)

        # Check if project already exists in projects output folder
        projects_commit_folder = os.path.join(self.context.projects_output_folder, commit_id)
        skip_extraction = os.path.exists(projects_commit_folder)
        
        if skip_extraction:
            logging.info(f"[{commit_id}] Project already exists at {projects_commit_folder} - skipping extraction")
        else:
            # Extract project only if it doesn't exist
            image_ref = f"ghcr.io/chains-project/breaking-updates:{commit_id}-breaking"
            if not self.docker.pull_image(image_ref, commit_id):
                raise RuntimeError(f"[{commit_id}] Docker image pull failed for {image_ref}")
            logging.info(f"[{commit_id}] Docker image pulled successfully")

            container_name = self.docker.create_container(image_ref, commit_id)
            if not container_name:
                raise RuntimeError(f"[{commit_id}] Docker container creation failed")
            logging.info(f"[{commit_id}] Docker container created: {container_name}")

            try:
                # Extract project to main output folder
                dest_parent = os.path.join(self.context.projects_output_folder, commit_id)
                src_path = f"/{project_id}"
                extracted = self.docker.copy_from_container(container_name, src_path, dest_parent, commit_id)
                if not extracted:
                    raise RuntimeError(f"[{commit_id}] Project extraction from Docker container failed")
                logging.info(f"[{commit_id}] Project successfully extracted to {dest_parent}")
            finally:
                print(self.docker.remove_container(container_name, commit_id))

        # Copy project to combined output folder with project name (always do this)
        projects_commit_folder = os.path.join(self.context.projects_output_folder, commit_id)
        project_src = os.path.join(projects_commit_folder, project_id)
        os.makedirs(combined_commit_folder, exist_ok=True)
        combined_project_dest = os.path.join(combined_commit_folder, project_id)
        shutil.copytree(project_src, combined_project_dest, dirs_exist_ok=True)
        logging.info(f"[{commit_id}] Project copied to combined folder: {combined_project_dest}")

        # Copy rules to combined output folder in rules subfolder (always do this)
        rules_src = os.path.join(self.context.rules_folder, commit_id)
        if os.path.isdir(rules_src):
            combined_rules_dest = os.path.join(combined_commit_folder, "rules")
            shutil.copytree(rules_src, combined_rules_dest, dirs_exist_ok=True)
            logging.info(f"[{commit_id}] Rules copied to combined folder: {combined_rules_dest}")
        else:
            logging.warning(f"[{commit_id}] Rules source not found: {rules_src}")

        # Compile template with Maven (always do this)
        compile_success = self._compile_template(commit_id, combined_commit_folder)
        
        # Save compilation status to JSON
        self._save_compilation_status(commit_id, combined_commit_folder, compile_success)

    def _compile_template(self, commit_id: str, combined_commit_folder: str) -> bool:
        """Compile template with Maven on the host. Returns True if compilation was successful."""
        template_name = f"{self.context.engine}-base-template"
        template_path = os.path.join(combined_commit_folder, "rules", template_name)
        
        if not os.path.isdir(template_path):
            logging.warning(f"[{commit_id}] Template not found at {template_path} - skipping compilation")
            return False
        
        logging.info(f"[{commit_id}] Compiling {self.context.engine} template on host at {template_path}")

        import subprocess

        compile_success = False
        output_lines = []
        try:
            result = subprocess.run(
                ["mvn", "compile"],
                cwd=template_path,
                text=True,
                capture_output=True,
                check=False,
            )
            output = (result.stdout or "") + "\n" + (result.stderr or "")
            output_lines = output.split("\n")
            compile_success = result.returncode == 0

            if not compile_success:
                logging.warning(f"[{commit_id}] Maven compilation had issues (exit code {result.returncode})")
                output_lines.insert(0, f"[{commit_id}] Compilation completed with warnings/errors\n")
            else:
                logging.info(f"[{commit_id}] Maven compilation completed successfully")
                output_lines.insert(0, f"[{commit_id}] Compilation completed successfully\n")
        except Exception as e:
            logging.error(f"[{commit_id}] Compilation error: {e}")
            output_lines = [f"[{commit_id}] Compilation error: {str(e)}"]
            compile_success = False

        # Save compilation log
        log_file = os.path.join(combined_commit_folder, f"{self.context.engine}-build.log")
        with open(log_file, "w", encoding="utf-8") as f:
            f.write("\n".join(output_lines))
        logging.info(f"[{commit_id}] Build log saved to {log_file}")
        
        return compile_success

    def _save_compilation_status(self, commit_id: str, combined_commit_folder: str, compile_success: bool) -> None:
        """Save compilation status to central JSON file. Structure: { commitId: { compile: true/false, errorsByFileCount?: int } }"""
        # Default status with compilation result
        status_data = {
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
                    # Collect file paths (if present) for each entry
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