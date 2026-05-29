import json
import os
import stat
from pathlib import Path
from typing import Any, Mapping


DEFAULT_HOME = Path.home() / ".factor-mining-agent"
DEFAULT_BASE_URL = "https://d25q1jf66e8y4g.cloudfront.net"
HOME_ENV = "FACTOR_MINING_AGENT_HOME"


def agent_home(home: str | Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    if home is not None:
        return Path(home).expanduser()
    env = env if env is not None else os.environ
    if env.get(HOME_ENV):
        return Path(env[HOME_ENV]).expanduser()
    return DEFAULT_HOME


def ensure_agent_home(home: str | Path | None = None) -> Path:
    root = agent_home(home)
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, stat.S_IRWXU)
    except OSError:
        pass
    return root


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    data = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
    finally:
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
