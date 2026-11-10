"""Reassemble bounded public validation evidence from already downloaded job logs."""

import argparse
import hashlib
import json
import sys
from pathlib import Path, PurePosixPath

from validation_runtime import decode_evidence, require


def reassemble(records, cap=64 * 1024**2):
    groups = {}
    total = 0
    for record in records:
        name = record["path"]
        path = PurePosixPath(name)
        require(not path.is_absolute() and ".." not in path.parts and str(path) == name,
                "Unsafe evidence path")
        require(0 <= record["file_bytes"] <= cap, "Evidence file cap")
        count = max(1, (record["file_bytes"] + 8191) // 8192)
        require(record["parts"] == count and 0 <= record["index"] < count,
                "Evidence chunk range mismatch")
        require(record["bytes"] <= 8192, "Evidence chunk cap")
        payload = decode_evidence(record, 8192)
        group = groups.setdefault(name, {"file_bytes": record["file_bytes"],
                                        "file_sha256": record["file_sha256"],
                                        "parts": count, "chunks": {}})
        require(group["file_bytes"] == record["file_bytes"] and
                group["file_sha256"] == record["file_sha256"], "Evidence metadata changed")
        require(record["index"] not in group["chunks"], "Duplicate evidence chunk")
        group["chunks"][record["index"]] = payload
        total += len(payload)
        require(total <= cap, "Total evidence cap")
    require(groups, "No actual evidence records")
    files = {}
    for name, group in groups.items():
        require(len(group["chunks"]) == group["parts"], "Missing evidence chunks")
        payload = b"".join(group["chunks"][index] for index in range(group["parts"]))
        require(len(payload) == group["file_bytes"] and
                hashlib.sha256(payload).hexdigest() == group["file_sha256"],
                "Reassembled evidence identity mismatch")
        files[name] = payload
    return files


def main():
    if len(sys.argv) > 1 and sys.argv[1] == '--compare-environments':
        from compare_environments import main as comparison_main
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        comparison_main()
        return
    parser = argparse.ArgumentParser()
    parser.add_argument("job_log", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    require(args.job_log.stat().st_size <= 128 * 1024**2, "Job log cap")
    root = args.destination.resolve()
    require(root.is_relative_to(Path.cwd().resolve()) and not root.exists(),
            "Evidence destination must be fresh and inside this workspace")
    records = []
    for line in args.job_log.read_text().splitlines():
        marker = "EVIDENCE667="
        if marker in line:
            records.append(json.loads(line.split(marker, 1)[1]))
    files = reassemble(records)
    root.mkdir(parents=True)
    manifest = {}
    for name, payload in files.items():
        path = root / name
        require(path.resolve().is_relative_to(root), "Evidence output escapes root")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as output:
            output.write(payload)
        manifest[name] = {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    (root / "collected-manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps({"files": len(files), "bytes": sum(map(len, files.values()))}))


if __name__ == "__main__":
    main()
