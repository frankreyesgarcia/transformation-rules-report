#!/usr/bin/env python3

import argparse
import logging
import os
import sys

from .commit_resolver import CommitResolver
from .processor import ProcessingContext, CommitProcessor


def setup_logging() -> None:
    """Configure logging for the process."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Initial setup for processing breaking-change commits.")
    parser.add_argument("json_file", help="Path to main JSON file containing commitId keys")
    parser.add_argument("benchmark_folder", help="Folder with <commitId>.json files")
    parser.add_argument("rules_folder", help="Folder with subfolders named by commitId containing rules")
    parser.add_argument("projects_output_folder", help="Base output directory for extracted projects")
    parser.add_argument("combined_output_folder", help="Base output directory for combined projects and rules")
    parser.add_argument("--engine", dest="engine", choices=["spoon", "javaparser"], required=True, help="Rules engine: spoon or javaparser")
    parser.add_argument("--specific_commit", dest="specific_commit", default=None, help="If provided, process only this commitId")
    return parser.parse_args()


def main() -> None:
    setup_logging()
    args = parse_args()

    json_file = os.path.abspath(args.json_file)
    benchmark_folder = os.path.abspath(args.benchmark_folder)
    rules_folder = os.path.abspath(args.rules_folder)
    projects_output_folder = os.path.abspath(args.projects_output_folder)
    combined_output_folder = os.path.abspath(args.combined_output_folder)
    engine = args.engine

    # Validate that required input paths exist
    for path in [json_file, benchmark_folder, rules_folder]:
        if not os.path.exists(path):
            logging.error(f"Path not found: {path}")
            sys.exit(3)

    # Create output directories
    os.makedirs(projects_output_folder, exist_ok=True)
    os.makedirs(combined_output_folder, exist_ok=True)

    # Resolve commits from JSON file
    try:
        commits = CommitResolver(json_file).resolve(args.specific_commit)
    except Exception as e:
        logging.error(f"Failed to resolve commits: {e}")
        sys.exit(1)
    
    if not commits:
        logging.error("No commits to process.")
        sys.exit(4)

    processor = CommitProcessor(
        ProcessingContext(
            benchmark_folder=benchmark_folder,
            rules_folder=rules_folder,
            projects_output_folder=projects_output_folder,
            combined_output_folder=combined_output_folder,
            engine=engine,
        )
    )

    # Process each commit - stop on first error
    for commit_id in commits:
        try:
            processor.process(commit_id)
        except FileNotFoundError as e:
            logging.error(f"[{commit_id}] FATAL ERROR - Commit JSON file not found: {e}")
            sys.exit(5)
        except RuntimeError as e:
            logging.error(f"[{commit_id}] FATAL ERROR - {e}")
            sys.exit(6)
        except Exception as e:
            logging.error(f"[{commit_id}] FATAL ERROR - Unexpected error: {e}")
            sys.exit(7)

    logging.info(f"Successfully completed processing all {len(commits)} commit(s)")


if __name__ == "__main__":
    main()
