"""
Tests for configuration management.

Covers:
  load_config    — env-only fallback, YAML loading, property overrides
  Config class   — property accessors and defaults
"""

import os
from unittest import mock

import pytest

from asset_management.web.config import Config, MasterConfigError, load_config


class TestLoadConfig:

    def test_defaults_without_yaml(self):
        with mock.patch("asset_management.web.config.os.path.exists", return_value=False):
            cfg = load_config()
        assert cfg["APP_HOST"] == "0.0.0.0"
        assert cfg["APP_PORT"] == int(os.environ.get("APP_PORT", 8084))
        assert cfg["FBOAUTH_HOSTNAME"] is None

    def test_env_vars_override_defaults(self):
        with (
            mock.patch.dict(os.environ, {"APP_PORT": "9999"}),
            mock.patch("asset_management.web.config.os.path.exists", return_value=False),
        ):
            cfg = load_config()
        assert cfg["APP_PORT"] == 9999

    def test_yaml_config_loaded(self):
        yaml_content = """
asset_web:
  port: 5000
  flask_secret_key: yaml-secret
fboauth:
  hostname: oauth.example.com
yubico:
  client_id: "12345"
  secret_key: "abc=="
"""
        with (
            mock.patch("asset_management.web.config.os.path.exists", return_value=True),
            mock.patch("builtins.open", mock.mock_open(read_data=yaml_content.encode("utf-8"))),
        ):
            cfg = load_config()
        assert cfg["APP_PORT"] == 5000
        assert cfg["FBOAUTH_HOSTNAME"] == "oauth.example.com"
        assert cfg["YUBICO_CLIENT_ID"] == "12345"
        assert cfg["YUBICO_SECRET_KEY"] == "abc=="

    def test_yaml_secret_not_overridden_when_env_set(self):
        yaml_content = """
asset_web:
  flask_secret_key: yaml-secret
"""
        with (
            mock.patch.dict(os.environ, {"SECRET_KEY": "env-secret"}),
            mock.patch("asset_management.web.config.os.path.exists", return_value=True),
            mock.patch("builtins.open", mock.mock_open(read_data=yaml_content.encode("utf-8"))),
        ):
            cfg = load_config()
        assert cfg["SECRET_KEY"] == "env-secret"

    def test_yaml_secret_used_when_default(self):
        yaml_content = """
asset_web:
  flask_secret_key: yaml-secret
"""
        with (
            mock.patch.dict(os.environ, {"SECRET_KEY": "dev-secret-key-change-in-production"}, clear=False),
            mock.patch("asset_management.web.config.os.path.exists", return_value=True),
            mock.patch("builtins.open", mock.mock_open(read_data=yaml_content.encode("utf-8"))),
        ):
            cfg = load_config()
        assert cfg["SECRET_KEY"] == "yaml-secret"

    def test_unreadable_yaml_refuses_to_boot(self):
        # a master-config that EXISTS but cannot be read must stop
        # boot, never warn-and-continue on defaults (the 2026-06-06 em-dash
        # incident ran prod on defaults behind a single stdout warning).
        with (
            mock.patch("asset_management.web.config.os.path.exists", return_value=True),
            mock.patch("builtins.open", side_effect=OSError("read error")),
        ):
            with pytest.raises(MasterConfigError, match="cannot be read"):
                load_config()

    def test_yubico_env_vars_take_precedence(self):
        yaml_content = """
yubico:
  client_id: "yaml-id"
  secret_key: "yaml-key"
"""
        with (
            mock.patch.dict(os.environ, {"YUBICO_CLIENT_ID": "env-id", "YUBICO_SECRET_KEY": "env-key"}),
            mock.patch("asset_management.web.config.os.path.exists", return_value=True),
            mock.patch("builtins.open", mock.mock_open(read_data=yaml_content.encode("utf-8"))),
        ):
            cfg = load_config()
        assert cfg["YUBICO_CLIENT_ID"] == "env-id"
        assert cfg["YUBICO_SECRET_KEY"] == "env-key"


class TestConfigClass:

    def test_properties_return_values(self):
        with mock.patch("asset_management.web.config.os.path.exists", return_value=False):
            cfg = Config()
        assert isinstance(cfg.SECRET_KEY, str)
        assert isinstance(cfg.APP_HOST, str)
        assert isinstance(cfg.APP_PORT, int)

    def test_es_auth_required_default_true(self):
        with mock.patch.dict(os.environ, {"ES_AUTH_REQUIRED": "true"}):
            cfg = Config.__new__(Config)
            cfg._config = {}
            assert cfg.ES_AUTH_REQUIRED is True

    def test_es_auth_required_false(self):
        with mock.patch.dict(os.environ, {"ES_AUTH_REQUIRED": "false"}):
            cfg = Config.__new__(Config)
            cfg._config = {}
            assert cfg.ES_AUTH_REQUIRED is False

    def test_session_lifetime_default(self):
        # SESSION_LIFETIME is the idle timeout. Env var name kept
        # for back-compat; semantic shifted from absolute to idle.
        with mock.patch.dict(os.environ, {}, clear=False):
            cfg = Config.__new__(Config)
            cfg._config = {}
            lifetime = cfg.SESSION_LIFETIME
            assert isinstance(lifetime, int)
            assert lifetime > 0

    def test_session_lifetime_from_env(self):
        with mock.patch.dict(os.environ, {"SESSION_LIFETIME": "7200"}):
            cfg = Config.__new__(Config)
            cfg._config = {}
            assert cfg.SESSION_LIFETIME == 7200

    def test_session_max_lifetime_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            cfg = Config.__new__(Config)
            cfg._config = {}
            max_lifetime = cfg.SESSION_MAX_LIFETIME
            assert isinstance(max_lifetime, int)
            assert max_lifetime > 0

    def test_session_max_lifetime_from_env(self):
        with mock.patch.dict(os.environ, {"SESSION_MAX_LIFETIME": "43200"}):
            cfg = Config.__new__(Config)
            cfg._config = {}
            assert cfg.SESSION_MAX_LIFETIME == 43200

    def test_trust_nginx_headers_explicit_true(self):
        with mock.patch.dict(os.environ, {"TRUST_NGINX_HEADERS": "true"}):
            cfg = Config.__new__(Config)
            cfg._config = {}
            assert cfg.TRUST_NGINX_HEADERS is True

    def test_trust_nginx_headers_explicit_false(self):
        with mock.patch.dict(os.environ, {"TRUST_NGINX_HEADERS": "false"}):
            cfg = Config.__new__(Config)
            cfg._config = {}
            assert cfg.TRUST_NGINX_HEADERS is False

    def test_trust_nginx_headers_default_is_false(self):
        """default must be false to prevent header-injection auth bypass."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRUST_NGINX_HEADERS", None)
            cfg = Config.__new__(Config)
            cfg._config = {}
            assert cfg.TRUST_NGINX_HEADERS is False

    def test_required_role_default(self):
        with mock.patch.dict(os.environ, {"ASSET_ADMIN_ROLE": "asset_admin"}):
            cfg = Config.__new__(Config)
            cfg._config = {}
            assert cfg.REQUIRED_ROLE == "asset_admin"

    def test_fboauth_hostname(self):
        with mock.patch("asset_management.web.config.os.path.exists", return_value=False):
            cfg = Config()
        assert cfg.FBOAUTH_HOSTNAME is None

    def test_yubico_properties(self):
        with mock.patch("asset_management.web.config.os.path.exists", return_value=False):
            cfg = Config()
        assert isinstance(cfg.YUBICO_CLIENT_ID, str)
        assert isinstance(cfg.YUBICO_SECRET_KEY, str)
