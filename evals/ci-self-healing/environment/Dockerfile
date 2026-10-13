ARG BASE_IMAGE=public.ecr.aws/docker/library/python@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d
FROM ${BASE_IMAGE}

COPY debian.sources /etc/apt/sources.list.d/debian.sources
RUN apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=30 update && apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=30 install -y --no-install-recommends \
    ca-certificates git ripgrep openjdk-21-jdk-headless maven \
    && rm -rf /var/lib/apt/lists/*
# Use exactly one explicitly selected wheel, with its original metadata/name.
# No Chrys release is pinned here; build a fresh image for each selected package.
COPY wheels/ /opt/wheels/
ARG EVAL_PYPI_INDEX=https://pypi.org/simple
RUN python3 -c "import pathlib; w=list(pathlib.Path('/opt/wheels').glob('*.whl')); assert len(w)==1 and w[0].name.startswith('chrys-'), 'provide exactly one Chrys wheel'" \
    && python3 -m pip install --index-url "${EVAL_PYPI_INDEX}" --no-cache-dir /opt/wheels/*.whl \
    && python3 -m pip check
# A host-built wheel may contain a macOS vendor binary. Replace only that
# image-local payload with Debian's verified, same-platform rg; fail on mismatch.
RUN python3 -c "import pathlib,shutil,chrys; p=pathlib.Path(chrys.__file__).parent/'foundation/vendor/ripgrep/rg'; assert not p.is_symlink(); shutil.copy2('/usr/bin/rg',p) if p.is_file() else None" \
    && python3 -c "import subprocess; from chrys.service.tools.builtins.search import _find_rg; subprocess.run([_find_rg(),'--version'],check=True)"

# Warm only a benign project's compile dependencies, never evaluation answers.
COPY cache-pom.xml /opt/cache-seed/pom.xml
COPY CacheSeed.java /opt/cache-seed/src/main/java/CacheSeed.java
RUN mvn -B -f /opt/cache-seed/pom.xml -Dmaven.repo.local=/opt/maven-repository compile \
    && rm -rf /opt/cache-seed
ARG EVAL_UID=501
RUN useradd --uid ${EVAL_UID} --create-home --home-dir /home/eval eval \
    && mkdir /home/eval/.chrys \
    && chown eval:eval /home/eval/.chrys
COPY gateway.py /opt/ci-eval/gateway.py
USER eval
WORKDIR /home/eval
