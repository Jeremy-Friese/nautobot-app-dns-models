# -------------------------------------------------------------------------------------
# Nautobot App Developement Dockerfile Template
# Version: 1.1.0
#
# Apps that need to add additional steps or packages can do in the section below.
# -------------------------------------------------------------------------------------
# !!! USE CAUTION WHEN MODIFYING LINES BELOW

# Accepts a desired Nautobot version as build argument, default to 3.0.0
ARG NAUTOBOT_VER="3.0.0"

# Accepts a desired Python version as build argument, default to 3.12
ARG PYTHON_VER="3.12"

# Retrieve published development image of Nautobot base which should include most CI dependencies
FROM ghcr.io/nautobot/nautobot-dev:${NAUTOBOT_VER}-py${PYTHON_VER}

# Runtime argument and environment setup
ARG NAUTOBOT_ROOT=/opt/nautobot

ENV prometheus_multiproc_dir=/prom_cache
ENV NAUTOBOT_ROOT=${NAUTOBOT_ROOT}
ENV INVOKE_NAUTOBOT_DNS_MODELS_LOCAL=true

# Install Poetry manually via its installer script;
# We might be using an older version of Nautobot that includes an older version of Poetry
# and CI and local development may have a newer version of Poetry
# Since this is only used for development and we don't ship this container, pinning Poetry back is not expressly necessary
# We also don't need virtual environments in container
RUN which poetry || curl -sSL https://install.python-poetry.org | python3 - && \
    poetry config virtualenvs.create false

# !!! USE CAUTION WHEN MODIFYING LINES ABOVE
# -------------------------------------------------------------------------------------
# App-specifc system build/test dependencies.
#
# Example: LDAP requires `libldap2-dev` to be apt-installed before the Python package.
# -------------------------------------------------------------------------------------
# --> Start safe to modify section

# Uncomment the lines below if you are apt-installing any package.
# RUN apt-get -y update && apt-get -y install \
#     libldap2-dev \
#     && rm -rf /var/lib/apt/lists/*

# --> Stop safe to modify section
# -------------------------------------------------------------------------------------
# Install Nautobot App
# -------------------------------------------------------------------------------------
# !!! USE CAUTION WHEN MODIFYING LINES BELOW

# Copy in the source code
WORKDIR /source
COPY . /source

# Build args must be declared in each stage
ARG NAUTOBOT_VER
ARG PYTHON_VER

# Constrain the Nautobot version to NAUTOBOT_VER, fall back to installing from git branch if not available on PyPi
# In CI, this should be done outside of the Dockerfile to prevent cross-compile build failures
ARG CI
RUN if [ -z "${CI+x}" ]; then \
    INSTALLED_NAUTOBOT_VER=$(pip show nautobot | grep "^Version" | sed "s/Version: //"); \
    poetry add --lock nautobot@${INSTALLED_NAUTOBOT_VER} --python ${PYTHON_VER} || \
    poetry add --lock git+https://github.com/nautobot/nautobot.git#${NAUTOBOT_VER} --python ${PYTHON_VER}; fi

# Install the app
RUN poetry install --extras all --with dev

# frisian-mcp (MCP gateway) — exposes the DNS app over MCP (see nautobot_config.py).
# --no-deps is REQUIRED: frisian-mcp pins `django>=5`, but Nautobot 3.0 runs on
# Django 4.2, so a normal install would upgrade Django and break Nautobot.
# Nautobot already provides frisian-mcp's runtime deps (django, DRF, jsonschema).
# tiktoken enables real cl100k_base token counts (the [usage] feature); without it
# the counter degrades to an approximation.
# 1.1.0 (the per-route FRISIAN_MCP_ROUTES model this config uses) is not on PyPI
# yet — PyPI's latest is 1.0.12 — so install from git `main`. Override the ref at
# build: docker build --build-arg FRISIAN_MCP_REF=<branch|tag|sha> ...
ARG FRISIAN_MCP_REF=main
# Persist the tiktoken BPE cache in the image so the real cl100k_base token
# counter loads offline at runtime (same as the Nautobot Core setup).
ENV TIKTOKEN_CACHE_DIR=/opt/tiktoken_cache
RUN pip install --no-deps "frisian-mcp @ git+https://github.com/Frisian-MCP/frisian-mcp.git@${FRISIAN_MCP_REF}" \
    && pip install "tiktoken>=0.7,<1" \
    && mkdir -p "${TIKTOKEN_CACHE_DIR}" \
    && python -c "import tiktoken; tiktoken.get_encoding('cl100k_base'); print('cl100k_base cached')"

COPY development/nautobot_config.py ${NAUTOBOT_ROOT}/nautobot_config.py
# !!! USE CAUTION WHEN MODIFYING LINES ABOVE
