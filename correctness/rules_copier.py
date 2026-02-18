import logging
import os
import shutil


class RulesCopier:
    """Copies generated rules from the rules folder into the output folder for a commit."""

    def __init__(self, rules_folder: str, rules_output_folder: str) -> None:
        self.rules_folder = rules_folder
        self.rules_output_folder = rules_output_folder

    def copy_for_commit(self, commit_id: str) -> None:
        src = os.path.join(self.rules_folder, commit_id)
        dest = os.path.join(self.rules_output_folder, commit_id)
        if not os.path.isdir(src):
            logging.warning(f"[{commit_id}] Rules source not found: {src}")
            return
        os.makedirs(self.rules_output_folder, exist_ok=True)
        try:
            shutil.copytree(src, dest, dirs_exist_ok=True)
            logging.info(f"[{commit_id}] Rules copied to {dest}")
        except Exception as e:
            logging.error(f"[{commit_id}] Failed to copy rules: {e}")
