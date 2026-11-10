"""Proposed CausalPy source identity adapter; only for a reviewed Docker packet."""
import hashlib
from pathlib import Path, PurePosixPath
import stat


def require(value, message):
    if not value:
        raise RuntimeError(message)


def verify(source, contract, command, prefix):
    """command(label, argv, seconds) must use the reviewed bounded controller."""
    source = Path(source)
    expected = contract['tracked_source']
    require(len(expected) == 339, 'Wrong sealed CausalPy source inventory')
    def git(label, *args):
        return command(prefix + '-' + label,
                       ['git', '-C', str(source), *args], 20)
    require(git('commit', 'rev-parse', 'HEAD').decode().strip() == contract['base'],
            'Source commit changed')
    require(git('tree', 'rev-parse', 'HEAD^{tree}').decode().strip() == contract['source_tree'],
            'Source root tree changed')
    require(git('clean', 'status', '--porcelain') == b'', 'Source is not pristine')
    raw = git('inventory', 'ls-tree', '-rz', '--full-tree', 'HEAD')
    observed = {}
    for item in raw.split(b'\0'):
        if not item:
            continue
        header, name = item.split(b'\t', 1)
        mode, kind, oid = header.decode().split()
        name = name.decode('utf-8')
        path = PurePosixPath(name)
        require(kind == 'blob' and not path.is_absolute() and '..' not in path.parts,
                'Unexpected Git entry or unsafe path')
        require(name not in observed, 'Duplicate Git path')
        observed[name] = (mode, oid)
    require(set(observed) == set(expected), 'Full tracked source inventory differs')
    total = 0
    for name, item in expected.items():
        mode, oid = observed[name]
        require((mode, oid) == (item['mode'], item['git_oid']), 'Source OID/mode differs: ' + name)
        target = source / name
        info = target.lstat()
        require(stat.S_ISREG(info.st_mode) and not target.is_symlink(), 'Not a regular source file: ' + name)
        require(bool(info.st_mode & stat.S_IXUSR) == (mode == '100755'), 'Executable mode differs: ' + name)
        require(info.st_size == item['bytes'], 'Source length differs: ' + name)
        payload = target.read_bytes()
        blob = b'blob ' + str(len(payload)).encode() + b'\0' + payload
        require(hashlib.sha1(blob).hexdigest() == oid, 'Working source content differs: ' + name)
        total += len(payload)
    return {'base': contract['base'], 'tree': contract['source_tree'],
            'tracked_files': len(expected), 'tracked_bytes': total, 'pristine': True}
