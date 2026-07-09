#!/usr/bin/env python3
"""
Shared .env loader for all PhonesInventory scripts.
Loads KEY=VALUE pairs from DEPLOY_DIR/.env into os.environ
(without overriding variables already set in the environment).
"""
import os

DEPLOY_DIR = os.environ.get(
    "DEPLOY_DIR",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


def load_env(path=None):
    """Load .env file. Existing environment variables take precedence."""
    path = path or os.path.join(DEPLOY_DIR, ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception as e:
        print(f"[env] Failed to load {path}: {e}")


def require_env(key):
    """Get a required env var; exit with a clear error if missing."""
    value = os.environ.get(key, "").strip()
    if not value:
        raise SystemExit(f"[env] FATAL: required environment variable {key} is not set "
                         f"(add it to {os.path.join(DEPLOY_DIR, '.env')})")
    return value


# Auto-load on import
load_env()
