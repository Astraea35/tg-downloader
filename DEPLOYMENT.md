# AstrBot 与 NAS Telegram 联动

2026-10-14 配置结果：

- NAS Docker 负责 Telegram 收发、原有下载插件及文件保存。
- 本机 AstrBot 的 `nas_tg` 平台使用独立的 TG 配置，明确选择 18 个个人插件。
- QQ 仍使用原配置；群管理、群统计和群分析未加入 TG 插件列表。
- TG 的新闻、视频总结、运势、订阅列表、OCR 和系统状态默认发送文字。
- 处理提示更新成最终结果；超过消息长度限制的文字分为多页。
- 真实图片、GIF、视频和文件保留媒体输出，并保存到 NAS 的 `downloads/astrbot`。
- 私人 B 站订阅已复制到 TG；QQ 原订阅保留。AI 早报、综合日报也配置了私人推送。
- NAS 原有的哔哩哔哩总结、AI 新闻和日报移植插件已备份并删除。
- Telegram、Twitter 下载、网盘/动漫检索和壁纸 HTTP 请求使用 NAS 配置的代理。
- 本机多平台下载插件的媒体、缩略图、升图原图和分类模型下载使用 NAS 代理。

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `/bili 链接或BV号` | B 站视频文字总结 |
| `/bili_list` | 当前会话订阅列表 |
| `/latest UP主` | 最新视频及总结 |
| `/subscribe UP主`、`/unsubscribe UP主` | 修改 TG 会话订阅 |
| `/ainews`、`/news` | AI 早报、综合日报 |
| `/fortune`、`/pig` | 运势、今日小猪 |
| `/status` 或 `/状态` | 本机 AstrBot 所在 Windows 的状态 |
| `/ocr` | 发送图片附上命令，或回复图片识别文字 |
| `/convert` | 回复图片转换为文件 |
| `/反色`、`/左右翻转`、`/转GIF` | GIF 工具箱；回复图片或视频使用 |
| `/aqt` | 回复文字或图片生成名言梗图 |
| `/image 描述`、`/upscale` | 已启用的生图、回复图片升图 |
| `/pan 关键词`、`/nyaa 关键词` | NAS 网盘、动漫资源检索 |
| `/wp 创意工坊链接`、`/save` | NAS 壁纸下载、贴纸保存 |
| `/bt` | NAS BT 任务；当前辅助服务授权仍需修复 |
| `/help` | 下载器命令帮助 |

中文 AstrBot 命令也可以直接发送。GIF 和名言等创作结果本身是图片，保留
图片或文件输出。Telegram GIF 输入兼容 MP4/WebM，需要本机 FFmpeg。

## 已验证的实际流程

TG 私聊实测通过：运势、26 个 B 站订阅的列表、AI 新闻、综合日报、普通
对话、系统状态、引用图片 OCR、图片转文件、今日小猪、B 站文字总结、
GIF 反色和引用文字生成名言图片。B 站定时更新也实际推送到了 TG。
文件传输测试确认 NAS 保存文件与返回路径一致。

本机插件必须保持 AstrBot 实例运行。NAS 下载器在 AstrBot 离线时仍可处理
下载命令；图片生成模型、网站登录和第三方站点的可用性仍依赖原有配置。
没有逐项调用全部插件、没有实际下载付费 Steam 内容，也没有启动新的大型
BT 下载。

## 剩余问题

`/bt` 的独立 NAS 辅助服务当前返回 `auth failed`。这不是 AstrBot 联动报错。
辅助服务源码已移除硬编码凭证，改为读取 Git 忽略的 `steamcmd_config.json`
中 `qbt.username`、`qbt.password`，或 `QBT_USERNAME`、`QBT_PASSWORD` 环境变量。
仍需在 NAS 上核实有效的 qBittorrent 凭证并部署、重启该独立服务。

当前没有可用的 fnOS 管理会话。继续处理运行中的辅助服务需要 NAS 管理
登录。fnOS 技能使用 OAuth：用户自行在浏览器登录、确认授权，并在工具
终端粘贴一次性授权码；无需向聊天发送 NAS 密码。

SteamCMD 和 qBittorrent 是独立 NAS 服务，数据传输仍遵循它们自身的网络
设置；本次没有把这些协议的实际代理路径宣称为已验证。

## GitHub

所有修改发布到 `Astraea35` 的个人仓库，状态插件使用 `dev` 分支。

- [NAS 下载器](https://github.com/Astraea35/tg-downloader)
- [联动平台](https://github.com/Astraea35/astrbot_plugin_tg_assistant_core)
- [B 站总结](https://github.com/Astraea35/astrbot_plugin_bilivideo_custom)
- [AI 新闻](https://github.com/Astraea35/astrbot_plugin_daily_ai_news_mod)
- [综合日报](https://github.com/Astraea35/astrbot_plugin_zhenxunribao_mod)
- [系统状态](https://github.com/Astraea35/astrbot_plugin_status/tree/dev)
- [今日运势](https://github.com/Astraea35/astrbot_plugin_jrysprpr)
- [多平台解析与升图](https://github.com/Astraea35/astrbot_plugin_link_resolver_plus)
- [图片转文件](https://github.com/Astraea35/astrbot_plugin_sticker_convert)
- [OCR](https://github.com/Astraea35/astrbot_plugin_ocr)
- [名言梗图](https://github.com/Astraea35/astrbot_plugin_quote_image)
- [GIF 工具箱](https://github.com/Astraea35/astrbot_plugin_gif_master)

## 维护资料

`tools/private/` 保存本次部署、发布和测试记录，并被 Git 忽略；没有发布
Telegram 会话、私人订阅、API 凭证、运行配置或备份。
`integration-backups/` 保存被替换的插件代码及配置快照。
