# AstrBot 与 NAS Telegram 联动

2026-10-15 配置结果：

- NAS Docker 负责 Telegram 收发、原有下载插件及文件保存。
- 本机 AstrBot 的 `nas_tg` 平台使用独立的 TG 配置，明确选择 17 个个人插件。
- QQ 仍使用原配置；群管理、群统计和群分析未加入 TG 插件列表。
- TG 的新闻、视频总结、运势、订阅列表、OCR、系统状态、选歌列表和歌词默认发送文字。
- 处理提示更新成最终结果；超过消息长度限制的文字分为多页。
- 原进度消息已失效时，新建结果消息，保留最终文字和保存路径。
- 后续歌词或图片处理标注保留此前已发送文件的 NAS 路径。
- 生图指令的后台任务保留原请求关联，结果更新原来的 TG 任务提示。
- 真实图片、GIF、视频和文件保留媒体输出，并保存到 NAS 的 `downloads/astrbot`。
- 私人 B 站订阅已复制到 TG；QQ 原订阅保留。AI 早报、综合日报也配置了私人推送。
- NAS 原有的哔哩哔哩总结、AI 新闻和日报移植插件已备份并删除。
- Telegram、Twitter 下载、网盘/动漫检索和壁纸 HTTP 请求使用 NAS 配置的代理。
- 本机多平台下载插件的媒体、缩略图、升图原图和分类模型下载使用 NAS 代理。
- 音乐查询和音频下载配置为使用 NAS 代理。
- TG 保留 AI 菜谱插件，排除会截断同名指令的群聊随机食物回复插件；QQ 配置保留。
- TG 注册 45 个菜单命令，中文指令提供英文别名；启动和重载时同步默认、私聊与白名单会话的中文/英文菜单。

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
| `/invert`、`/flip`、`/gif`（`/反色`、`/左右翻转`、`/转GIF`） | GIF 工具箱；回复图片或视频使用 |
| `/aqt` | 回复文字或图片生成名言梗图 |
| `/mc 标题 描述`、`/mc_challenge 标题 描述`（`/mc成就`、`/mc挑战`） | 支持回复图片作为 MC 成就图标 |
| `/music 歌名`、`/qqmusic 歌名 1`、`/lyrics 歌名`（`/点歌`、`/QQ点歌`、`/查歌词`） | 文字选歌、音频和文字歌词 |
| `/eat 食材或口味`（`/吃什么`） | 文字菜谱推荐 |
| `/reminder_add 名称 cron 正文`、`/task_add 名称 cron 指令`（`/添加提醒`、`/添加任务`） | 定时提醒和执行插件命令 |
| `/reminder_run 名称`、`/task_run 名称`（`/立即提醒`、`/立即执行`） | 手动触发已经创建的提醒或任务 |
| `/reminder_delete 名称`、`/task_delete 名称`（`/删除提醒`、`/删除任务`） | 删除指定提醒或任务 |
| `/image 描述`、`/upscale` | 已启用的生图、回复图片升图 |
| `/pan 关键词`、`/nyaa 关键词` | NAS 网盘、动漫资源检索 |
| `/wp 创意工坊链接`、`/save` | NAS 壁纸下载、贴纸保存 |
| `/bt` | 查看 NAS qBittorrent 任务、进度、速度和保存目录 |
| `/help` | 下载器命令帮助 |

中文 AstrBot 命令也可以直接发送。GIF 和名言等创作结果本身是图片，保留
图片或文件输出。Telegram GIF 输入兼容 MP4/WebM，需要本机 FFmpeg。
Telegram 菜单命令只支持小写英文字母、数字和下划线；菜单中的英文命令由
NAS 联动插件映射到原来的中文指令，命令参数和回复附件保留。

## 已验证的实际流程

TG 私聊实测通过：运势、26 个 B 站订阅的列表、AI 新闻、综合日报、普通
对话、系统状态、引用图片 OCR、图片转文件、今日小猪、B 站文字总结、
GIF 反色、引用文字生成名言图片、引用图片生成 MC 成就、文字歌词、音频、
文字菜谱、生图和图片工具转码。B 站定时更新也实际推送到了 TG。
文件传输测试确认 NAS 保存文件与返回路径一致。
提醒和任务的创建、立即执行、删除共 6 项实测通过，临时测试任务已清理。
升图测试使用文字截图，被分类器正确识别为文字/UI 并跳过 AI 放大，
已验证随后转码、发送文件和保留路径的流程；没有把这一项当作 AI 放大实测。

本机插件必须保持 AstrBot 实例运行。NAS 下载器在 AstrBot 离线时仍可处理
下载命令；图片生成模型、网站登录和第三方站点的可用性仍依赖原有配置。
没有逐项测试每个插件的所有命令，没有实际下载付费 Steam 内容，也没有
启动新的大型 BT 下载。本机在内存使用较高时出现过事件循环延迟；
NAS 等待本机接收请求的超时已从 15 秒调整到 60 秒。

## NAS 辅助服务

`/bt` 的 `auth failed` 已修复。fnOS 的下载引擎使用运行时动态密码；
辅助服务现在从与目标 Unix socket 匹配的 `trim-qbittorrent-nox` 进程中读取
当前凭证，只在内存中使用，不记录或上传密码。其他 qBittorrent 部署仍可使用
`QBT_USERNAME`、`QBT_PASSWORD` 或忽略的 `steamcmd_config.json` 中的静态配置。
登录必须同时满足成功状态、`Ok.` 响应和有效会话 cookie。

`steam-workshop-service.service` 已重启加载修改，接口和 TG `/bt` 实测均成功，
读取到原有的 17 个 BT 任务。没有重建下载引擎或删除既有任务、文件。

qBittorrent 已配置 NAS 的 `192.168.1.34:7890` SOCKS5 代理，包括主机名解析、
BT、Peer、RSS 和其他 HTTP 请求。设置回读一致，NAS 上通过此 SOCKS5 通道
建立 HTTPS 连接并获得 HTTP 200。没有用这一项冒充新 BT 大文件下载的实测。

SteamCMD 容器由 `steam_proxy` 配置或 `STEAM_PROXY` 环境变量注入大小写的
HTTP/HTTPS 代理变量，当前配置为 NAS HTTP 代理。没有实际下载付费 Steam
内容；SteamCMD 各种协议是否均使用这些变量不作已验证声明。

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
- [MC 成就](https://github.com/Astraea35/astrbot_plugin_mc_ament)
- [音乐](https://github.com/Astraea35/astrbot_plugin_music)
- [通用生图](https://github.com/Astraea35/astrbot_plugin_image_generation)

## 维护资料

`tools/private/` 保存本次部署、发布和测试记录，并被 Git 忽略；没有发布
Telegram 会话、私人订阅、API 凭证、运行配置或备份。
`integration-backups/` 保存被替换的插件代码及配置快照。
