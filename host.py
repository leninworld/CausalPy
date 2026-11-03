"""Owned hosted-Docker controller; no Mac engine or private host mounts."""
from pathlib import Path
import hashlib
import json
import math
import os
import platform
import re
import selectors
import shutil
import signal
import subprocess
import time
import urllib.request

P = Path(__file__).resolve().parent
SOURCE = P.parent / 'source'
R = P.parent / 'runtime'
PREPARED = P.parent / 'prepared'
M = json.loads((P / 'manifest.json').read_text())
assert M['runtime_approved'] is True and M['public_write_approved'] is True, 'UNAPPROVED_PACKET'
KIND = os.environ['VALIDATION_KIND']
MODE = os.environ.get('VALIDATION_MODE', 'baseline')
PYTHON = os.environ.get('VALIDATION_PYTHON', '3.12')
START_EPOCH = float(os.environ['VALIDATION_START_EPOCH'])
END = START_EPOCH + M['target_seconds']
WORK_END = min(END, START_EPOCH + M['prepare_cap_seconds']) if KIND == 'prepare' else END
OWN = None
LAST_SAMPLE = 0
PHASE = KIND + '-' + MODE
LOGS = []
SAMPLES = []
RESULT = {'status': 'PREPARING', 'full_validation': False, 'lane_pass': False,
          'kind': KIND, 'mode': MODE, 'python': PYTHON,
          'global_start_epoch': START_EPOCH, 'global_target_epoch': END,
          'absolute_epoch': START_EPOCH + M['absolute_seconds']}
routes = [line.split()[0] for line in Path('/proc/net/route').read_text().splitlines()[1:]
          if line.split()[1] == '00000000']
assert len(set(routes)) == 1
RX = Path('/sys/class/net') / routes[0] / 'statistics/rx_bytes'
NETWORK_START = int(RX.read_text())
J = {'network_gib': M['network_gib'][KIND]}

def guard():
    assert time.time() < WORK_END, 'GLOBAL_TARGET_OR_PREPARE_DEADLINE'
    assert shutil.disk_usage(P).free >= 4 * 1024**3, 'HOST_DISK_PRESSURE'
    available = int(re.search(r'^MemAvailable:\s+(\d+)', Path('/proc/meminfo').read_text(), re.M)[1]) * 1024
    assert available >= 1536 * 1024**2, 'HOST_MEMORY_PRESSURE'
    assert int(RX.read_text()) - NETWORK_START < M['network_gib'][KIND] * 1024**3, 'RUNTIME_NETWORK_CAP'

def command(label, args, seconds, cap=4 * 1024**2, monitor=True):
    global LAST_SAMPLE
    guard()
    assert re.fullmatch('[a-z0-9-]+', label)
    target = R / (label + '.log')
    assert not target.exists()
    env = {k: v for k, v in os.environ.items()
           if not any(part in k.upper() for part in ('TOKEN', 'SECRET', 'PASSWORD', 'CREDENTIAL', 'API_KEY'))}
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               env=env, start_new_session=True)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    chunks = []
    size = 0
    started = time.monotonic()
    error = None
    try:
        while selector.get_map():
            guard()
            assert time.monotonic() - started < seconds, 'STEP_TIME_' + label
            if monitor and OWN and time.monotonic() - LAST_SAMPLE >= 5:
                LAST_SAMPLE = time.monotonic()
                state = json.loads(command('state-' + str(len(LOGS)),
                    ['docker', 'inspect', '--format', '{{json .State}}', OWN], 8, 8192, False))
                assert not state['OOMKilled'], 'CONTAINER_OOM'
                if state['Running']:
                    raw = None
                    try:
                        raw = command('memory-' + str(len(LOGS)),
                            ['docker', 'stats', '--no-stream', '--format', '{{.MemPerc}}', OWN],
                            8, 8192, False).decode().strip()
                    except AssertionError as problem:
                        if not str(problem).startswith('STEP_FAILED_memory-'):
                            raise
                    if raw is None or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?%', raw):
                        after = json.loads(command('state-after-stats-' + str(len(LOGS)),
                            ['docker', 'inspect', '--format', '{{json .State}}', OWN], 8, 8192, False))
                        assert not after['Running'], 'MISSING_WORKING_SET_SAMPLE_WHILE_RUNNING'
                        RESULT.setdefault('terminal_stats_races', []).append({'phase': PHASE, 'state': after})
                        continue
                    percent = float(raw[:-1])
                    limit = int(command('limit-' + str(len(LOGS)),
                        ['docker', 'inspect', '--format', '{{.HostConfig.Memory}}', OWN],
                        8, 8192, False))
                    sample = {'at': time.time(), 'phase': PHASE, 'percent': percent,
                              'limit': limit, 'sampled_working_set_upper_bytes':
                              math.ceil((percent + .01) / 100 * limit) + 1024**2}
                    SAMPLES.append(sample)
                    checkpoint = {'at': time.time(), 'samples': len(SAMPLES),
                                  'max_sampled_working_set_upper_bytes': max(x['sampled_working_set_upper_bytes'] for x in SAMPLES),
                                  'limit': limit}
                    temporary = R / 'memory-checkpoint-next.json'
                    temporary.write_text(json.dumps(checkpoint))
                    temporary.replace(R / 'memory-checkpoint.json')
                    assert percent < 93.75, 'CONTAINER_WORKING_SET_PRESSURE'
            for key, _ in selector.select(.25):
                data = os.read(key.fd, 8192)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                assert size + len(data) <= cap, 'OUTPUT_CAP_' + label
                chunks.append(data)
                size += len(data)
        assert process.wait(timeout=3) == 0, 'STEP_FAILED_' + label
        return b''.join(chunks)
    except BaseException as exc:
        error = str(exc)
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=3)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        raise
    finally:
        selector.close()
        target.write_bytes(b''.join(chunks))
        LOGS.append({'label': label, 'args': args, 'exit': process.returncode,
                     'elapsed': time.monotonic() - started, 'bytes': size, 'error': error})

def verify_source():
    assert command('source-clean', ['git', '-C', str(SOURCE), 'status', '--porcelain'], 10) == b''
    assert command('source-commit', ['git', '-C', str(SOURCE), 'rev-parse', 'HEAD'], 10).decode().strip() == M['base']
    auth = subprocess.run(['git', '-C', str(SOURCE), 'config', '--local', '--get-regexp',
                          'extraheader|credential'], capture_output=True, timeout=5)
    assert auth.returncode == 1 and not auth.stdout


def fetch_uv():
    item = M['uv']
    req = urllib.request.Request(item['url'], headers={'User-Agent': 'causalpy-validation'})
    with urllib.request.urlopen(req, timeout=min(25, WORK_END-time.time()-10)) as stream:
        raw = stream.read(item['size'] + 1)
    assert len(raw) == item['size']
    assert hashlib.sha256(raw).hexdigest() == item['digests']['sha256']
    (P / 'uv.whl').write_bytes(raw)


def execute():
    global OWN
    assert platform.machine() == 'x86_64' and KIND in ('prepare', 'unit', 'docs')
    R.mkdir()
    assert 0 <= time.time() - START_EPOCH < M['target_seconds']
    if KIND != 'prepare':
        verify_source()
    info = json.loads(command('docker-info', ['docker', 'info', '--format',
        '{"root":{{json .DockerRootDir}},"memory":{{.MemTotal}},"cpus":{{.NCPU}}}'], 15))
    available = int(re.search(r'^MemAvailable:\s+(\d+)', Path('/proc/meminfo').read_text(), re.M)[1])*1024
    facts = {'available': available, 'workspace_free': shutil.disk_usage(P).free,
             'docker_free': shutil.disk_usage(info['root']).free, 'docker': info}
    RESULT['admission'] = facts
    assert info['cpus'] >= M['cpus'] and available >= M['host_available_gib']*1024**3
    assert info['memory'] >= 15*1024**3
    assert facts['workspace_free'] >= M['free_disk_gib']*1024**3
    assert facts['docker_free'] >= M['free_disk_gib']*1024**3
    image = M['images'][PYTHON]['image']
    command('pull-base', ['docker', 'pull', '--platform', 'linux/amd64', image], 300)
    inspect = json.loads(command('inspect-base', ['docker', 'image', 'inspect', image], 10, 65536))[0]
    assert inspect['Architecture'] == 'amd64'
    assert any(s.endswith('@'+image.split('@')[1]) for s in inspect['RepoDigests'])
    assert inspect['Size'] < 4*1024**3
    fetch_uv()
    ident = os.environ['GITHUB_RUN_ID']
    assert re.fullmatch('[0-9]+', ident)
    tag = 'causalpy1365-i1:' + ident
    build = ['docker', 'build', '--file', str(P/'Dockerfile'), '--tag', tag,
             '--build-arg', 'PYTHON_BASE='+image,
             '--build-arg', 'UV_SHA256='+M['uv']['digests']['sha256'],
             '--build-arg', 'INSTALL_GRAPHVIZ='+('1' if KIND in ('prepare','docs') else '0')]
    if KIND == 'docs':
        c = json.loads((PREPARED/'closure.json').read_text())
        assert c['epoch'] == START_EPOCH and c['base'] == M['base']
        assert hashlib.sha256((PREPARED/'docs-lock.txt').read_bytes()).hexdigest() == c['sha256']
        version = c['graphviz_version']
        assert re.fullmatch(r'[A-Za-z0-9.+:~_-]+', version)
        build += ['--build-arg', 'GRAPHVIZ_VERSION='+version]
    build += [str(P)]
    command('build-image', build, 180)
    name = 'causalpy1365-'+KIND+'-'+MODE+'-'+ident
    args = ['docker', 'create', '--name', name, '--label', 'org.causalpy1365.run='+ident,
            '--init', '--cpus', str(M['cpus']), '--memory', str(M['container_memory_gib'])+'g',
            '--memory-swap', str(M['container_memory_gib'])+'g', '--shm-size', '1g',
            '--pids-limit', '2048', '--security-opt', 'no-new-privileges', '--cap-drop', 'ALL',
            '--cap-add', 'CHOWN', '--cap-add', 'DAC_OVERRIDE', '--cap-add', 'SETUID', '--cap-add', 'SETGID',
            '--mount', 'type=bind,src='+str(P)+',dst=/packet,readonly',
            '--mount', 'type=bind,src='+str(R)+',dst=/runtime']
    if KIND != 'prepare':
        args += ['--mount','type=bind,src='+str(SOURCE)+',dst=/source,readonly']
    if KIND == 'docs':
        args += ['--mount','type=bind,src='+str(PREPARED)+',dst=/prepared,readonly']
    values = {'VALIDATION_KIND': KIND, 'VALIDATION_MODE': MODE,
              'VALIDATION_PYTHON': PYTHON,
              'VALIDATION_PANDAS': os.environ.get('VALIDATION_PANDAS','pandas3'),
              'VALIDATION_NOTEBOOK_MODE': os.environ.get('VALIDATION_NOTEBOOK_MODE','native'),
              'VALIDATION_START_EPOCH': str(START_EPOCH),
              'VALIDATION_END_EPOCH': str(WORK_END if KIND=='prepare' else END)}
    for key,value in values.items():
        args += ['--env', key+'='+value]
    args.append(tag)
    OWN = command('create-container', args, 15).decode().strip()
    assert re.fullmatch('[a-f0-9]{64}', OWN)
    command('execute-container', ['docker','start','--attach', OWN], 3000, 32*1024**2)
    state = json.loads(command('container-state', ['docker','inspect','--format','{{json .State}}',OWN],10,8192,False))
    assert not state['Running'] and not state['OOMKilled'] and state['ExitCode']==0
    lane = json.loads((R/'result.json').read_text())
    assert lane['status']=='SUCCESS'
    RESULT.update(status='LANE_PASS',lane_pass=True,lane=lane)


def interrupted(signum,frame):
    raise TimeoutError('HOST_SIGNAL_'+str(signum))


signal.signal(signal.SIGTERM, interrupted)
signal.signal(signal.SIGINT, interrupted)
try:
    execute()
except BaseException as error:
    RESULT.update(status='STOP_'+type(error).__name__,error=str(error))
finally:
    if OWN:
        check = subprocess.run(['docker','inspect','--format','{{json .State}}',OWN],capture_output=True,timeout=10)
        if check.returncode == 0 and json.loads(check.stdout)['Running']:
            subprocess.run(['docker','stop','--time','2',OWN],capture_output=True,timeout=10)
        check = subprocess.run(['docker','inspect','--format','{{json .State}}',OWN],capture_output=True,timeout=10)
        RESULT['owned_stop_verified'] = check.returncode==0 and not json.loads(check.stdout)['Running']
        if check.returncode==0: RESULT['final_owned_state']=json.loads(check.stdout)
    else:
        RESULT['owned_stop_verified']=True
        RESULT['main_container_not_started']=True
    RESULT['host_steps']=LOGS
    RESULT['runtime_network_received_bytes']=int(RX.read_text())-NETWORK_START
    if R.exists():
        (R/'host-result.json').write_text(json.dumps(RESULT,indent=2))
        (R/'memory-samples.jsonl').write_text(''.join(json.dumps(s)+'\n' for s in SAMPLES))
        for step in [s for s in LOGS if s['exit'] not in (None,0)][:1]:
            print((R/(step['label']+'.log')).read_text(errors='replace')[-262144:],flush=True)
    print('CAUSALPY_HOST_RESULT='+json.dumps(RESULT),flush=True)
assert RESULT['lane_pass'] and RESULT['owned_stop_verified']
