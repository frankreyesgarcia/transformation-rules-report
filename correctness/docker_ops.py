import logging
import os
import subprocess
from typing import List, Optional, Tuple


class DockerOps:
    """Thin wrapper around Docker CLI operations using subprocess."""

    @staticmethod
    def run(cmd: List[str]) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, text=True, capture_output=True)

    def pull_image(self, image_ref: str, commit_id: str) -> bool:
        logging.info(f"[{commit_id}] Pulling Docker image {image_ref}")
        result = self.run(["docker", "pull", image_ref])
        if result.returncode != 0:
            logging.error(f"[{commit_id}] Docker pull failed: {result.stderr.strip()}")
            return False
        logging.info(f"[{commit_id}] Pulled image successfully")
        return True

    def create_container(self, image_ref: str, commit_id: str) -> Optional[str]:
        import uuid
        container_name = f"breaking-{commit_id[:12]}-{uuid.uuid4().hex[:8]}"
        # Use docker run -d to create and start container in detached mode
        result = self.run(["docker", "run", "-d", "-i", "--name", container_name, image_ref, "sleep", "infinity"])
        if result.returncode != 0:
            logging.error(f"[{commit_id}] Docker run failed: {result.stderr.strip()}")
            return None
        logging.info(f"[{commit_id}] Created and started container {container_name}")
        return container_name

    def remove_container(self, container_name: str, commit_id: str) -> None:
        result = self.run(["docker", "rm", "-f", container_name])
        if result.returncode != 0:
            logging.warning(f"[{commit_id}] Failed to remove container {container_name}: {result.stderr.strip()}")
        else:
            logging.info(f"[{commit_id}] Removed container {container_name}")

    def copy_from_container(self, container_name: str, src_path: str, dest_parent: str, commit_id: str) -> bool:
        os.makedirs(dest_parent, exist_ok=True)
        logging.info(f"[{commit_id}] Copying {src_path} from container to {dest_parent}")
        result = self.run(["docker", "cp", f"{container_name}:{src_path}", dest_parent])
        if result.returncode != 0:
            logging.error(f"[{commit_id}] Docker cp failed: {result.stderr.strip()}")
            return False
        import os as _os
        logging.info(f"[{commit_id}] Project extracted to {_os.path.join(dest_parent, _os.path.basename(src_path))}")
        return True

    def exec_in_container(self, container_name: str, cmd: List[str], commit_id: str) -> Tuple[bool, str]:
        """Execute command in container and return stdout."""
        full_cmd = ["docker", "exec", container_name] + cmd
        logging.info(f"[{commit_id}] Executing in container: {' '.join(cmd)}")
        result = self.run(full_cmd)
        if result.returncode != 0:
            logging.warning(f"[{commit_id}] Command failed: {result.stderr.strip()}")
            return False, result.stdout + "\n" + result.stderr
        return True, result.stdout


