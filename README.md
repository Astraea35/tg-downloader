# TG Downloader and AstrBot

The NAS remains the Telegram gateway and download host. The `astrbot_bridge`
plugin forwards personal commands and conversation to an AstrBot instance through
the `astrbot_plugin_tg_assistant_core` platform adapter. QQ and Telegram use
separate AstrBot profiles and plugin lists.

Configure matching bridge secrets and the two LAN URLs. The NAS receives replies
at `/api/astrbot/push` on its existing web port; AstrBot receives messages on port
8769. The bridge does not use the Windows proxy for LAN communication.
Download plugins continue to use the NAS configuration and save on the NAS.
Telegram, Twitter extraction/downloads, FlareSolverr browser requests, search
requests and Steam previews use the configured NAS proxy. A configured proxy
failure does not switch these HTTP downloads to direct access. LAN bridge and
NAS helper-service calls bypass system proxies. SteamCMD and qBittorrent transfers
remain in their existing NAS services and follow those services' network settings.

Telegram replies edit the same progress message. Long results retain all text in
additional pages. Images, GIFs, videos and files are delivered as media and saved
under the NAS `downloads/astrbot` directory.

Supported English aliases include `/bili`, `/bili_list`, `/news`, `/fortune`,
`/pig`, `/image`, `/upscale`, `/ocr`, `/convert`, `/latest`, `/subscribe` and
`/unsubscribe`. AstrBot's Chinese commands can also be sent directly.

The bridge authenticates both directions and accepts only whitelisted private
chats. Download commands, Twitter links, Steam links, torrent tasks and Telegram
download links are kept on the NAS. AstrBot downtime does not disable downloads.

Keep live configuration, API keys, Telegram sessions, plugin data and backups
out of Git. `config.example.yaml` contains only deployment examples.

The NAS Steam/qBittorrent helper reads `qbt.username` and `qbt.password` from its
ignored `steamcmd_config.json`, or `QBT_USERNAME` and `QBT_PASSWORD` from its
environment. A rejected login does not count as a valid session. Restart that
independent helper after applying source updates.
