"""Nautobot development configuration file."""

import os
import sys

from nautobot.core.settings import *  # noqa: F403  # pylint: disable=wildcard-import,unused-wildcard-import
from nautobot.core.settings_funcs import is_truthy

#
# Debug
#

DEBUG = is_truthy(os.getenv("NAUTOBOT_DEBUG", "false"))
_TESTING = len(sys.argv) > 1 and sys.argv[1] == "test"

if DEBUG and not _TESTING:
    DEBUG_TOOLBAR_CONFIG = {"SHOW_TOOLBAR_CALLBACK": lambda _request: True}

    if "debug_toolbar" not in INSTALLED_APPS:  # noqa: F405
        INSTALLED_APPS.append("debug_toolbar")  # noqa: F405
    if "debug_toolbar.middleware.DebugToolbarMiddleware" not in MIDDLEWARE:  # noqa: F405
        MIDDLEWARE.insert(0, "debug_toolbar.middleware.DebugToolbarMiddleware")  # noqa: F405

#
# Misc. settings
#

ALLOWED_HOSTS = os.getenv("NAUTOBOT_ALLOWED_HOSTS", "").split(" ")
SECRET_KEY = os.getenv("NAUTOBOT_SECRET_KEY", "")

#
# Database
#

nautobot_db_engine = os.getenv("NAUTOBOT_DB_ENGINE", "django.db.backends.postgresql")
default_db_settings = {
    "django.db.backends.postgresql": {
        "NAUTOBOT_DB_PORT": "5432",
    },
    "django.db.backends.mysql": {
        "NAUTOBOT_DB_PORT": "3306",
    },
}
DATABASES = {
    "default": {
        "NAME": os.getenv("NAUTOBOT_DB_NAME", "nautobot"),  # Database name
        "USER": os.getenv("NAUTOBOT_DB_USER", ""),  # Database username
        "PASSWORD": os.getenv("NAUTOBOT_DB_PASSWORD", ""),  # Database password
        "HOST": os.getenv("NAUTOBOT_DB_HOST", "localhost"),  # Database server
        "PORT": os.getenv(
            "NAUTOBOT_DB_PORT", default_db_settings[nautobot_db_engine]["NAUTOBOT_DB_PORT"]
        ),  # Database port, default to postgres
        "CONN_MAX_AGE": int(os.getenv("NAUTOBOT_DB_TIMEOUT", "300")),  # Database timeout
        "ENGINE": nautobot_db_engine,
    }
}

# Ensure proper Unicode handling for MySQL
if DATABASES["default"]["ENGINE"] == "django.db.backends.mysql":
    DATABASES["default"]["OPTIONS"] = {"charset": "utf8mb4"}

#
# Redis
#

# The django-redis cache is used to establish concurrent locks using Redis.
# Inherited from nautobot.core.settings
# CACHES = {....}

#
# Celery settings are not defined here because they can be overloaded with
# environment variables. By default they use `CACHES["default"]["LOCATION"]`.
#

#
# Logging
#

LOG_LEVEL = "DEBUG" if DEBUG else "INFO"

# Verbose logging during normal development operation, but quiet logging during unit test execution
if not _TESTING:
    LOGGING = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "normal": {
                "format": "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s : %(message)s",
                "datefmt": "%H:%M:%S",
            },
            "verbose": {
                "format": "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)-20s %(filename)-15s %(funcName)30s() : %(message)s",
                "datefmt": "%H:%M:%S",
            },
        },
        "handlers": {
            "normal_console": {
                "level": "INFO",
                "class": "logging.StreamHandler",
                "formatter": "normal",
            },
            "verbose_console": {
                "level": "DEBUG",
                "class": "logging.StreamHandler",
                "formatter": "verbose",
            },
        },
        "loggers": {
            "django": {"handlers": ["normal_console"], "level": "INFO"},
            "nautobot": {
                "handlers": ["verbose_console" if DEBUG else "normal_console"],
                "level": LOG_LEVEL,
            },
        },
    }

#
# Apps
#

# Enable installed Apps. Add the name of each App to the list.
PLUGINS = ["nautobot_dns_models"]

# Apps configuration settings. These settings are used by various Apps that the user may have installed.
# Each key in the dictionary is the name of an installed App and its value is a dictionary of settings.
# PLUGINS_CONFIG = {
#     'nautobot_dns_models': {
#         'foo': 'bar',
#         'buzz': 'bazz'
#     }
# }

# ===========================================================================
# Frisian-MCP — expose the DNS Models app over MCP  (dns-mcp branch)
# ---------------------------------------------------------------------------
# Two AUTHENTICATED routes (NO open / read-only door, per design):
#   POST /mcp/read-write   auth, ceiling `read_write`  (list/retrieve/create/update)
#   POST /mcp/admin        auth, ceiling `admin`       (full CRUD + bulk + delete)
# Both expose the full DNS surface via the `dns` dispatch group (13 resources).
# frisian_mcp auto-mounts these routes from FRISIAN_MCP_ROUTES; no urls.py edit.
# ===========================================================================

# frisian-mcp is a plain Django app (not a Nautobot plugin) + its two contribs.
# Guarded on import: the dev containers install frisian-mcp via the entrypoint
# wrapper (development/docker-entrypoint.frisian-mcp.sh), so this activates. The
# "final"/CI containers do NOT run that wrapper, so without the guard they would
# crash loading a missing app — instead they simply run without the MCP surface.
try:
    import frisian_mcp  # noqa: F401  (installed check only)

    EXTRA_INSTALLED_APPS = [
        "frisian_mcp",
        "frisian_mcp.contrib.oauth",
        "frisian_mcp.contrib.tokens",
    ]
except ImportError:
    EXTRA_INSTALLED_APPS = []

# --- Routes: two authenticated tiers, no open-world/guest door --------------
_DNS_ALLOW = ["dns"]  # only the DNS dispatch group is exposed
FRISIAN_MCP_ROUTES = {
    "elevated": {
        "path": "mcp/read-write",
        "highest_tier": "read_write",
        "allow_list": list(_DNS_ALLOW),
    },
    "admin": {
        "path": "mcp/admin",
        "highest_tier": "admin",
        "allow_list": list(_DNS_ALLOW),
    },
}

# No open door: every request to either route must authenticate.
FRISIAN_MCP_ALLOW_UNAUTHENTICATED = False

# --- Auth: static Bearer tokens + OAuth.  NO guest fallback (no open route). -
FRISIAN_MCP_AUTHENTICATION_CLASSES = [
    "frisian_mcp.contrib.tokens.authentication.FrisianMcpTokenAuthentication",
    "frisian_mcp.contrib.oauth.authentication.OAuthTokenAuthentication",
]

# Local dev: no reverse proxy in front, so the issuer is the runserver origin
# and there are zero trusted proxies appending to X-Forwarded-For.
FRISIAN_MCP_OAUTH_ISSUER = os.getenv("FRISIAN_MCP_OAUTH_ISSUER", "http://localhost:8080")
FRISIAN_MCP_TRUSTED_PROXY_COUNT = int(os.getenv("FRISIAN_MCP_TRUSTED_PROXY_COUNT", "0"))
FRISIAN_MCP_HMAC_KEY = os.getenv("FRISIAN_MCP_HMAC_KEY", "dev-dns-mcp-hmac-key-change-me")

# OAuth lifecycle — safe defaults.  For local testing a static FrisianMcpToken
# (create one in the Django admin / nbshell) is the simplest credential; OAuth
# is here for MCP connectors (Claude.ai / ChatGPT) if you want them.
FRISIAN_MCP_OAUTH_AUTO_APPROVE = False
FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER = False
FRISIAN_MCP_OAUTH_REGISTRATION_OPEN = False
FRISIAN_MCP_OAUTH_PKCE_DEFAULT_PERMISSION = "read"
FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY = True
FRISIAN_MCP_OAUTH_TOKEN_EXPIRY_SECONDS = 60 * 60 * 24 * 365  # 1 year (dev)

# tools/list reflects the caller's Django object permissions.
FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY = True

# --- Dispatch group: the full DNS surface -----------------------------------
# Basenames = model object_name lowercased (verified against
# nautobot_dns_models/models.py on the dns-mcp branch — 13 concrete models).
FRISIAN_MCP_DISPATCH_GROUPS = {
    "dns": [
        "dnsview", "dnsviewprefixassignment",
        "dnsregistrar", "dnsregistration", "dnszone",
        "nsrecord", "arecord", "aaaarecord",
        "cnamerecord", "mxrecord", "txtrecord",
        "ptrrecord", "srvrecord",
    ],
}
