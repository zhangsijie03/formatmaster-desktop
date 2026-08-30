# FormatMaster Privacy Notice / 隐私说明

Effective date / 生效日期: 2026-08-30

FormatMaster has no account system, advertising SDK, analytics SDK, or automatic
crash-report upload. Media and documents are processed on the user's device by
default. / FormatMaster 不包含账号系统、广告 SDK、统计 SDK 或自动崩溃上传；媒体与
文档默认在用户设备本地处理。

## Data stored locally / 本地存储的数据

The app stores preferences, conversion history, task state, presets, and rotating
diagnostic logs in the operating system's application-data directory. History and
logs may contain local file paths, output paths, source URLs, error messages, and
stack traces. PDF passwords are retained only in memory for the current session
and are not written to preferences. / 程序会在系统应用数据目录保存偏好、转换历史、
任务状态、预设和轮转诊断日志。历史与日志可能包含本地文件路径、输出路径、源网址、
错误信息和调用栈。PDF 密码仅在当前会话内存中保留，不写入偏好文件。

- Windows: `%APPDATA%\FormatMaster` (with a user-directory fallback)
- macOS: `~/Library/Application Support/FormatMaster`
- Linux source builds: `$XDG_DATA_HOME/FormatMaster` or `~/.local/share/FormatMaster`

Users can clear conversion history and logs in Settings. Removing the application
data directory after closing FormatMaster deletes the remaining local settings. /
用户可在设置中清除历史和日志；退出程序后删除上述应用数据目录可移除其余本地配置。

## Network access / 网络访问

FormatMaster accesses the network only for features that require it:

- checking GitHub Releases and downloading user-approved app or tool updates;
- downloading media or M3U8 resources from URLs entered by the user;
- contacting configured GitHub download mirrors when direct access is unavailable;
- serving files and chat messages to devices on the same LAN after the user starts
  the LAN transfer service;
- loading a URL explicitly supplied to an online-content feature.

程序仅在相应功能需要时联网，包括检查 GitHub 更新、下载用户确认的程序或工具更新、
访问用户输入的媒体/M3U8 地址、使用配置的 GitHub 下载镜像，以及用户主动启动后的
局域网传输。相关第三方服务会按其自身政策接收 IP 地址、请求时间和目标 URL 等常规
网络信息。

LAN transfer is disabled until the user starts it. Anyone who has the displayed
address and session credentials may be able to access the shared session; use it
only on a trusted network and stop the service afterwards. / 局域网传输在用户启动前保持
关闭。请仅在可信网络使用，并在传输结束后停止服务。

## Crash reports and support / 崩溃与反馈

Crash details are saved locally and shown to the user for review. FormatMaster does
not upload them automatically. Before posting a log publicly, remove file paths,
URLs, filenames, or other personal information. / 崩溃详情只保存在本地并展示给用户，
不会自动上传。公开提交日志前，请先移除路径、网址、文件名等个人信息。

## Changes and contact / 变更与联系

Material changes are recorded in this file and the release notes. Privacy questions
may be submitted through the repository's GitHub Issues without attaching private
files or logs. / 重大变更会记录在本文件和发布说明中。隐私问题可通过仓库 Issue 提交，
请勿附带私人文件或未经脱敏的日志。
