<h3 align="center">Ozivine<br/>
<sup>A Download Utility for Free Australian & NZ Streaming Services</sup></h3>

<p align="center">
    <a href="https://python.org">
        <img src="https://img.shields.io/badge/python-3.9+-blue" alt="Python version">
    </a>
    <a href="https://docs.python.org/3/library/venv.html">
        <img src="https://img.shields.io/badge/python-venv-blue" alt="Python virtual environments">
    </a>
</p>

## Features

- [x] Movies and TV series
- [x] Automatic PSSH, manifest, and key retrieval
- [x] Cookie support where required
- [x] Login credential support where required
- [x] Optional proxy support for Australian and New Zealand services
- [x] Info and action modes for previewing or manually selecting available streams
- [x] Supported sites: ABC iView, 7Plus, 9Now, 10Play, SBS On Demand, ThreeNow, and TVNZ

## Requirements

- [Python](https://www.python.org/)
- [N_m3u8DL-RE](https://github.com/nilaoda/N_m3u8DL-RE/releases/)
- [ffmpeg](https://ffmpeg.org/)
- [mkvmerge](https://mkvtoolnix.download/downloads.html)
- [mp4decrypt](https://www.bento4.com/downloads/)
- Valid Widevine CDM. This is not included.

> [!TIP]
> Windows users are recommended to use PowerShell 7 in Windows Terminal for the best experience.

## Installation

1. Install Python. On Windows, tick **Add to PATH** during installation.
2. Clone the main branch or download the latest version from [Releases](https://github.com/billybanana80/ozivine/releases).
3. Place required tools inside the Ozivine folder, or add them to your system PATH.
4. Create a `wvd` folder and place your `.wvd` file inside it, or set the path to your existing `.wvd` in `config.yaml`.
5. Install the Python packages:

```powershell
pip install -r requirements.txt
```

6. Edit `config.yaml`.

Sample config:

```yaml
downloads_path: "C:/Downloads/"
wvd_device_path: "C:/Downloads/Ozivine/wvd/l3.wvd"
cookies_path: "C:/Downloads/Ozivine/cookies/cookies.txt"

credentials:
  10play: username:password
  sbs: username:password

tvnz:
  local_storage: "D:/Downloads/CDM/local_storage.json"

proxy:
  enabled: false
  provider_order:
    - surfsharkvpn
    - nordvpn
  services:
    abciview: true
    7plus: true
    9now: true
    10play: true
    sbs: true
    threenow: true
    tvnz: true

proxy_providers:
  surfsharkvpn:
    username:
    password:
    server_map:
      AU: https://username:password@au-syd.prod.surfshark.com:443
      NZ: https://username:password@nz-akl.prod.surfshark.com:443
  nordvpn:
    username:
    password:
    server_map:
      AU:
      NZ:
```

> [!TIP]
> Clone the main branch to stay up to date:
>
> ```powershell
> git clone https://github.com/billybanana80/ozivine.git ozivine
> ```

## Proxy Support

Ozivine can route requests and downloads through a configured proxy provider. This is useful when a service requires an Australian or New Zealand IP address, or when your direct IP has been temporarily rate limited.

Proxy routing is selected automatically from the input video URL:

| Region | Services |
| --- | --- |
| AU | ABC iView, 7Plus, 9Now, 10Play, SBS On Demand |
| NZ | ThreeNow, TVNZ |

Set proxy support in `config.yaml`:

```yaml
proxy:
  enabled: true
  provider_order:
    - surfsharkvpn
    - nordvpn
  services:
    abciview: true
    7plus: true
    9now: true
    10play: true
    sbs: true
    threenow: true
    tvnz: true
```

Provider selection works like this:

1. If `proxy.enabled` is `false`, Ozivine uses a direct connection.
2. If `proxy.enabled` is `true`, Ozivine checks the service flag under `proxy.services`.
3. If the current service is set to `false`, Ozivine uses a direct connection for that service.
4. If the current service is set to `true`, Ozivine tries providers in `provider_order`.
5. Surfshark is used first if its username, password, and matching `AU` or `NZ` server URL are filled in.
6. NordVPN is used if Surfshark is incomplete and NordVPN has complete details for the required region.
7. If no complete provider is configured, Ozivine falls back to a direct connection.

Example: use proxy for New Zealand services only:

```yaml
proxy:
  enabled: true
  services:
    abciview: false
    7plus: false
    9now: false
    10play: false
    sbs: false
    threenow: true
    tvnz: true
```

Example Surfshark config:

```yaml
proxy_providers:
  surfsharkvpn:
    username: your_service_username
    password: your_service_password
    server_map:
      AU: https://username:password@au-syd.prod.surfshark.com:443
      NZ: https://username:password@nz-akl.prod.surfshark.com:443
```

The `username` and `password` placeholders in `server_map` are replaced automatically from the provider credentials above.

When a proxy is active, Ozivine also passes it to `N_m3u8DL-RE` with `--custom-proxy`. Printed download commands mask proxy credentials, but the real command uses the full proxy URL.

9Now uses lower downloader concurrency when a proxy is active because its HLS segment downloads can be sensitive to proxy/CDN `502 Bad Gateway` errors.

## Common Issues

### `ModuleNotFoundError: No module named ...`

The required Python packages have not been installed. Run:

```powershell
pip install -r requirements.txt
```

### `Required key and client ID not found`

The content is encrypted and a decryption module is needed. This is up to the user and is not provided by this project.

### `ConnectionError: 400/403/404`

You are most likely being geo-blocked by the service. Use a VPN or enable the proxy option.

## Credentials And Cookies

Some services require cookies or login credentials.

If a service requires cookies, use a browser extension to export cookies in `.txt` format:

- Firefox: [Export Cookies TXT](https://addons.mozilla.org/addon/export-cookies-txt)
- Chrome: [Get cookies.txt Clean](https://chromewebstore.google.com/detail/get-cookiestxt-clean/ahmnmhfbokciafffnknlekllgcnafnie)

Name the file for the service and set its path in `config.yaml`.

Example:

```yaml
cookies_path: "C:/Downloads/Ozivine/cookies/cookies.txt"
```

Service notes:

- ABC iView and 9Now can be used without an account.
- 7Plus requires cookies from a logged-in free account.
- 10Play and SBS require login/account data.
- TVNZ requires local storage token.
- ThreeNow requires a login to navigate the site.

## Usage

Australian or New Zealand IP access is required depending on the service. Use a VPN or proxy if needed.

Run Ozivine and paste a supported video URL:

```powershell
python ozivine.py
```

Or pass the URL directly:

```powershell
python ozivine.py "https://www.9now.com.au/paramedics/season-5/episode-10"
```

Supported service home pages:

| Service | URL |
| --- | --- |
| ABC iView | https://iview.abc.net.au |
| 7Plus | https://7plus.com.au |
| 9Now | https://www.9now.com.au |
| 10Play | https://10.com.au |
| SBS On Demand | https://www.sbs.com.au/ondemand |
| ThreeNow | https://www.threenow.co.nz |
| TVNZ | https://www.tvnz.co.nz |

Example video URLs:

```text
https://iview.abc.net.au/video/LE2427H007S00
https://7plus.com.au/below-deck-down-under?episode-id=4NBCU2330-S2T18
https://www.9now.com.au/paramedics/season-5/episode-10
https://www.sbs.com.au/ondemand/watch/2260044867809
https://10play.com.au/masterchef/episodes/season-16/episode-45/tpv240705dyovw
https://www.threenow.co.nz/shows/thirst-with-shay-mitchell/season-1-ep-1/1718148621037/M86965-766
https://www.tvnz.co.nz/player/tvepisode/tauranga-hilltop
```

It is not necessary to play the video in the browser to obtain the page URL.

At the end of the script, Ozivine prints the `N_m3u8DL-RE` command and asks whether you want to download.

```text
Do you wish to download? Y or N:
```

You can choose `Y` to download, choose `N` to skip, or copy the printed command and modify it yourself.

> [!TIP]
> See `N_m3u8DL-RE --morehelp select-video/audio/subtitle` for possible selection patterns.

### Download Modes

Ozivine has three download modes:

| Mode | Flags | Behaviour |
| --- | --- | --- |
| Auto | none | Builds the default best-quality command and asks whether to download. |
| Info | `--info` or `-i` | Shows available streams and the suggested filename without downloading. |
| Action | `--action` or `-a` | Builds a command without automatic stream selectors so `N_m3u8DL-RE` can prompt for manual choices. |

Examples:

```powershell
python ozivine.py "https://www.9now.com.au/paramedics/season-5/episode-10" -i
python ozivine.py "https://www.9now.com.au/paramedics/season-5/episode-10" -a
```

The same flags can be entered after the URL when using the interactive prompt:

```text
Enter the video URL: https://www.9now.com.au/paramedics/season-5/episode-10 -i
Enter the video URL: https://www.9now.com.au/paramedics/season-5/episode-10 -a
```

Info mode is useful for checking available resolutions, audio tracks, subtitles, keys, and the generated filename before starting a download.

Action mode is useful when you want to choose a lower resolution, alternate audio stream, or subtitle track manually. The generated filename is still based on Ozivine's default/best-quality expectation, so manually choosing a lower stream may require renaming the file afterward.

## TVNZ Local Storage

TVNZ no longer uses a simple username/password flow for Ozivine. It uses browser local storage values, which need to be extracted once and then cached for future use.

It is recommended to use a separate TVNZ account for this script. Do not share the same TVNZ browser session with Ozivine, as the sessions cannot be shared between the two.

To extract your local storage details:

1. Open TVNZ in your browser.
2. Press `F12` to open Developer Tools.
3. Open the Console tab.
4. Paste the following code and press Enter:

```javascript
Object.assign(document.createElement('a'), {
  href: URL.createObjectURL(new Blob([JSON.stringify({
    accessToken: localStorage.accessToken,
    refreshToken: localStorage.refreshToken,
    deviceref: localStorage.deviceref
  }, null, 2)])),
  download: 'local_storage.json'
}).click();
```

This saves a file named `local_storage.json` to your browser downloads folder. Set the path in `config.yaml`:

```yaml
tvnz:
  local_storage: "D:/Downloads/CDM/local_storage.json"
```

## Disclaimer

1. This project is purely for educational purposes and does not condone piracy.
2. RSA key pair required for key derivation is not included in this project.
