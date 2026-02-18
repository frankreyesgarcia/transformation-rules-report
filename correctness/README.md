# Breaking Change Commit Processing (Initial Setup)

This tool performs the initial setup for processing breaking-change commits:
- Resolves commit IDs from a main JSON file.
- Extracts benchmark project directories from Docker images.
- Copies generated rules per commit.
- Handles errors per commit without stopping the full run.

## Requirements
- macOS/Linux with Docker CLI installed and running.
- Access to `ghcr.io/chains-project/breaking-updates` images (login if private).

## Usage
Run the script from the repository root or the `correctness` folder.

### As a script
```bash
# From repo root
python3 correctness/process_breaking_commits.py \
  gemini-3-pro-preview/breaking-updates-results.json \
  gemini-3-pro-preview \
  gemini-3-pro-preview \
  /tmp/projects_output \
  /tmp/rules_output

# Process a single specific commit
python3 correctness/process_breaking_commits.py \
  gemini-3-pro-preview/breaking-updates-results.json \
  gemini-3-pro-preview \
  gemini-3-pro-preview \
  /tmp/projects_output \
  /tmp/rules_output \
  --specific_commit 00a7cc31784ac4a9cc27d506a73ae589d6df36d6
```

### As a module
```bash
# Run using python -m (from repo root)
python3 -m correctness.process_breaking_commits \
  gemini-3-pro-preview/breaking-updates-results.json \
  gemini-3-pro-preview \
  gemini-3-pro-preview \
  /tmp/projects_output \
  /tmp/rules_output
```

## Module Layout
- `__init__.py`: Package initialization; exports all main classes.
- `process_breaking_commits.py`: CLI entrypoint.
- `commit_resolver.py`: Resolve commit IDs and validate specific commit.
- `metadata_loader.py`: Load per-commit metadata and extract `project`.
- `docker_ops.py`: Pull, create, copy, and remove Docker container.
- `rules_copier.py`: Copy rules for a given commit.
- `processor.py`: Coordinate per-commit processing using the above modules.

## Arguments
- `json_file`: Path to the main JSON file containing multiple `commitId` keys.
- `benchmark_folder`: Folder containing individual `<commitId>.json` files with a `project` field.
- `rules_folder`: Folder containing subfolders named by `commitId` with generated rules.
- `projects_output_folder`: Base output directory for extracted projects.
- `rules_output_folder`: Base output directory for copied rules.
- `--specific_commit`: Optional commit ID to process only that one.

## Output Structure
```
projects_output_folder/
└── <commitId>/<project_id>/

rules_output_folder/
└── <commitId>/
```

## Notes
- If metadata file or `project` is missing, the commit's project extraction is skipped; rules copy still attempted.
- Docker ops use `subprocess` and ensure container cleanup.
- Rule contents are copied as-is (no validation or modification).
- This is a Python package. Import classes directly: `from correctness import CommitProcessor, ProcessingContext`.
