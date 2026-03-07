#!/usr/bin/env python3
"""Lark Open API authentication: credentials + tenant access token.

Shared by handoff and lark-wiki skills.  Each skill provides its own
config_file path; the rest (token fetch, caching, file-lock) is identical.

Usage:
    from lark_auth import LarkAuth
    auth = LarkAuth("~/.lark-wiki/config.json")   # or ~/.handoff/config.json
    token = auth.get_token()
"""

import json
import os
import time
import urllib.error
import urllib.request

BASE_URL = "https://open.larksuite.com/open-apis"


class LarkAuth:
    """Manage Lark tenant access token with file-based caching and locking."""

    def __init__(self, config_file):
        self._config_file = os.path.expanduser(config_file)
        self._cache_dir = os.path.dirname(self._config_file)
        self._token_cache = {"token": None, "expires_at": 0}
        self._lock_path = os.path.join(self._cache_dir, "token.lock")
        self._cache_file = os.path.join(self._cache_dir, "token-cache.json")

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------

    def load_credentials(self):
        """Load app_id/app_secret from config file.

        Returns dict with at least app_id and app_secret, or None.
        """
        try:
            with open(self._config_file) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        if not data.get("app_id") or not data.get("app_secret"):
            return None
        return data

    # ------------------------------------------------------------------
    # Token
    # ------------------------------------------------------------------

    def get_token(self):
        """Get a valid tenant access token, refreshing if needed."""
        creds = self.load_credentials()
        if not creds:
            raise RuntimeError(
                f"No Lark credentials found in {self._config_file}"
            )
        return self._get_tenant_token(creds["app_id"], creds["app_secret"])

    def _get_tenant_token(self, app_id, app_secret):
        now = time.time()
        if self._token_cache["token"] and now < self._token_cache["expires_at"]:
            return self._token_cache["token"]

        lock_fd = self._acquire_lock()
        try:
            now = time.time()
            if self._token_cache["token"] and now < self._token_cache["expires_at"]:
                return self._token_cache["token"]

            shared = self._load_shared_cache(app_id)
            if shared and now < shared["expires_at"]:
                self._token_cache["token"] = shared["token"]
                self._token_cache["expires_at"] = shared["expires_at"]
                return self._token_cache["token"]

            payload = {"app_id": app_id, "app_secret": app_secret}
            req = urllib.request.Request(
                f"{BASE_URL}/auth/v3/tenant_access_token/internal",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read())

            if data.get("code") != 0:
                raise RuntimeError(f"Failed to get token: {data}")

            self._token_cache["token"] = data["tenant_access_token"]
            self._token_cache["expires_at"] = (
                time.time() + data.get("expire", 7200) - 60
            )
            self._store_shared_cache(
                app_id,
                self._token_cache["token"],
                self._token_cache["expires_at"],
            )
            return self._token_cache["token"]
        finally:
            self._release_lock(lock_fd)

    # ------------------------------------------------------------------
    # File lock (cross-process safe token refresh)
    # ------------------------------------------------------------------

    def _acquire_lock(self):
        os.makedirs(os.path.dirname(self._lock_path), exist_ok=True)
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        except Exception:
            os.close(fd)
            raise
        return fd

    @staticmethod
    def _release_lock(fd):
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            os.close(fd)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Shared token cache (disk)
    # ------------------------------------------------------------------

    def _load_shared_cache(self, app_id):
        try:
            with open(self._cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None
        if data.get("app_id") != app_id:
            return None
        token = data.get("token")
        expires_at = data.get("expires_at")
        if not token:
            return None
        try:
            expires_at = float(expires_at)
        except Exception:
            return None
        return {"token": token, "expires_at": expires_at}

    def _store_shared_cache(self, app_id, token, expires_at):
        os.makedirs(os.path.dirname(self._cache_file), exist_ok=True)
        tmp = f"{self._cache_file}.tmp.{os.getpid()}"
        data = {"app_id": app_id, "token": token, "expires_at": expires_at}
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, self._cache_file)
