import sys
import os
import importlib
import argparse
import subprocess
import shutil
import yaml
from rich.console import Console
from rich.padding import Padding
from rich.text import Text
from datetime import datetime
from pathlib import Path
from colors import bcolors
from batch_import import load_batch_entries, source_counts
from download_confirm import config_auto_confirm, confirm_download
from proxy_config import configure_proxy
from quality_utils import normalize_quality
from update_check import get_update_notice, update_checks_enabled
import icons

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

#   Ozivine: Downloader for Australian & New Zealand FTA services
#   Author: billybanana
#   Quality: up to 1080p, service dependent
#   Geo: Australian or NZ IP address required, service dependent
#
#   Supports:
#   - Single episode/video downloads
#   - Episode info and download command preview modes
#   - Series listing, export, and selector-based downloads
#   - Encrypted and non-encrypted streams
#   - Surfshark and NordVPN proxy profiles
#
#   Full usage details and examples are in README.md.

console = Console()
__version__ = "4.3"  # Replace with the actual version
GITHUB_REPO = "billybanana80/ozivine"
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.yaml"
TEMP_DIR = SCRIPT_DIR / "temp"
EXPORT_DIR = SCRIPT_DIR / "export"
BATCH_CHILD_ENV = "OZIVINE_BATCH_CHILD"

def print_ascii_art(version=None):
    ascii_art = Text(
        r"          _       _            " + "\n"
        r"  ___ ___(_)_   _(_)_ __   ___ " + "\n"
        r" / _ \_  / \ \ / / | '_ \ / _ \ " + "\n"
        r"| (_) / /| |\ V /| | | | |  __/ " + "\n"
        r" \___/___|_| \_/ |_|_| |_|\___| " + "\n"
        r"                               ",
        
    )

    version_info = Text(f"Version {__version__} Copyright © {datetime.now().year} billybanana", style="none")
    github_link = Text("https://github.com/billybanana80/ozivine", style="bright_blue")

    combined_text = ascii_art + Text("\n") + version_info + Text("\n") + github_link
    padded_art = Padding(combined_text, (1, 21, 1, 20), expand=True)

    console.print(padded_art, justify="left")

    if version:
        return
    
def load_config():
    with open(CONFIG_PATH, 'r', encoding="utf-8") as file:
        return yaml.safe_load(file) or {}

def line_indent(line):
    return len(line) - len(line.lstrip(" "))

def remove_nested_yaml_key(lines, parent_key, child_key):
    removed = False
    parent_index = None
    parent_indent = 0
    for index, line in enumerate(lines):
        if line.strip() == f"{parent_key}:":
            parent_index = index
            parent_indent = line_indent(line)
            break
    if parent_index is None:
        return lines, removed

    index = parent_index + 1
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        indent = line_indent(line)
        if stripped and indent <= parent_indent:
            break
        if stripped.startswith(f"{child_key}:") and indent == parent_indent + 2:
            end = index + 1
            while end < len(lines):
                end_line = lines[end]
                end_stripped = end_line.strip()
                end_indent = line_indent(end_line)
                if end_stripped and end_indent <= indent:
                    break
                end += 1
            del lines[index:end]
            removed = True
            continue
        index += 1
    return lines, removed

def normalize_empty_yaml_section(lines, parent_key):
    for index, line in enumerate(lines):
        if line.strip() != f"{parent_key}:":
            continue

        parent_indent = line_indent(line)
        next_index = index + 1
        has_nested_lines = False

        while next_index < len(lines):
            candidate = lines[next_index]
            stripped = candidate.strip()
            if not stripped:
                next_index += 1
                continue
            if line_indent(candidate) <= parent_indent:
                break
            has_nested_lines = True
            break

        if not has_nested_lines:
            newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            lines[index] = f"{' ' * parent_indent}{parent_key}: {{}}{newline}"

    return lines

def clear_config_token_cache():
    if not CONFIG_PATH.exists():
        return []

    lines = CONFIG_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    removed_items = []

    for parent_key, child_keys in {
        "sbs": ["cache"],
        "10play": ["cache"],
        "7plus": ["cache"],
        "tvnz": ["cache"],
    }.items():
        for child_key in child_keys:
            lines, removed = remove_nested_yaml_key(lines, parent_key, child_key)
            if removed:
                removed_items.append(f"{parent_key}.{child_key}")

    if removed_items:
        for parent_key in ("sbs", "10play", "7plus", "tvnz"):
            lines = normalize_empty_yaml_section(lines, parent_key)
        CONFIG_PATH.write_text("".join(lines), encoding="utf-8")

    return removed_items

def clear_temp_folder():
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    resolved_temp = TEMP_DIR.resolve()
    resolved_root = SCRIPT_DIR.resolve()
    if resolved_temp.parent != resolved_root or resolved_temp.name.lower() != "temp":
        raise RuntimeError(f"Refusing to clear unexpected temp folder: {TEMP_DIR}")

    removed_count = 0
    for item in TEMP_DIR.iterdir():
        if item.is_dir() and not item.is_symlink():
            shutil.rmtree(item)
        else:
            item.unlink()
        removed_count += 1
    return removed_count

def clear_project_cache():
    removed_config_items = clear_config_token_cache()
    removed_temp_items = clear_temp_folder()

    if removed_config_items:
        print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Cleared config cache entries: {', '.join(removed_config_items)}{bcolors.ENDC}")
    else:
        print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}No cached token entries found in config.yaml{bcolors.ENDC}")

    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Cleared {removed_temp_items} item(s) from {TEMP_DIR}{bcolors.ENDC}")

def parse_args():
    parser = argparse.ArgumentParser(description="Ozivine downloader")
    parser.add_argument("video_url", nargs="?", help="Episode URL to download, show URL with --list/-l or --download/-d, or omit with --batch/-b")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--info", "-i", action="store_true", help="Show available formats without downloading")
    mode_group.add_argument("--action", "-a", action="store_true", help="Let N_m3u8DL-RE prompt for stream choices")
    mode_group.add_argument("--list", "-l", action="store_true", help="List available episodes for a show URL")
    mode_group.add_argument("--download", "-d", metavar="SELECTOR", help="Download from a show URL using sXXeXX, sXXXXeXX, sXX, or sXXXX")
    mode_group.add_argument("--batch", "-b", action="store_true", help="Download episode URLs imported from text files in export/")
    parser.add_argument("--export", "-x", action="store_true", help="Export list-mode episode URLs to a text file")
    parser.add_argument("--quality", "-q", type=normalize_quality, help="Select video height for downloads, e.g. 720 or 1080")
    parser.add_argument("--yes", "-y", action="store_true", help="Automatically answer yes to download prompts")
    parser.add_argument("--subs", "-s", action="store_true", help="Keep service subtitles where implemented")
    parser.add_argument("--clear-cache", "-c", action="store_true", help="Clear cached service tokens from config.yaml and remove files from temp/")
    return parser.parse_args()

def normalize_prompt_flag_spacing(parts):
    normalized = []
    short_flags = {"i", "a", "l", "d", "b", "x", "q", "y", "s", "c"}
    long_flags = {"info", "action", "list", "download", "batch", "export", "quality", "yes", "subs", "clear-cache"}
    index = 0
    while index < len(parts):
        part = parts[index]
        if part in {"-", "--"} and index + 1 < len(parts):
            next_part = parts[index + 1]
            if part == "-" and next_part in short_flags:
                normalized.append(f"-{next_part}")
                index += 2
                continue
            if part == "--" and next_part in long_flags:
                normalized.append(f"--{next_part}")
                index += 2
                continue
        normalized.append(part)
        index += 1
    return normalized

def parse_prompt_input(value, mode, export_list=False, download_selector=None, quality=None, auto_confirm=False, save_subs=False):
    parts = normalize_prompt_flag_spacing(value.strip().split())
    if not parts:
        return "", mode, export_list, download_selector, quality, auto_confirm, save_subs

    detected_modes = []
    url_parts = []
    index = 0
    while index < len(parts):
        part = parts[index]
        if part in {"--info", "-i"}:
            detected_modes.append("info")
        elif part in {"--action", "-a"}:
            detected_modes.append("interactive")
        elif part in {"--list", "-l"}:
            detected_modes.append("list")
        elif part in {"--batch", "-b"}:
            detected_modes.append("batch")
        elif part.startswith("--download="):
            detected_modes.append("download")
            download_selector = part.split("=", 1)[1]
        elif part.startswith("-d") and len(part) > 2:
            detected_modes.append("download")
            download_selector = part[2:]
        elif part in {"--download", "-d"}:
            detected_modes.append("download")
            if index + 1 >= len(parts):
                raise ValueError("Download mode requires a selector such as s01e01, s01, or s01e01-s02e02.")
            index += 1
            download_selector = parts[index]
        elif part.startswith("--quality="):
            quality = normalize_quality(part.split("=", 1)[1])
        elif part.startswith("-q") and len(part) > 2:
            quality = normalize_quality(part[2:])
        elif part in {"--quality", "-q"}:
            if index + 1 >= len(parts):
                raise ValueError("Quality requires a height such as 720 or 1080.")
            index += 1
            quality = normalize_quality(parts[index])
        elif part in {"--export", "-x"}:
            export_list = True
        elif part in {"--yes", "-y"}:
            auto_confirm = True
        elif part in {"--subs", "-s"}:
            save_subs = True
        else:
            url_parts.append(part)
        index += 1

    if len(set(detected_modes)) > 1:
        raise ValueError("Use only one of --info/-i, --action/-a, --list/-l, --download/-d, or --batch/-b.")

    if detected_modes:
        mode = detected_modes[-1]

    return " ".join(url_parts).strip(), mode, export_list, download_selector, quality, auto_confirm, save_subs

def input_label_for_mode(mode):
    if mode == "batch":
        return "Batch"
    return "Series URL" if mode in {"list", "download"} else "Episode URL"

def run_batch_mode(quality=None, auto_confirm=False, save_subs=False):
    entries = load_batch_entries(EXPORT_DIR)
    if not entries:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}No episode URLs found in {EXPORT_DIR}{bcolors.ENDC}")
        return

    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Imported {len(entries)} episode URLs from {EXPORT_DIR}{bcolors.ENDC}")
    for source_path, count in source_counts(entries).items():
        print(f"{bcolors.GRAY}- {source_path.name}: {count}{bcolors.ENDC}")

    if quality:
        print(
            f"{icons.ICON_WARNING} {bcolors.WARNING}Batch quality mode uses an exact height selector. "
            f"If a service or episode does not offer {quality}p, N_m3u8DL-RE may select no video. "
            f"Use -i first, choose a common height, or omit -q for best.{bcolors.ENDC}"
        )

    episode_word = "episode" if len(entries) == 1 else "episodes"
    this_or_these = "this" if len(entries) == 1 else "these"
    if not confirm_download(f"Do you wish to download {this_or_these} {len(entries)} {episode_word}? Y or N: ", auto_confirm=auto_confirm):
        print(f"{icons.ICON_FAILURE} {bcolors.RED}Download cancelled{bcolors.ENDC}")
        return

    failed_count = 0
    child_env = os.environ.copy()
    child_env[BATCH_CHILD_ENV] = "1"

    for index, entry in enumerate(entries, start=1):
        print(f"\n{icons.ICON_INFO} {bcolors.LIGHTBLUE}Batch {index}/{len(entries)}:{bcolors.ENDC} {entry.url}")
        if entry.label:
            print(f"{bcolors.GRAY}{entry.label}{bcolors.ENDC}")

        command = [sys.executable, str(SCRIPT_DIR / "ozivine.py"), entry.url, "-y"]
        if save_subs:
            command.append("-s")
        if quality:
            command.extend(["-q", str(quality)])

        result = subprocess.run(command, cwd=str(SCRIPT_DIR), env=child_env)
        if result.returncode != 0:
            failed_count += 1

    if failed_count:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Batch finished with {failed_count} failed child run(s).{bcolors.ENDC}")
    else:
        print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Batch complete: {len(entries)} episode URL(s) processed.{bcolors.ENDC}")

def main():
    batch_child = os.environ.get(BATCH_CHILD_ENV) == "1"
    if not batch_child:
        print_ascii_art(version=__version__)  # Display the ASCII art and version info
    parsed_args = parse_args()
    if parsed_args.clear_cache:
        clear_project_cache()
        return

    mode = "auto"
    if parsed_args.info:
        mode = "info"
    elif parsed_args.action:
        mode = "interactive"
    elif parsed_args.list:
        mode = "list"
    elif parsed_args.download:
        mode = "download"
    elif parsed_args.batch:
        mode = "batch"
    export_list = parsed_args.export
    download_selector = parsed_args.download
    quality = parsed_args.quality
    save_subs = parsed_args.subs

    config = load_config()
    if update_checks_enabled(config) and not batch_child:
        update_notice = get_update_notice(__version__, GITHUB_REPO)
        if update_notice:
            latest_version, release_url = update_notice
            print(f"{bcolors.YELLOW}{icons.ICON_INFO} Update available: {latest_version} is available.{bcolors.ENDC}")
            print(f"{release_url}")

    auto_confirm = config_auto_confirm(config) or parsed_args.yes
    downloads_path = config.get('downloads_path')
    wvd_device_path = config.get('wvd_device_path')
    cookies_path = config.get('cookies_path')
    credentials = config.get('credentials', {})
    tvnz_credential = credentials.get("tvnz")

    if mode == "batch":
        run_batch_mode(quality=quality, auto_confirm=auto_confirm, save_subs=save_subs)
        return

    # Check if a URL is provided as a command-line argument
    if parsed_args.video_url:
        video_url = parsed_args.video_url.strip()
    else:
        # Prompt user for manual input if no command-line argument is given
        prompt_value = input(f"{bcolors.LIGHTBLUE}Enter URL with optional flags: {bcolors.ENDC}").strip()
        video_url, mode, export_list, download_selector, quality, auto_confirm, save_subs = parse_prompt_input(prompt_value, mode, export_list, download_selector, quality, auto_confirm, save_subs)
        if mode == "batch":
            run_batch_mode(quality=quality, auto_confirm=auto_confirm, save_subs=save_subs)
            return

    print(f"{bcolors.LIGHTBLUE}{input_label_for_mode(mode)}: {bcolors.ENDC}{video_url}")

    if video_url.startswith("https://www.9now.com.au"):
        service_key = "9now"
        service_module = "services.9now.9now"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Ozivine..........initiating 9Now{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path, mode, export_list, download_selector, False, quality, auto_confirm, save_subs)
    elif video_url.startswith("https://7plus.com.au"):
        service_key = "7plus"
        service_module = "services.7plus.7plus"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Ozivine..........initiating 7Plus{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path, cookies_path, mode, export_list, download_selector, False, quality, auto_confirm, save_subs)
    elif video_url.startswith("https://www.sbs.com.au"):
        service_key = "sbs"
        service_module = "services.sbs.sbs"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Ozivine..........initiating SBS{bcolors.ENDC}")
        args = (video_url, downloads_path, credentials.get("sbs"), mode, export_list, download_selector, False, quality, auto_confirm, save_subs)
    elif video_url.startswith("https://iview.abc.net.au"):
        service_key = "abciview"
        service_module = "services.abciview.abc"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Ozivine..........initiating ABC iView{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path, mode, export_list, download_selector, False, quality, auto_confirm, save_subs)
    elif video_url.startswith(("https://10play.com.au/", "https://10.com.au/")):
        service_key = "10play"
        service_module = "services.10play.10play"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Ozivine..........initiating 10{bcolors.ENDC}")
        args = (video_url, downloads_path, credentials.get("10play"), mode, export_list, download_selector, False, quality, auto_confirm, save_subs) 
    elif video_url.startswith("https://watch.brollie.com.au/"):
        service_key = "brollie"
        service_module = "services.brollie.brollie"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Ozivine..........initiating Brollie{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path, mode, export_list, download_selector, False, quality, auto_confirm, save_subs)
    elif video_url.startswith("https://www.tvnz.co.nz/"):
        service_key = "tvnz"
        service_module = "services.tvnz.tvnz"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Ozivine..........initiating TVNZ{bcolors.ENDC}")

        if mode != "list" and not tvnz_credential:
            print(f"{bcolors.RED}{icons.ICON_FAILURE} Missing config value: credentials.tvnz{bcolors.ENDC}")
            sys.exit(1)

        args = (video_url, downloads_path, wvd_device_path, tvnz_credential, str(CONFIG_PATH), mode, export_list, download_selector, False, quality, auto_confirm, save_subs) 
    elif video_url.startswith("https://www.maoriplus.co.nz"):
        service_key = "mplus"
        service_module = "services.mplus.mplus"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Ozivine..........initiating Maori+{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path, mode, export_list, download_selector, False, quality, auto_confirm, save_subs)
    elif video_url.startswith("https://www.threenow.co.nz"):
        service_key = "threenow"
        service_module = "services.threenow.threenow"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Ozivine..........initiating ThreeNow{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path, mode, export_list, download_selector, False, quality, auto_confirm, save_subs)                      
    else:
        print(f"{bcolors.RED}{icons.ICON_FAILURE} Unsupported URL. Please enter a valid video URL from 9Now, 7Plus, 10, SBS, ABC iView, Brollie, Maori+, ThreeNow or TVNZ.{bcolors.ENDC}")
        sys.exit(1)

    try:
        if export_list and mode != "list":
            print(f"{bcolors.YELLOW}{icons.ICON_FAILURE} Export mode is only available with --list/-l.{bcolors.ENDC}")
            sys.exit(1)
        if mode == "download" and service_key not in {"abciview", "7plus", "9now", "10play", "brollie", "sbs", "mplus", "threenow", "tvnz"}:
            print(f"{bcolors.YELLOW}{icons.ICON_FAILURE} Download selector mode is currently implemented for ABC iView, 7Plus, 9Now, 10, Brollie, SBS, Maori+, ThreeNow, and TVNZ only.{bcolors.ENDC}")
            sys.exit(1)
        if mode == "download" and not download_selector:
            print(f"{bcolors.YELLOW}{icons.ICON_FAILURE} Download mode requires a selector such as s01e01, s2026e01, s01, s2026, s01e01-s02e02, or s01-s03.{bcolors.ENDC}")
            sys.exit(1)
        if mode == "list" and service_key not in {"sbs", "abciview", "7plus", "9now", "10play", "brollie", "mplus", "threenow", "tvnz"}:
            print(f"{bcolors.YELLOW}{icons.ICON_FAILURE} List mode is currently implemented for SBS, ABC iView, 7Plus, 9Now, 10, Brollie, Maori+, ThreeNow, and TVNZ only.{bcolors.ENDC}")
            sys.exit(1)
        if mode != "auto" and service_key not in {"9now", "7plus", "sbs", "abciview", "10play", "brollie", "mplus", "tvnz", "threenow"}:
            print(f"{bcolors.YELLOW}{icons.ICON_FAILURE} {mode} mode is not implemented for this service yet; using default service behavior.{bcolors.ENDC}")
        configure_proxy(config, service_key)
        service = importlib.import_module(service_module)
        service.main(*args)
    except ValueError as e:
        print(f"{bcolors.RED}{icons.ICON_FAILURE} {e}{bcolors.ENDC}")
    except Exception as e:
        print(f"{bcolors.RED}{icons.ICON_FAILURE} Error importing or running the service module: {e}{bcolors.ENDC}")

if __name__ == "__main__":
    main()
