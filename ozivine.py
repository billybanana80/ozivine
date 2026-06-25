import sys
import importlib
import argparse
import yaml
from rich.console import Console
from rich.padding import Padding
from rich.text import Text
from datetime import datetime
from proxy_config import configure_proxy

#   Ozivine: Downloader for Australian & New Zealand FTA services
#   Author: billybanana
#   Quality: up to 1080p
#   Geo: Australian or NZ IP address required service dependent
#   Key Features:
#   1. Extract Video ID: Parses the respective video URL to extract the series name, season, and episode number.
#   2. Extract PSSH: Retrieves and parses the MPD file to extract the PSSH data necessary for Widevine decryption.
#   3. Fetch Decryption Keys: Uses the PSSH and license URL to request and retrieve the Widevine decryption keys.
#   4. Print Download Information: Outputs the MPD URL, license URL, PSSH, and decryption keys required for downloading and decrypting the video content.
#   5. Note: this script functions for both encrypted and non-encrypted video files.
#   6. Proxy support for both Surfshark and NordVPN. You need to obtain the OpenVPN credentials from your VPN provider - these are not the same as your email/password account credentials.

console = Console()
__version__ = "3.0"  # Replace with the actual version

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
    
# Define color formatting
class bcolors:
    LIGHTBLUE = '\033[94m'
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    ENDC = '\033[0m'
    ORANGE = '\033[38;5;208m'

def load_config():
    with open('config.yaml', 'r') as file:
        return yaml.safe_load(file)

def parse_args():
    parser = argparse.ArgumentParser(description="Ozivine downloader")
    parser.add_argument("video_url", nargs="?", help="Video URL to process")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--info", "-i", action="store_true", help="Show available formats without downloading")
    mode_group.add_argument("--action", "-a", action="store_true", help="Let N_m3u8DL-RE prompt for stream choices")
    return parser.parse_args()

def parse_prompt_input(value, mode):
    parts = value.strip().split()
    if not parts:
        return "", mode

    detected_modes = []
    url_parts = []
    for part in parts:
        if part in {"--info", "-i"}:
            detected_modes.append("info")
        elif part in {"--action", "-a"}:
            detected_modes.append("interactive")
        else:
            url_parts.append(part)

    if len(set(detected_modes)) > 1:
        raise ValueError("Use only one of --info/--i or --action/--a.")

    if detected_modes:
        mode = detected_modes[-1]

    return " ".join(url_parts).strip(), mode

def main():
    print_ascii_art(version=__version__)  # Display the ASCII art and version info
    parsed_args = parse_args()
    mode = "auto"
    if parsed_args.info:
        mode = "info"
    elif parsed_args.action:
        mode = "interactive"

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
        print(f"{bcolors.LIGHTBLUE}Video URL: {bcolors.ENDC}{video_url}")
    else:
        # Prompt user for manual input if no command-line argument is given
        prompt_value = input(f"{bcolors.LIGHTBLUE}Enter the video URL: {bcolors.ENDC}").strip()
        video_url, mode = parse_prompt_input(prompt_value, mode)

    if video_url.startswith("https://www.9now.com.au"):
        service_key = "9now"
        service_module = "services.9now.9now"
        print(f"{bcolors.LIGHTBLUE}Ozivine..........initiating 9Now{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path, mode)
    elif video_url.startswith("https://7plus.com.au"):
        service_key = "7plus"
        service_module = "services.7plus.7plus"
        print(f"{bcolors.LIGHTBLUE}Ozivine..........initiating 7Plus{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path, cookies_path, mode)
    elif video_url.startswith("https://www.sbs.com.au"):
        service_key = "sbs"
        service_module = "services.sbs.sbs"
        print(f"{bcolors.LIGHTBLUE}Ozivine..........initiating SBS{bcolors.ENDC}")
        args = (video_url, downloads_path, credentials.get("sbs"), mode)
    elif video_url.startswith("https://iview.abc.net.au"):
        service_key = "abciview"
        service_module = "services.abciview.abc"
        print(f"{bcolors.LIGHTBLUE}Ozivine..........initiating ABC iView{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path, mode)
    elif video_url.startswith(("https://10play.com.au/", "https://10.com.au/")):
        service_key = "10play"
        service_module = "services.10play.10play"
        print(f"{bcolors.LIGHTBLUE}Ozivine..........initiating 10{bcolors.ENDC}")
        args = (video_url, downloads_path, credentials.get("10play"), mode) 
    elif video_url.startswith("https://www.tvnz.co.nz/"):
        service_key = "tvnz"
        service_module = "services.tvnz.tvnz"
        print(f"{bcolors.LIGHTBLUE}Ozivine..........initiating TVNZ{bcolors.ENDC}")

        if not tvnz_local_storage:
            print(f"{bcolors.RED}Missing config value: tvnz.local_storage{bcolors.ENDC}")
            sys.exit(1)

        args = (video_url, downloads_path, wvd_device_path, tvnz_local_storage, mode) 
    elif video_url.startswith("https://www.threenow.co.nz"):
        service_key = "threenow"
        service_module = "services.threenow.threenow"
        print(f"{bcolors.LIGHTBLUE}Ozivine..........initiating ThreeNow{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path, mode)                      
    else:
        print(f"{bcolors.RED}Unsupported URL. Please enter a valid video URL from 9Now, 7Plus, 10Play, SBS, ABC iView, ThreeNow or TVNZ.{bcolors.ENDC}")
        sys.exit(1)

    try:
        if mode != "auto" and service_key not in {"9now", "7plus", "sbs", "abciview", "10play", "tvnz", "threenow"}:
            print(f"{bcolors.YELLOW}{mode} mode is not implemented for this service yet; using default service behavior.{bcolors.ENDC}")
        configure_proxy(config, service_key)
        service = importlib.import_module(service_module)
        service.main(*args)
    except Exception as e:
        print(f"{bcolors.RED}Error importing or running the service module: {e}{bcolors.ENDC}")

if __name__ == "__main__":
    main()
