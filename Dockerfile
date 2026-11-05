ARG PYTHON_BASE
FROM ${PYTHON_BASE}
ARG INSTALL_GRAPHVIZ=0
ARG GRAPHVIZ_VERSION=
RUN if [ "$INSTALL_GRAPHVIZ" = 1 ]; then apt-get -o Acquire::Retries=0 update && if [ -n "$GRAPHVIZ_VERSION" ]; then apt-get -o Acquire::Retries=0 install -y --no-install-recommends "graphviz=$GRAPHVIZ_VERSION"; else apt-get -o Acquire::Retries=0 install -y --no-install-recommends graphviz; fi; fi
COPY uv.whl /opt/uv.whl
ARG UV_SHA256
RUN UV_SHA256="$UV_SHA256" python -c 'import hashlib,os,pathlib,zipfile; p=pathlib.Path("/opt/uv.whl"); assert hashlib.sha256(p.read_bytes()).hexdigest()==os.environ["UV_SHA256"]; z=zipfile.ZipFile(p); n=next(n for n in z.namelist() if n.endswith("/uv")); assert z.getinfo(n).file_size<128*1024**2; b=pathlib.Path("/usr/local/bin/uv"); b.write_bytes(z.read(n)); b.chmod(0o755)' && uv --version && g++ --version
ENV UV_PYTHON_DOWNLOADS=never UV_HTTP_RETRIES=0 UV_HTTP_TIMEOUT=20 UV_LINK_MODE=copy UV_DEFAULT_INDEX=https://pypi.org/simple MPLBACKEND=Agg PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
ENTRYPOINT ["python", "/packet/inside.py"]
