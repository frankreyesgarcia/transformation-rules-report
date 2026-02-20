import logging
import os
import shutil
import json
from dataclasses import dataclass

from .metadata_loader import BenchmarkMetadataLoader
from .docker_ops import DockerOps
from .template_adjuster import TemplateAdjuster, TemplateAdjusterContext
from .status_recorder import CompilationStatusRecorder, StatusRecorderContext


@dataclass
class ProcessingContext:
    benchmark_folder: str
    rules_folder: str
    projects_output_folder: str
    combined_output_folder: str
    engine: str
    # If True, skip commits that already have an entry in compilation-results.json
    skip_existing_commits: bool = False


class CommitProcessor:
    """Coordinates per-commit processing: metadata, docker extraction, rule copying."""

    def __init__(self, context: ProcessingContext) -> None:
        self.context = context
        self.metadata_loader = BenchmarkMetadataLoader(context.benchmark_folder)
        self.docker = DockerOps()
        self.template_adjuster = TemplateAdjuster(
            TemplateAdjusterContext(engine=context.engine)
        )
        self.status_recorder = CompilationStatusRecorder(
            StatusRecorderContext(engine=context.engine)
        )

    def process(self, commit_id: str) -> None:
        logging.info(f"[{commit_id}] ==== Processing commit ====")

        # Optionally skip commits already present in compilation-results.json
        if self.context.skip_existing_commits:
            central_status_file = os.path.join(
                self.context.combined_output_folder, "compilation-results.json"
            )
            if os.path.isfile(central_status_file):
                try:
                    with open(central_status_file, "r", encoding="utf-8") as f:
                        all_status = json.load(f)
                    if commit_id in all_status:
                        logging.info(
                            f"[{commit_id}] Already in compilation-results.json - skipping (skip_existing_commits=True)"
                        )
                        return
                except Exception as e:
                    logging.warning(f"[{commit_id}] Could not check compilation-results.json: {e}")

        # Load project ID - will raise FileNotFoundError if JSON is missing
        project_id = self.metadata_loader.load_project_id(commit_id)
        logging.info(f"[{commit_id}] Loaded project ID: {project_id}")

        # Clean combined output folder if it exists
        combined_commit_folder = os.path.join(self.context.combined_output_folder, commit_id)
        if os.path.exists(combined_commit_folder):
            logging.info(f"[{commit_id}] Removing existing combined folder: {combined_commit_folder}")
            shutil.rmtree(combined_commit_folder)

        # Clear any previous entry for this commit in the central results JSON
        self._clear_compilation_status_entry(commit_id, combined_commit_folder)

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

        # Prepare a per-commit copy of the template with updated input resources
        self.template_adjuster.prepare_adjusted_template(
            commit_id, combined_commit_folder, project_id
        )

        # Compile template with Maven (always do this)
        compile_success = self.template_adjuster.compile_template(
            commit_id, combined_commit_folder, project_id
        )

        # Run project tests inside the Docker image (creates test logs) only if rules were executed
        # (compile_success will be False for NO_RULES and for rule compilation/execution failures)
        if compile_success:
            self._run_project_tests(commit_id, combined_commit_folder, project_id)

        # Save compilation status to JSON (includes failureCategory derived from test log)
        self.status_recorder.save_status(
            commit_id, combined_commit_folder, compile_success
        )

    def _clear_compilation_status_entry(self, commit_id: str, combined_commit_folder: str) -> None:
        """Remove any existing entry for this commit from compilation-results.json.

        Useful when re-running a specific commit so that stale data does not remain
        if the new run fails before saving status.
        """
        central_status_file = os.path.join(os.path.dirname(combined_commit_folder), "compilation-results.json")
        if not os.path.isfile(central_status_file):
            return
        try:
            with open(central_status_file, "r", encoding="utf-8") as f:
                all_status = json.load(f)
            if commit_id in all_status:
                del all_status[commit_id]
                with open(central_status_file, "w", encoding="utf-8") as f:
                    json.dump(all_status, f, indent=2)
                logging.info(f"[{commit_id}] Removed previous entry from {central_status_file}")
        except Exception as e:
            logging.warning(f"[{commit_id}] Failed to clear previous compilation status: {e}")

    def _run_project_tests(self, commit_id: str, combined_commit_folder: str, project_id: str) -> None:
        """Create a container from the benchmark Docker image, overwrite the project at root,
        run `mvn test`, and copy the test log back into the combined output folder."""
        try:
            _, docker_image = self.metadata_loader.load_metadata(commit_id)
        except Exception as e:
            logging.warning(f"[{commit_id}] Could not load Docker image for tests: {e} - skipping mvn test")
            return

        if not self.docker.pull_image(docker_image, commit_id):
            logging.warning(f"[{commit_id}] Failed to pull Docker image for tests - skipping mvn test")
            return

        container_name = self.docker.create_container(docker_image, commit_id)
        if not container_name:
            logging.warning(f"[{commit_id}] Failed to create Docker container for tests - skipping mvn test")
            return

        project_root = os.path.join(combined_commit_folder, project_id)
        if not os.path.isdir(project_root):
            logging.warning(f"[{commit_id}] Project root not found at {project_root} - skipping mvn test")
            return

        try:
            # First, run mvn test on the original project inside the container
            pre_test_cmd = f"cd /{project_id} && mvn test"
            pre_ok, pre_output = self.docker.exec_in_container(
                container_name,
                ["sh", "-c", pre_test_cmd],
                commit_id,
            )
            pre_log_file = os.path.join(combined_commit_folder, "pre-modification.log")
            with open(pre_log_file, "w", encoding="utf-8") as f:
                f.write(pre_output)
            if pre_ok:
                logging.info(f"[{commit_id}] Pre-modification mvn test completed successfully; log saved to {pre_log_file}")
            else:
                logging.warning(f"[{commit_id}] Pre-modification mvn test failed; log saved to {pre_log_file}")

            # Remove existing project path inside the container, then copy the updated project there
            rm_ok, _ = self.docker.exec_in_container(
                container_name,
                ["sh", "-c", f"rm -rf /{project_id}"],
                commit_id,
            )
            if not rm_ok:
                logging.warning(f"[{commit_id}] Failed to remove existing /{project_id} in container - tests may be inconsistent")

            if not self.docker.copy_to_container(project_root, container_name, f"/{project_id}", commit_id):
                logging.warning(f"[{commit_id}] Failed to copy project into container - skipping mvn test")
                return

            # Run mvn test inside the project directory (after modifications)
            test_cmd = f"cd /{project_id} && mvn test"
            ok, output = self.docker.exec_in_container(
                container_name,
                ["sh", "-c", test_cmd],
                commit_id,
            )

            # Save test log regardless of success (after modifications)
            log_file = os.path.join(combined_commit_folder, "after-modifications.log")
            with open(log_file, "w", encoding="utf-8") as f:
                f.write(output)

            if ok:
                logging.info(f"[{commit_id}] mvn test completed successfully; log saved to {log_file}")
            else:
                logging.warning(f"[{commit_id}] mvn test failed; log saved to {log_file}")
        finally:
            # Intentionally keep the container alive for inspection instead of removing it.
            logging.info(f"[{commit_id}] Keeping test container {container_name} alive for inspection")