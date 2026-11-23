# SpecPack toolchain usage

```python
from qhp_mcp.specpack import validate_pack, attest_pack

files = {
    "specpack.yaml": 'specpack_version: "0.1.0"\nentrypoints:\n  spec: "spec/SPEC.md"\n  tests: "tests/tests.yaml"\n',
    "spec/SPEC.md": "# My spec\n",
    "tests/tests.yaml": 'version: "0.1.0"\n',
}

report = validate_pack(files)   # {"valid": True, "errors": [], "warnings": []}
lock = attest_pack(files)       # {"files": {"spec/SPEC.md": {"sha256": "...", "bytes": 10}, ...}}
```

Both functions are pure and deterministic: no filesystem or network access; hashes use UTF-8 bytes.
Conformance: `tests/specpack/test_specpack.py` runs the official `tests.yaml` (4 of 4 pass).
In this project, `verify_data_pull` packages each verification as a SpecPack (request, checks, null counts,
source manifest), validates it with `validate_pack`, and fingerprints it with `attest_pack`.