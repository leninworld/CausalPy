"""Real isolated Git/process/evidence fixtures, inside the proposed Docker execution only."""
import copy
import hashlib
import json
import time

from collect import reassemble
from evidence_export import export_evidence
from source_identity import verify
from validation_runtime import require


def main(runtime, run, work, evidence):
    root = work / 'd0-source'
    root.mkdir()
    for i in range(339):
        (root / ('file-%03d' % i)).write_bytes(b'fixture\n')
    (root / 'file-000').chmod(0o755)
    run('fixture-git-init', ['git', '-C', str(root), 'init'], 10)
    run('fixture-git-add', ['git', '-C', str(root), 'add', '.'], 10)
    run('fixture-git-commit', ['git', '-C', str(root), '-c', 'user.name=Synthetic',
        '-c', 'user.email=fixture@example.invalid', '-c', 'core.hooksPath=/dev/null',
        'commit', '--no-gpg-sign', '-m', 'Synthetic source identity fixture'], 10)
    base = run('fixture-base', ['git', '-C', str(root), 'rev-parse', 'HEAD'], 10).decode().strip()
    tree = run('fixture-tree', ['git', '-C', str(root), 'rev-parse', 'HEAD^{tree}'], 10).decode().strip()
    inventory = {}
    for i in range(339):
        name = 'file-%03d' % i
        raw = (root / name).read_bytes()
        inventory[name] = {'mode': '100755' if i == 0 else '100644', 'bytes': len(raw),
                          'git_oid': hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()}
    contract = dict(base=base, source_tree=tree, tracked_source=inventory)
    results = []
    verify(root, contract, run, 'fixture-success')
    results.append('actual339fileGitidentitysuccess')

    def rejects(name, changed, token):
        try:
            verify(root, changed, run, 'fixture-' + name)
        except RuntimeError as error:
            require(token in str(error), 'Wrong failure cause for ' + name)
            results.append(name)
        else:
            raise RuntimeError('False source pass: ' + name)

    changed = copy.deepcopy(contract); changed['base'] = '0' * 40
    rejects('wrong-commit', changed, 'Source commit changed')
    changed = copy.deepcopy(contract); changed['source_tree'] = '0' * 40
    rejects('wrong-tree', changed, 'Source root tree changed')
    changed = copy.deepcopy(contract)
    changed['tracked_source']['absent'] = changed['tracked_source'].pop('file-001')
    rejects('wrong-path', changed, 'Full tracked source inventory differs')
    changed = copy.deepcopy(contract); changed['tracked_source']['file-001']['mode'] = '100755'
    rejects('wrong-git-mode', changed, 'Source OID/mode differs')
    (root / 'file-001').write_bytes(b'dirty\n')
    rejects('dirty-tree', contract, 'Source is not pristine')
    (root / 'file-001').write_bytes(b'fixture\n')
    run('fixture-assume', ['git', '-C', str(root), 'update-index', '--assume-unchanged', 'file-001'], 10)
    (root / 'file-001').write_bytes(b'changed-length\n')
    rejects('working-length', contract, 'Source length differs')
    (root / 'file-001').write_bytes(b'badbytes')
    rejects('working-hash', contract, 'Working source content differs')
    (root / 'file-001').write_bytes(b'fixture\n')
    (root / 'file-001').chmod(0o755)
    rejects('working-mode', contract, 'Executable mode differs')
    (root / 'file-001').chmod(0o644)
    # This deletion is solely a synthetic file inside the owned hosted fixture directory.
    (root / 'file-001').unlink()
    (root / 'file-001').symlink_to(root / 'file-002')
    rejects('working-link', contract, 'Not a regular source file')
    try:
        run('fixture-nonzero', ['python', '-c', 'raise SystemExit(7)'], 10)
    except RuntimeError as error:
        require('Unexpected exit 7' in str(error), 'Nonzero command cause lost')
        results.append('real-nonzero-process-retained')
    else:
        raise RuntimeError('Nonzero command falsely passed')
    old = runtime.deadline_epoch
    runtime.deadline_epoch = time.time() - 1
    try:
        runtime.guard()
    except RuntimeError as error:
        require('Original workflow deadline expired' in str(error), 'Wrong expired guard cause')
        results.append('real-expired-deadline-rejected')
    else:
        raise RuntimeError('Expired deadline falsely passed')
    finally:
        runtime.deadline_epoch = old  # Restore unchanged original deadline after the negative fixture.
    payload = evidence / 'fixture-payload.json'
    payload.write_text(json.dumps({'synthetic': True, 'message': 'evidence roundtrip'}))
    emitted = []
    result = export_evidence(evidence, [payload], old,
        emit=lambda line, **unused: emitted.append(line))
    require(result['complete'], 'Fixture export incomplete')
    records = [json.loads(line.split('EVIDENCE667=', 1)[1]) for line in emitted]
    require(reassemble(records)['fixture-payload.json'] == payload.read_bytes(), 'Actual evidence decode mismatch')
    bad = copy.deepcopy(records); bad[-1]['sha256'] = '0' * 64
    try:
        reassemble(bad)
    except RuntimeError:
        results.append('corrupt-evidence-rejected')
    else:
        raise RuntimeError('Corrupt evidence falsely passed')
    results.append('actual-evidence-roundtrip')
    (evidence / 'fixture-proof.json').write_text(json.dumps({'passed': True, 'checks': results}, indent=2))
