def coerce_bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def config_auto_confirm(config):
    download_config = (config or {}).get("download") or {}
    return coerce_bool(download_config.get("auto_confirm"))


def confirm_download(prompt="Do you wish to download? Y or N: ", auto_confirm=False, auto_download=False):
    if auto_download:
        return True

    if auto_confirm:
        print("Auto-confirm enabled; proceeding.")
        return True

    try:
        return input(prompt).strip().lower() == "y"
    except EOFError:
        return False
