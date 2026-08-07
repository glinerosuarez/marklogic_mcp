import os
import re
import yaml
from ml_client import MarkLogicClient


def load_config(path: str) -> dict:
    try:
        with open(path) as f:
            raw = f.read()
    except FileNotFoundError:
        raise FileNotFoundError(
            f"config.yaml not found at '{path}'. "
            "Set ML_CONFIG_PATH to its location or run from the project root."
        )

    def _interpolate(m: re.Match) -> str:
        var = m.group(1)
        if var not in os.environ:
            raise KeyError(f"Environment variable '{var}' (referenced in config.yaml) is not set.")
        return os.environ[var]

    interpolated = re.sub(r"\$\{([^}]+)\}", _interpolate, raw)
    return yaml.safe_load(interpolated)


def build_client_pool(config: dict) -> dict[str, MarkLogicClient]:
    clients: dict[str, MarkLogicClient] = {}
    for name, cfg in config.get("servers", {}).items():
        clients[name] = MarkLogicClient(
            host=cfg.get("host", "localhost"),
            port=int(cfg.get("port", 8000)),
            user=cfg.get("user", ""),
            password=cfg.get("password", ""),
            use_https=cfg.get("use_https", False),
            ssl_ca_bundle=cfg.get("ssl_ca_bundle", True),
            default_permissions=cfg.get("default_permissions", "rest-reader:read"),
        )
    return clients


CONFIG = load_config(os.getenv("ML_CONFIG_PATH", "config.yaml"))
CLIENTS: dict[str, MarkLogicClient] = build_client_pool(CONFIG)
DEFAULT_SERVER: str = CONFIG.get("default_server", next(iter(CLIENTS)))
