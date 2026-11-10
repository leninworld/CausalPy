"""Unexecuted Docker-only primitives for the complete #1850 validation packet.

Not a standalone execution authorization. The final controller supplies the
original workflow deadline and retains the returned process/memory evidence.
"""

from dataclasses import dataclass
import atexit
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import selectors
import signal
import stat
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
import zlib


GIB = 1024**3


def check_csv_header(path, expected):
    with path.open(newline="", encoding="utf8") as source:
        actual = next(csv.reader(source))
    require(actual == expected, "Dataset header mismatch")
    return actual


def decode_evidence(record, byte_cap):
    require(0 <= record["bytes"] <= byte_cap, "Evidence length cap")
    encoded_cap = byte_cap + byte_cap // 100 + 1024
    require(len(record["base64"]) <= 4 * ((encoded_cap + 2) // 3), "Encoded evidence cap")
    payload = base64.b64decode(record["base64"], validate=True)
    encoding = record.get("encoding", "base64")
    require(encoding in ("base64", "gzip-base64"), "Unknown evidence encoding")
    if encoding == "gzip-base64":
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        payload = decoder.decompress(payload, record["bytes"] + 1)
        require(decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail,
                "Incomplete/oversized compressed evidence")
    require(len(payload) == record["bytes"], "Evidence decoded length mismatch")
    require(hashlib.sha256(payload).hexdigest() == record["sha256"],
            "Evidence hash mismatch")
    return payload


def check_junit(paths, expected_ids, expected_failures=(), allowed_skips=()):
    expected, failed, skipped, seen = set(expected_ids), set(), set(), set()
    require(expected and len(expected) == len(expected_ids), "Empty/duplicate expected test IDs")
    for path in paths:
        for case in ET.parse(path).getroot().iter("testcase"):
            key = (case.get("classname"), case.get("name"))
            require(all(key) and key not in seen, "Missing/duplicate actual test ID")
            seen.add(key)
            if case.find("failure") is not None or case.find("error") is not None:
                failed.add(key)
            if case.find("skipped") is not None:
                skipped.add(key)
    require(seen == expected, "Test selection mismatch")
    require(failed == set(expected_failures), "Unexpected test failures")
    require(skipped == set(allowed_skips), "Unexpected test skips")
    return {"tests": len(seen), "failures": len(failed), "skips": len(skipped)}


def require(value, message):
    if not value:
        raise RuntimeError(message)


def docker_only():
    require(sys.platform == "linux" and Path("/.dockerenv").is_file(),
            "Project validation is Docker-only")


def cgroup_memory():
    docker_only()
    root = Path("/sys/fs/cgroup")
    peak = int((root / "memory.peak").read_text())
    current = int((root / "memory.current").read_text())
    raw_max = (root / "memory.max").read_text().strip()
    require(raw_max != "max", "Missing finite container memory limit")
    events = dict(line.split() for line in (root / "memory.events").read_text().splitlines())
    stats = dict(line.split() for line in (root / "memory.stat").read_text().splitlines())
    return {"peak": peak, "current": current, "limit": int(raw_max),
            "anon": int(stats["anon"]),
            "kernel": int(stats.get("kernel", 0)),
            "file": int(stats["file"]),
            "events": {key: int(value) for key, value in events.items()}}


class MemoryTracker:
    """Sampled non-reclaimable memory; never label this a continuous peak."""

    def __init__(self, trace=None, start=True):
        self.trace = trace
        self.lock = threading.Lock()
        self.count = 0
        self.peak = 0
        self.last = None
        self.max_gap = 0.0
        self.error = None
        self.done = threading.Event()
        self.thread = None
        if start:
            require(trace is not None and not trace.exists(), "Existing/missing memory trace")
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()

    def observe(self, memory, at):
        with self.lock:
            if self.last is not None:
                require(at >= self.last, "Non-monotonic memory sample")
                self.max_gap = max(self.max_gap, at - self.last)
            self.last = at
            self.count += 1
            self.peak = max(self.peak, memory["anon"] + memory["kernel"])
            if self.trace is not None:
                with self.trace.open("a") as output:
                    output.write(json.dumps({"at": at, "pid": os.getpid(),
                        "anon": memory["anon"], "kernel": memory["kernel"],
                        "file": memory["file"], "raw_cgroup_peak": memory["peak"]}) + "\n")

    def _loop(self):
        try:
            while not self.done.is_set():
                self.observe(cgroup_memory(), time.monotonic())
                self.done.wait(0.1)
        except BaseException as error:
            with self.lock:
                self.error = f"{type(error).__name__}: {error}"

    def stop(self):
        self.done.set()
        if self.thread is not None:
            self.thread.join(timeout=2)

    def snapshot(self, trace_prefix=False):
        with self.lock:
            data = {"sampled_nonreclaimable_peak": self.peak, "samples": self.count,
                    "last_monotonic": self.last, "max_gap_seconds": self.max_gap,
                    "nominal_interval_seconds": 0.1, "error": self.error,
                    "measurement": "sampled anon+kernel; raw kernel peak includes file cache"}
            if trace_prefix and self.trace is not None:
                payload = self.trace.read_bytes()
                data.update(trace=str(self.trace), trace_prefix_bytes=len(payload),
                            trace_prefix_sha256=hashlib.sha256(payload).hexdigest())
            return data


_tracker = None


def memory_tracker(root):
    global _tracker
    if _tracker is None:
        docker_only()
        phase = os.environ.get("VALIDATION_PHASE", "fixture")
        require(phase in ("setup", "validate", "fixture"), "Unexpected memory-trace phase")
        _tracker = MemoryTracker(root / ("nonreclaimable-memory-" + phase + ".jsonl"))
        atexit.register(_tracker.stop)
    return _tracker


def admit_from_baseline(baseline, actual_available_bytes):
    measured = baseline["sampled_nonreclaimable_peak"]
    require(measured > 0, "No measured baseline peak")
    require(not any(baseline["events"].get(key, 0) for key in ("oom", "oom_kill")),
            "Baseline encountered OOM")
    needed = 2 * measured + GIB
    require(needed <= min(actual_available_bytes, baseline["limit"]),
            "Full validation memory admission failed")
    return {"baseline": baseline, "required_bytes": needed,
            "actual_available_bytes": actual_available_bytes}


@dataclass
class Runtime:
    deadline_epoch: float
    evidence_dir: Path
    minimum_disk_bytes: int = 4 * GIB

    def __post_init__(self):
        self.memory = memory_tracker(self.evidence_dir)

    def guard(self):
        import shutil

        docker_only()
        require(self.memory.snapshot()["error"] is None, "Memory sampler failed")
        require(time.time() < self.deadline_epoch, "Original workflow deadline expired")
        require(shutil.disk_usage(self.evidence_dir).free >= self.minimum_disk_bytes,
                "Insufficient workspace disk")
        memory = cgroup_memory()
        require(not any(memory["events"].get(key, 0) for key in ("oom", "oom_kill")),
                "Container OOM event")
        require(memory["anon"] + memory["kernel"] < memory["limit"] * 0.90,
                "Non-reclaimable memory pressure stop")

    def run(self, label, argv, cwd, step_seconds, expected=(0,), output_cap=64 * 1024**2):
        require(label and all(c.isalnum() or c == "-" for c in label), "Bad label")
        self.guard()
        destination = self.evidence_dir / (label + ".log")
        require(not destination.exists(), "Existing step evidence; no automatic replay")
        started = time.monotonic()
        proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, start_new_session=True)
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        captured = 0
        error = None
        try:
            with destination.open("xb") as output:
                while selector.get_map() or proc.poll() is None:
                    self.guard()
                    require(time.monotonic() - started < step_seconds, "Step deadline")
                    for key, _ in selector.select(0.2):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        require(captured + len(chunk) <= output_cap, "Step output cap")
                        output.write(chunk)
                        captured += len(chunk)
                code = proc.wait(timeout=3)
                require(code in expected, f"Unexpected exit {code}")
        except BaseException as caught:
            error = f"{type(caught).__name__}: {caught}"
            raise
        finally:
            # Kill the owned group even if the group leader has already exited:
            # a child may otherwise outlive a timeout/output-cap failure.
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            if proc.poll() is None:
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=3)
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            selector.close()
            proc.stdout.close()
            record = {"argv": argv, "exit": proc.returncode, "expected": list(expected),
                      "seconds": time.monotonic() - started, "bytes": captured,
                      "leader_terminal": proc.poll() is not None, "error": error,
                      "cgroup": cgroup_memory(), "sampled_memory": self.memory.snapshot()}
            with (self.evidence_dir / (label + ".json")).open("x") as output:
                json.dump(record, output, indent=2)
        return record


def destination_for(root, name):
    raw = name.rstrip("/")
    require(raw and not raw.startswith(("/", "\\")) and "\\" not in raw,
            "Unsafe archive path")
    require(not any(part in ("", ".", "..") for part in raw.split("/")),
            "Unsafe archive component")
    relative = PurePosixPath(raw)
    require(not relative.is_absolute() and ":" not in relative.parts[0],
            "Unsafe absolute/drive path")
    target = root.joinpath(*relative.parts)
    require(target.resolve().is_relative_to(root.resolve()), "Archive path escapes root")
    return target


def extract_archive(archive, root, byte_cap, member_cap=100000):
    """Fresh owned output only; reject links/devices/sparse entries and duplicates."""
    docker_only()
    require(not root.exists(), "Extraction output must be fresh")
    root.mkdir(parents=True)
    seen, total, records = set(), 0, []

    def copy(name, size, stream, directory):
        nonlocal total
        target = destination_for(root, name)
        require(str(target) not in seen, "Duplicate archive entry")
        seen.add(str(target))
        require(len(seen) <= member_cap, "Archive member cap")
        if directory:
            target.mkdir(parents=True, exist_ok=True)
            return
        require(size >= 0 and total + size <= byte_cap, "Archive expansion cap")
        target.parent.mkdir(parents=True, exist_ok=True)
        sha, count = hashlib.sha256(), 0
        with target.open("xb") as output:
            while True:
                chunk = stream.read(min(65536, size - count + 1))
                if not chunk:
                    break
                count += len(chunk)
                require(count <= size, "Archive entry exceeds declared size")
                output.write(chunk)
                sha.update(chunk)
        require(count == size, "Truncated archive entry")
        total += count
        records.append({"path": name, "bytes": count, "sha256": sha.hexdigest()})

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as source:
            for member in source.infolist():
                mode = member.external_attr >> 16
                require(not stat.S_ISLNK(mode), "ZIP symlink rejected")
                require(stat.S_IFMT(mode) in (0, stat.S_IFREG, stat.S_IFDIR),
                        "ZIP special entry rejected")
                with source.open(member) as stream:
                    copy(member.filename, member.file_size, stream, member.is_dir())
    else:
        with tarfile.open(archive, "r:*") as source:
            for member in source:
                require(member.isdir() or member.isfile(), "TAR special/link entry rejected")
                require(not member.issparse(), "Sparse TAR entry rejected")
                if member.isdir():
                    copy(member.name, 0, io.BytesIO(), True)
                else:
                    with source.extractfile(member) as stream:
                        copy(member.name, member.size, stream, False)
    return records
