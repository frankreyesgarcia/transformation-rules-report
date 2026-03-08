#!/usr/bin/env python3
"""
Evaluates alignment between breaking-change errors and the generated Spoon
transformation rules for each commit in agent/gemini/spoon/.

For each commit:
  - breaking-classifier-report.json  → the compilation errors to fix
  - input_change-impact.json         → the API changes that caused each error
  - spoon-base-template/src/…/*.java → the generated Spoon processors

The script checks whether the generated processor code addresses every
error/API-change reported in the JSON files and outputs a structured
evaluation JSON keyed by commit hash.
"""

import json
import os
import re
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).parent
SPOON_DIR = REPO_ROOT / "agent" / "gemini" / "spoon"
OUTPUT_FILE = REPO_ROOT / "spoon_alignment_evaluation.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_json(path: Path) -> dict | list | None:
    """Load a JSON file; return None if missing or invalid."""
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def read_processor_sources(commit_dir: Path) -> dict[str, str]:
    """Return {filename: source_code} for all Java files in github/chains/."""
    chains_dir = (
        commit_dir
        / "spoon-base-template"
        / "src" / "main" / "java" / "github" / "chains"
    )
    sources = {}
    if chains_dir.is_dir():
        for java_file in chains_dir.glob("*.java"):
            try:
                sources[java_file.name] = java_file.read_text(encoding="utf-8")
            except OSError:
                sources[java_file.name] = ""
    return sources


def extract_symbol_from_details(details: list[str]) -> str | None:
    """
    Parse the Java compiler 'details' list and return the bare symbol name.

    Examples:
      "symbol:   method setUseClientMode(boolean)"  -> "setUseClientMode"
      "symbol:   class TFramedTransport"            -> "TFramedTransport"
      "symbol:   variable PEER_ADDRESS"             -> "PEER_ADDRESS"
    """
    for detail in details:
        m = re.search(r"symbol:\s+(?:method|class|variable|constructor)\s+([\w.<>$,\s]+)", detail)
        if m:
            raw = m.group(1).strip()
            # strip parameter list if present: "foo(int, boolean)" -> "foo"
            name = re.split(r"[\s(<]", raw)[0]
            return name
    return None


def collect_api_changes_for_commit(impact: dict) -> list[dict]:
    """
    Flatten input_change-impact.json into a list of unique API-change records
    that are REMOVED or MODIFIED (the ones that actually need to be handled).

    Each record:
        {
          "name":              str,   # simple name (e.g. "setUseClientMode")
          "qualified":         str,   # fully qualified signature
          "elementType":       str,   # METHOD / FIELD / CLASS / CONSTRUCTOR
          "changeStatus":      str,   # REMOVED / MODIFIED
          "compatibilityType": str,   # e.g. "METHOD_REMOVED"
          "file":              str,   # source file that triggered the error
          "errorMessage":      str,   # compiler error message
        }
    """
    seen = set()
    changes = []

    for file_entry in impact.get("files", []):
        file_path = file_entry.get("filePath", "")
        for err in file_entry.get("errors", []):
            err_msg = err.get("message", "")
            change_impact = err.get("changeImpact") or {}
            for construct in change_impact.get("constructs", []):
                for api_change in construct.get("apiChanges", []):
                    status = api_change.get("changeStatus", "")
                    if status in ("UNCHANGED",):
                        continue  # only care about actual breaking changes

                    name = api_change.get("name") or api_change.get("simpleName") or ""
                    qualified = api_change.get("qualifiedSignature") or ""
                    element_type = api_change.get("elementType", "")
                    compat_changes = api_change.get("compatibilityChanges") or []
                    compat_type = compat_changes[0]["type"] if compat_changes else ""

                    key = (qualified or name, file_path)
                    if key in seen:
                        continue
                    seen.add(key)

                    changes.append({
                        "name": name,
                        "qualified": qualified,
                        "elementType": element_type,
                        "changeStatus": status,
                        "compatibilityType": compat_type,
                        "file": file_path,
                        "errorMessage": err_msg,
                    })

    return changes


def symbol_covered(symbol: str, sources: dict[str, str]) -> bool:
    """
    Return True if the symbol (or its simple name) appears as a string literal
    in any processor source.

    Handles both fully-qualified names (e.g. "org.foo.Bar") and simple names
    (e.g. "Bar") by checking both forms.
    """
    if not symbol:
        return False
    candidates = {symbol}
    # Also check the last dotted segment as the simple name
    if "." in symbol:
        candidates.add(symbol.rsplit(".", 1)[-1])
    for candidate in candidates:
        pattern = f'"{candidate}"'
        for source in sources.values():
            if pattern in source:
                return True
    return False


def evaluate_commit(commit_hash: str, commit_dir: Path) -> dict:
    """
    Build the evaluation record for one commit.

    Returns a dict with:
        - commit metadata
        - errors extracted from breaking-classifier-report.json
        - api_changes extracted from input_change-impact.json
        - per-error and per-change coverage details
        - overall alignment verdict + score
    """
    result = {
        "commit": commit_hash,
        "dependency": None,
        "project": None,
        "total_errors": 0,
        "total_api_changes_needed": 0,
        "covered_api_changes": 0,
        "uncovered_api_changes": 0,
        "coverage_ratio": 0.0,
        "alignment_verdict": "NO_RULES_FOUND",
        "errors": [],
        "api_changes": [],
        "processor_files": [],
        "notes": [],
    }

    # ---- Load files --------------------------------------------------------
    classifier = load_json(commit_dir / "breaking-classifier-report.json")
    impact = load_json(commit_dir / "input_change-impact.json")
    sources = read_processor_sources(commit_dir)

    result["processor_files"] = list(sources.keys())

    if not sources:
        result["notes"].append("No processor Java files found in spoon-base-template.")

    # ---- Metadata from impact file ----------------------------------------
    if impact:
        dep = impact.get("dependency", {})
        result["project"] = impact.get("project")
        if dep:
            result["dependency"] = {
                "groupId": dep.get("groupId"),
                "artifactId": dep.get("artifactId"),
                "previousVersion": dep.get("previousVersion"),
                "newVersion": dep.get("newVersion"),
            }

    # ---- Errors from classifier --------------------------------------------
    errors_summary = []
    if classifier:
        for file_entry in classifier.get("errorsByFile", []):
            file_path = file_entry.get("filePath", "")
            for err in file_entry.get("errors", []):
                details = err.get("details", [])
                symbol = extract_symbol_from_details(details)
                covered = symbol_covered(symbol, sources) if symbol else False

                errors_summary.append({
                    "file": file_path,
                    "line": err.get("lineNumber"),
                    "message": err.get("message"),
                    "details": details,
                    "extracted_symbol": symbol,
                    "symbol_covered_in_rules": covered,
                })

    result["errors"] = errors_summary
    result["total_errors"] = len(errors_summary)

    # ---- API changes from impact -------------------------------------------
    api_changes = []
    if impact:
        raw_changes = collect_api_changes_for_commit(impact)
        for change in raw_changes:
            covered = symbol_covered(change["name"], sources)
            api_changes.append({
                **change,
                "covered_in_rules": covered,
            })

    result["api_changes"] = api_changes
    result["total_api_changes_needed"] = len(api_changes)
    covered_count = sum(1 for c in api_changes if c["covered_in_rules"])
    uncovered_count = len(api_changes) - covered_count
    result["covered_api_changes"] = covered_count
    result["uncovered_api_changes"] = uncovered_count

    # ---- Alignment verdict -------------------------------------------------
    total = len(api_changes)

    if not sources:
        verdict = "NO_RULES_FOUND"
        ratio = 0.0
    elif total == 0:
        # No API changes identified (impact file missing or no changes needed)
        # Fall back to symbol coverage from classifier details
        symbols = [e["extracted_symbol"] for e in errors_summary if e["extracted_symbol"]]
        if not symbols:
            verdict = "NO_ERRORS_IDENTIFIED"
            ratio = 0.0
        else:
            covered_syms = sum(1 for s in symbols if symbol_covered(s, sources))
            ratio = covered_syms / len(symbols)
            if ratio == 1.0:
                verdict = "FULL"
            elif ratio >= 0.5:
                verdict = "PARTIAL"
            else:
                verdict = "INSUFFICIENT"
    else:
        ratio = covered_count / total
        if ratio == 1.0:
            verdict = "FULL"
        elif ratio >= 0.75:
            verdict = "MOSTLY_COVERED"
        elif ratio >= 0.5:
            verdict = "PARTIAL"
        elif ratio > 0.0:
            verdict = "INSUFFICIENT"
        else:
            verdict = "NOT_COVERED"

    result["coverage_ratio"] = round(ratio, 4)
    result["alignment_verdict"] = verdict

    # ---- Summary note per uncovered change ---------------------------------
    uncovered_names = [c["name"] for c in api_changes if not c["covered_in_rules"]]
    if uncovered_names:
        result["notes"].append(
            f"Uncovered API changes: {', '.join(sorted(set(uncovered_names)))}"
        )

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not SPOON_DIR.is_dir():
        print(f"ERROR: spoon directory not found: {SPOON_DIR}", file=sys.stderr)
        sys.exit(1)

    # Gather all commit subdirectories (skip files like breaking-updates-results.json)
    commit_dirs = sorted(
        [d for d in SPOON_DIR.iterdir() if d.is_dir()]
    )

    print(f"Evaluating {len(commit_dirs)} commits …")

    evaluation = {}
    verdicts_count = {}

    for commit_dir in commit_dirs:
        commit_hash = commit_dir.name
        record = evaluate_commit(commit_hash, commit_dir)
        evaluation[commit_hash] = record

        v = record["alignment_verdict"]
        verdicts_count[v] = verdicts_count.get(v, 0) + 1

        status = (
            f"  {commit_hash[:10]}  "
            f"errors={record['total_errors']:2d}  "
            f"api_changes={record['total_api_changes_needed']:3d}  "
            f"covered={record['covered_api_changes']:3d}  "
            f"ratio={record['coverage_ratio']:.2f}  "
            f"{v}"
        )
        print(status)

    # ---- Write output -------------------------------------------------------
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(evaluation, f, indent=2, ensure_ascii=False)

    print(f"\nResults written to: {OUTPUT_FILE}")
    print("\nVerdict summary:")
    for verdict, count in sorted(verdicts_count.items(), key=lambda x: -x[1]):
        print(f"  {verdict:<20} {count:3d} commits")

    total = len(commit_dirs)
    fully_or_mostly = sum(
        verdicts_count.get(v, 0) for v in ("FULL", "MOSTLY_COVERED")
    )
    print(f"\n  {fully_or_mostly}/{total} commits have FULL or MOSTLY_COVERED rules.")


if __name__ == "__main__":
    main()
