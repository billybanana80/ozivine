import sys
import importlib
import argparse
import yaml
from rich.console import Console
from rich.padding import Padding
from rich.text import Text
from datetime import datetime
from colors import bcolors
from proxy_config import configure_proxy
import icons

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
__version__ = "4.0"  # Replace with the actual version

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
    with open('config.yaml', 'r') as file:
        return yaml.safe_load(file)

def parse_args():
    parser = argparse.ArgumentParser(description="Ozivine downloader")
    parser.add_argument("video_url", nargs="?", help="Episode URL to download, or show URL with --list/-l or --download/-d")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--info", "-i", action="store_true", help="Show available formats without downloading")
    mode_group.add_argument("--action", "-a", action="store_true", help="Let N_m3u8DL-RE prompt for stream choices")
    mode_group.add_argument("--list", "-l", action="store_true", help="List available episodes for a show URL")
    mode_group.add_argument("--download", "-d", metavar="SELECTOR", help="Download from a show URL using sXXeXX, sXXXXeXX, sXX, or sXXXX")
    parser.add_argument("--export", "-x", action="store_true", help="Export list-mode episode URLs to a text file")
    return parser.parse_args()

def parse_prompt_input(value, mode, export_list=False, download_selector=None):
    parts = value.strip().split()
    if not parts:
        return "", mode, export_list, download_selector

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
        elif part in {"--download", "-d"}:
            detected_modes.append("download")
            if index + 1 >= len(parts):
                raise ValueError("Download mode requires a selector such as s01e01, s01, or s01e01-s02e02.")
            index += 1
            download_selector = parts[index]
        elif part in {"--export", "-x"}:
            export_list = True
        else:
            url_parts.append(part)
        index += 1

    if len(set(detected_modes)) > 1:
        raise ValueError("Use only one of --info/-i, --action/-a, --list/-l, or --download/-d.")

    if detected_modes:
        mode = detected_modes[-1]

    return " ".join(url_parts).strip(), mode, export_list, download_selector

def input_label_for_mode(mode):
    return "Series URL" if mode in {"list", "download"} else "Episode URL"

def main():
    print_ascii_art(version=__version__)  # Display the ASCII art and version info
    parsed_args = parse_args()
    mode = "auto"
    if parsed_args.info:
        mode = "info"
    elif parsed_args.action:
        mode = "interactive"
    elif parsed_args.list:
        mode = "list"
    elif parsed_args.download:
        mode = "download"
    export_list = parsed_args.export
    download_selector = parsed_args.download

    config = load_config()
    downloads_path = config.get('downloads_path')
    wvd_device_path = config.get('wvd_device_path')
    cookies_path = config.get('cookies_path')
    credentials = config.get('credentials', {})
    tvnz_config = config.get("tvnz", {})
    tvnz_local_storage = tvnz_config.get("local_storage")

    # Check if a URL is provided as a command-line argument
    if parsed_args.video_url:
        video_url = parsed_args.video_url.strip()
    else:
        # Prompt user for manual input if no command-line argument is given
        prompt_value = input(f"{bcolors.LIGHTBLUE}Enter URL with optional flags: {bcolors.ENDC}").strip()
        video_url, mode, export_list, download_selector = parse_prompt_input(prompt_value, mode, export_list, download_selector)

    print(f"{bcolors.LIGHTBLUE}{input_label_for_mode(mode)}: {bcolors.ENDC}{video_url}")

    if video_url.startswith("https://www.9now.com.au"):
        service_key = "9now"
        service_module = "services.9now.9now"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Ozivine..........initiating 9Now{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path, mode, export_list, download_selector)
    elif video_url.startswith("https://7plus.com.au"):
        service_key = "7plus"
        service_module = "services.7plus.7plus"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Ozivine..........initiating 7Plus{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path, cookies_path, mode, export_list, download_selector)
    elif video_url.startswith("https://www.sbs.com.au"):
        service_key = "sbs"
        service_module = "services.sbs.sbs"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Ozivine..........initiating SBS{bcolors.ENDC}")
        args = (video_url, downloads_path, credentials.get("sbs"), mode, export_list, download_selector)
    elif video_url.startswith("https://iview.abc.net.au"):
        service_key = "abciview"
        service_module = "services.abciview.abc"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Ozivine..........initiating ABC iView{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path, mode, export_list, download_selector)
    elif video_url.startswith(("https://10play.com.au/", "https://10.com.au/")):
        service_key = "10play"
        service_module = "services.10play.10play"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Ozivine..........initiating 10{bcolors.ENDC}")
        args = (video_url, downloads_path, credentials.get("10play"), mode, export_list, download_selector) 
    elif video_url.startswith("https://www.tvnz.co.nz/"):
        service_key = "tvnz"
        service_module = "services.tvnz.tvnz"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Ozivine..........initiating TVNZ{bcolors.ENDC}")

        if mode != "list" and not tvnz_local_storage:
            print(f"{bcolors.RED}{icons.ICON_FAILURE} Missing config value: tvnz.local_storage{bcolors.ENDC}")
            sys.exit(1)

        args = (video_url, downloads_path, wvd_device_path, tvnz_local_storage, mode, export_list, download_selector) 
    elif video_url.startswith("https://www.threenow.co.nz"):
        service_key = "threenow"
        service_module = "services.threenow.threenow"
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Ozivine..........initiating ThreeNow{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path, mode, export_list, download_selector)                      
    else:
        print(f"{bcolors.RED}{icons.ICON_FAILURE} Unsupported URL. Please enter a valid video URL from 9Now, 7Plus, 10, SBS, ABC iView, ThreeNow or TVNZ.{bcolors.ENDC}")
        sys.exit(1)

    try:
        if export_list and mode != "list":
            print(f"{bcolors.YELLOW}{icons.ICON_FAILURE} Export mode is only available with --list/-l.{bcolors.ENDC}")
            sys.exit(1)
        if mode == "download" and service_key not in {"abciview", "7plus", "9now", "10play", "sbs", "threenow", "tvnz"}:
            print(f"{bcolors.YELLOW}{icons.ICON_FAILURE} Download selector mode is currently implemented for ABC iView, 7Plus, 9Now, 10, SBS, ThreeNow, and TVNZ only.{bcolors.ENDC}")
            sys.exit(1)
        if mode == "download" and not download_selector:
            print(f"{bcolors.YELLOW}{icons.ICON_FAILURE} Download mode requires a selector such as s01e01, s2026e01, s01, s2026, s01e01-s02e02, or s01-s03.{bcolors.ENDC}")
            sys.exit(1)
        if mode == "list" and service_key not in {"sbs", "abciview", "7plus", "9now", "10play", "threenow", "tvnz"}:
            print(f"{bcolors.YELLOW}{icons.ICON_FAILURE} List mode is currently implemented for SBS, ABC iView, 7Plus, 9Now, 10, ThreeNow, and TVNZ only.{bcolors.ENDC}")
            sys.exit(1)
        if mode != "auto" and service_key not in {"9now", "7plus", "sbs", "abciview", "10play", "tvnz", "threenow"}:
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
