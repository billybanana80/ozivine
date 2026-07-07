import re


def safe_windows_filename(name, replacement="_"):
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', replacement, str(name or ""))
    safe = re.sub(rf"{re.escape(replacement)}+", replacement, safe)
    safe = safe.strip(" .")
    return safe or "subtitle"
