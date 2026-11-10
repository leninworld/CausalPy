"""Bounded priority evidence export for the unexecuted hosted controller."""

import base64
import gzip
import hashlib
import json
import time


def select_evidence(root, paths, cap=63 * 1024**2, tail_bytes=32768):
    """Keep structured proof first, then log tails, then affordable full logs.

    Reserve one MiB outside this payload budget for the omission manifest.
    Never silently turn a missing critical proof into a successful export.
    """
    selected, omitted, used = {}, [], 0
    regular = []
    for path in sorted(set(paths)):
        name = str(path.relative_to(root))
        if path.is_symlink() or not path.is_file():
            omitted.append({"path": name, "reason": "not-regular", "critical": True})
            continue
        regular.append(path)
    def priority(path):
        name = str(path.relative_to(root))
        if path.name == "host-result.json":
            return (0, name)
        if path.name in ("lane-result.json", "setup-result.json"):
            return (1, name)
        if "admission" in path.name:
            return (2, name)
        if path.suffix == ".xml":
            return (3, name)
        if any(part.startswith("d0-") for part in path.relative_to(root).parts):
            return (4, name)
        return (5, name)

    critical = sorted((path for path in regular if path.suffix != ".log"), key=priority)
    logs = [path for path in regular if path.suffix == ".log"]
    for path in critical:
        size = path.stat().st_size
        if used + size <= cap:
            data = path.read_bytes()
            selected[str(path.relative_to(root))] = data
            used += len(data)
        else:
            omitted.append({"path": str(path.relative_to(root)), "bytes": size,
                            "reason": "structured-budget", "critical": True})
    for path in logs:
        size = path.stat().st_size
        count = min(size, tail_bytes)
        name = str(path.relative_to(root))
        if used + count <= cap:
            with path.open("rb") as source:
                source.seek(size - count)
                data = source.read(count)
            selected[name + ".tail"] = data
            used += len(data)
        else:
            omitted.append({"path": name + ".tail", "bytes": count,
                            "reason": "tail-budget", "critical": False})
    for path in logs:
        size = path.stat().st_size
        name = str(path.relative_to(root))
        if used + size <= cap:
            data = path.read_bytes()
            selected[name] = data
            used += len(data)
        else:
            omitted.append({"path": name, "bytes": size,
                            "reason": "full-log-budget", "critical": False})
    return selected, omitted


def export_evidence(root, paths, absolute, emit=print, cap=63 * 1024**2, tail_bytes=32768):
    selected, omissions = select_evidence(root, paths, cap, tail_bytes)
    inventory = {name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                 for name, data in selected.items()}
    # Structured records precede all log content; manifest itself goes first.
    summary = {"selected": inventory, "omissions": omissions,
               "critical_complete": not any(item["critical"] for item in omissions)}
    manifest = json.dumps(summary, sort_keys=True).encode()
    if len(manifest) > 1024**2:
        raise RuntimeError("Evidence inventory exceeds reserved one MiB")
    entries = [("evidence-export.json", manifest), *selected.items()]
    sent = []
    for name, data in entries:
        parts = max(1, (len(data) + 8191) // 8192)
        file_sha = hashlib.sha256(data).hexdigest()
        for index in range(parts):
            if time.time() >= absolute:
                return {"complete": False, "reason": "absolute-deadline",
                        "sent": sent, "critical_complete": False}
            piece = data[index * 8192:(index + 1) * 8192]
            record = {"path": name, "bytes": len(piece),
                      "sha256": hashlib.sha256(piece).hexdigest(), "encoding": "gzip-base64",
                      "index": index, "parts": parts, "file_bytes": len(data),
                      "file_sha256": file_sha,
                      "base64": base64.b64encode(gzip.compress(piece)).decode("ascii")}
            encoded = json.dumps(record)
            if len(encoded.encode()) >= 14000:
                raise RuntimeError("Evidence log line cap")
            emit("EVIDENCE667=" + encoded, flush=True)
        sent.append(name)
    return {"complete": True, "sent": sent, "critical_complete": summary["critical_complete"],
            "omissions": omissions}
