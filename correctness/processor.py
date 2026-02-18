import logging
import os
import shutil
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

        # Prepare a per-commit copy of the template with updated input resources
        self.template_adjuster.prepare_adjusted_template(
            commit_id, combined_commit_folder, project_id
        )

        # Compile template with Maven (always do this)
        compile_success = self.template_adjuster.compile_template(
            commit_id, combined_commit_folder
        )

        # Save compilation status to JSON
        self.status_recorder.save_status(
            commit_id, combined_commit_folder, compile_success
        )