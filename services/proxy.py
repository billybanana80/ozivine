import os
import re


def current_proxy_url():
    return os.environ.get("OZIVINE_PROXY_URL", "").strip()


def mask_proxy(proxy_url):
    if not proxy_url:
        return ""
    return re.sub(r"//[^:@/]+:[^@/]+@", "//***:***@", proxy_url)


def append_downloader_proxy(command):
    proxy_url = current_proxy_url()
    if not proxy_url or "--custom-proxy" in command:
        return command
    return f'{command} --custom-proxy "{proxy_url}"'


def mask_proxy_command(command):
    proxy_url = current_proxy_url()
    if not proxy_url:
        return command
    return command.replace(proxy_url, mask_proxy(proxy_url))
