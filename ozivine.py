import sys
import importlib
import yaml
from rich.console import Console
from rich.padding import Padding
from rich.text import Text
from datetime import datetime

#   Ozivine: Downloader for Australian FTA services
#   Author: billybanana
#   Quality: up to 1080p
#   Key Features:
#   1. Extract Video ID: Parses the respective video URL to extract the series name, season, and episode number.
#   2. Extract PSSH: Retrieves and parses the MPD file to extract the PSSH data necessary for Widevine decryption.
#   3. Fetch Decryption Keys: Uses the PSSH and license URL to request and retrieve the Widevine decryption keys.
#   4. Print Download Information: Outputs the MPD URL, license URL, PSSH, and decryption keys required for downloading and decrypting the video content.
#   5. Note: this script functions for both encrypted and non-encrypted video files.

console = Console()
__version__ = "1.1"  # Replace with the actual version

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

def main():
    print_ascii_art(version=__version__)  # Display the ASCII art and version info

    config = load_config()
    downloads_path = config.get('downloads_path')
    wvd_device_path = config.get('wvd_device_path')
    cookies_path = config.get('cookies_path')
    credentials = config.get('credentials', {})

    video_url = input(f"{bcolors.LIGHTBLUE}Enter the video URL: {bcolors.ENDC}")

    if video_url.startswith("https://www.9now.com.au"):
        service_module = "services.9now.9now"
        print(f"{bcolors.LIGHTBLUE}Ozivine..........initiating 9Now{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path)
    elif video_url.startswith("https://7plus.com.au"):
        service_module = "services.7plus.7plus"
        print(f"{bcolors.LIGHTBLUE}Ozivine..........initiating 7Plus{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path, cookies_path)
    elif video_url.startswith("https://www.sbs.com.au"):
        service_module = "services.sbs.sbs"
        print(f"{bcolors.LIGHTBLUE}Ozivine..........initiating SBS{bcolors.ENDC}")
        args = (video_url, downloads_path)
    elif video_url.startswith("https://iview.abc.net.au"):
        service_module = "services.abciview.abc"
        print(f"{bcolors.LIGHTBLUE}Ozivine..........initiating ABC iView{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path)
    elif video_url.startswith("https://10play.com.au/"):
        service_module = "services.10play.10play"
        print(f"{bcolors.LIGHTBLUE}Ozivine..........initiating 10Play{bcolors.ENDC}")
        args = (video_url, downloads_path, credentials.get("10play"))       
    else:
        print(f"{bcolors.RED}Unsupported URL. Please enter a valid video URL from 9Now, 7Plus, SBS, or ABC iView.{bcolors.ENDC}")
        sys.exit(1)

    try:
        service = importlib.import_module(service_module)
        service.main(*args)
    except Exception as e:
        print(f"{bcolors.RED}Error importing or running the service module: {e}{bcolors.ENDC}")

if __name__ == "__main__":
    main()
