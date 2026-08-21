import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from download_confirm import coerce_bool


def update_checks_enabled(config):
    if not isinstance(config, dict):
        return True
    return coerce_bool(config.get("update_checks", True))


def version_parts(version):
    parts = re.findall(r"\d+", str(version or ""))
    return tuple(int(part) for part in parts)


def is_newer_version(latest_version, current_version):
    latest_parts = version_parts(latest_version)
    current_parts = version_parts(current_version)
    if not latest_parts or not current_parts:
        return False

    max_len = max(len(latest_parts), len(current_parts))
    latest_parts += (0,) * (max_len - len(latest_parts))
    current_parts += (0,) * (max_len - len(current_parts))
    return latest_parts > current_parts


def get_latest_release(repo, timeout=2):
    request = Request(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Ozivine",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def get_update_notice(current_version, repo, timeout=2):
    try:
        latest_release = get_latest_release(repo, timeout=timeout)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return None

    latest_version = latest_release.get("tag_name") or latest_release.get("name")
    if not is_newer_version(latest_version, current_version):
        return None

    release_url = latest_release.get("html_url") or f"https://github.com/{repo}/releases/latest"
    return latest_version, release_url
