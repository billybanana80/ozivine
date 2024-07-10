<h3 align="center">Ozivine<br/>
<sup>A Download Utility for Free Australian & NZ Streaming Services</sup></h3>
<p align="center">
    <a href="https://python.org">
        <img src="https://img.shields.io/badge/python-3.9+-blue" alt="Python version">
    </a>
    <a href="https://docs.python.org/3/library/venv.html">
        <img src="https://img.shields.io/badge/python-venv-blue" alt="Python virtual environments">
</p>

## Features:

- [x] Movies & TV-series
- [x] Automatic PSSH, manifest, and key retreival 
- [x] Option to add cookies where required (currently only 7Plus is required)
- [x] Option to add login credentials where required (currently only 10Play and TVNZ is required)
- [x] [Supported sites] ABC iView, 7Plus, 9Now, 10Play, SBS on Demand and TVNZ.

## Requirements:

* [Python](https://www.python.org/)

* [N_m3u8DL-RE](https://github.com/nilaoda/N_m3u8DL-RE/releases/)

* [ffmpeg](https://ffmpeg.org/)

* [mkvmerge](https://mkvtoolnix.download/downloads.html)

* [mp4decrypt](https://www.bento4.com/downloads/)

* Valid Widevine CDM (this is not included, so don't ask)

> [!TIP]
> Windows users are recommended to use Powershell 7 in Windows Terminal for best experience

## Installation:

1. Install Python (check 'Add to PATH' if on Windows)
2. Clone main branch or download latest version from [Releases](https://github.com/billybanana/ozivine/releases)
3. Place required tools inside Ozivine folder OR add them to system PATH (recommended)
4. Create `/wvd/` folder and place .wvd file in folder (or specify the path to your existing *.wvd as below)
5. Install necessary packages: `pip install -r requirements.txt`
6. Config: 
      -specify your Downloads path in the config.yaml (optional)

      -specify your path to *.wvd file in the config.yaml (mandatory)

      Sample config.yaml file to confirm format

      downloads_path: "C:/Downloads/"
   
      wvd_device_path: "C:/Downloads/Ozivine/wvd/l3.wvd"
   
      cookies_path: "C:/Downloads/Ozivine/cookies/SEVEN.txt"

      credentials:
        10play: username:password
        tvnz: username:password


> [!TIP]
> Clone the main branch to always stay up to date:
>
> ```git clone https://github.com/billybanana/ozivine.git ozivine```

## Common issues:

> ModuleNotFoundError: No module named ...

You haven't installed the necessary packages. Run `pip install -r requirements.txt`

> "Required key and client ID not found"

Content is encrypted and a decryption module is needed. This is up to the user and not provided by this project.

> ConnectionError: 400/403/404

You're most likely being geo blocked by the service. Use a VPN or try the proxy option.

## Credentials:

If a service requires cookies, you can use a browser extension to download cookies as .txt file format:

Firefox: https://addons.mozilla.org/addon/export-cookies-txt

Chrome: https://chromewebstore.google.com/detail/open-cookiestxt/gdocmgbfkjnnpapoeobnolbbkoibbcif


Name it `{service_name}.txt` and place it in service folder eg: SEVEN.txt

Modify the path to your cookies in the config.yaml file

## Usage:

Note: Australian or NZ IP address is required service dependent. Use a VPN or proxy as required.

Navigate to the main url for the service you require

ABC iView
https://iview.abc.net.au


7Plus
https://7plus.com.au


9Now
https://www.9now.com.au


10Play
https://10play.com.au/


SBS On Demand
https://www.sbs.com.au/ondemand/


TVNZ
https://www.tvnz.co.nz/



Then navigate to the video url of the show/episode/movie required.

Examples:

https://iview.abc.net.au/video/LE2427H007S00


https://7plus.com.au/below-deck-down-under?episode-id=4NBCU2330-S2T18


https://www.9now.com.au/paramedics/season-5/episode-10


https://www.sbs.com.au/ondemand/watch/2260044867809


https://10play.com.au/masterchef/episodes/season-16/episode-45/tpv240705dyovw


https://www.tvnz.co.nz/shows/the-responder/episodes/s1-e6



Note: it is not necessary to play any of these videos in the browser to obtain the page url.

ABC iView, 9Now and SBS On Demand can be navigated without an account or login required.

7Plus requires cookies to function, so a free account with the service is required. Register an account and login before exporting any cookies file.

10Play and TVNZ requires a login to function, so a free account with the service is required. Register an account and add your credentials to the config.yaml.

```python
Commands:
  ozivine       Run the main Ozivine script

```
 A download command is printed at the end of the script. You can choose Y or N to downlaod or not.
 
 You may choose to copy the N_m3u8DL-RE command and modify as you wish.

> [!TIP]
> See "N_m3u8DL-RE --morehelp select-video/audio/subtitle" for possible selection patterns

## Disclaimer

1. This project is purely for educational purposes and does not condone piracy
2. RSA key pair required for key derivation is not included in this project

