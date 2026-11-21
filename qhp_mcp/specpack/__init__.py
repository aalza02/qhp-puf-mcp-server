"""SpecPack v0.1.0 toolchain: validate_pack and attest_pack.

Both are pure functions. They take a dict of {path: file_contents} and return a result.
No filesystem access, no network, deterministic. Implements spec/SPEC.md from the
specpack-toolchain pack provided by the GSA hackathon.
"""

from __future__ import annotations

import hashlib

import yaml

SPECPACK_VERSION = "0.1.0"
MANIFEST_PATH = "specpack.yaml"


def validate_pack(files: dict[str, str]) -> dict:
    """Check whether a SpecPack is well-formed.

    Args:
        files: mapping of path -> UTF-8 file contents (e.g. {"specpack.yaml": "...", ...}).

    Returns:
        {"valid": bool, "errors": [...], "warnings": [...]}.
    """
    errors: list[dict] = []
    warnings: list[dict] = []

    # 1. specpack.yaml must exist
    if MANIFEST_PATH not in files:
        return {
            "valid": False,
            "errors": [{"code": "MISSING_MANIFEST", "path": MANIFEST_PATH}],
            "warnings": warnings,
        }

    # 2. specpack.yaml must parse as YAML
    try:
        manifest = yaml.safe_load(files[MANIFEST_PATH])
    except yaml.YAMLError:
        return {
            "valid": False,
            "errors": [{"code": "INVALID_YAML", "path": MANIFEST_PATH}],
            "warnings": warnings,
        }

    if not isinstance(manifest, dict):
        return {
            "valid": False,
            "errors": [{"code": "INVALID_YAML", "path": MANIFEST_PATH}],
            "warnings": warnings,
        }

    # 3. specpack_version must equal "0.1.0"
    version = manifest.get("specpack_version")
    if version != SPECPACK_VERSION:
        errors.append({"code": "INVALID_SPECPACK_VERSION", "expected": SPECPACK_VERSION})

    # 4. entrypoints.spec and entrypoints.tests must point to paths that exist in files
    entrypoints = manifest.get("entrypoints", {})
    if isinstance(entrypoints, dict):
        for key in ("spec", "tests"):
            path = entrypoints.get(key)
            if path and path not in files:
                errors.append({"code": "MISSING_ENTRYPOINT", "key": key, "path": path})

    return {"valid": len(errors) == 0, "errors": errors, "warnings": warnings}


def attest_pack(files: dict[str, str]) -> dict:
    """Produce a SHA-256 lock for every file in the pack.

    Args:
        files: mapping of path -> UTF-8 file contents.

    Returns:
        {"files": {"<path>": {"sha256": "<hex>", "bytes": <int>}, ...}}.
    """
    lock_files: dict[str, dict] = {}
    for path in sorted(files):
        content_bytes = files[path].encode("utf-8")
        lock_files[path] = {
            "sha256": hashlib.sha256(content_bytes).hexdigest(),
            "bytes": len(content_bytes),
        }
    return {"files": lock_files}