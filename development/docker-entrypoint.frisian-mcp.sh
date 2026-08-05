#!/bin/bash
# Install frisian-mcp from the bind-mounted /opt/frisian-mcp BEFORE Nautobot
# loads its settings. This MUST run as the container entrypoint (not the
# command): the base image's /docker-entrypoint.sh runs `nautobot-server
# post_upgrade` and `nautobot-server check` first, both of which import
# INSTALLED_APPS -> frisian_mcp. If frisian-mcp were installed later (in the
# command) those steps would crash with ModuleNotFoundError: frisian_mcp.
#
# --no-deps is REQUIRED. frisian-mcp pins `django>=5`, but Nautobot 3.0 runs on
# Django 4.2. A normal install would upgrade Django to 6.x and break Nautobot
# (nautobot 3.0.0 requires Django<4.3). Nautobot already provides frisian-mcp's
# runtime deps (django, djangorestframework, jsonschema); frisian-mcp imports
# and runs on Django 4.2, so --no-deps is both necessary and sufficient.
#
# tiktoken (the [usage] extra -> real cl100k_base token counts) is installed
# separately and is non-fatal: without it the _usage counter transparently
# degrades to the `approx-char4` character approximation.
set -e

pip install -q --no-build-isolation --no-deps -e /opt/frisian-mcp
pip install -q --no-build-isolation 'tiktoken>=0.7,<1' \
    || echo "⚠  tiktoken not installed; _usage token counter will use the approx-char4 fallback."

exec "$@"
