# Security Policy / 安全策略

## Supported versions / 支持范围

Security fixes are provided for the latest published stable release. A current
prerelease may receive fixes during its testing period; older releases should be
upgraded before a report is evaluated. / 安全修复覆盖最新稳定版；正在测试的预发布版
也可能获得修复。旧版本应先升级后再确认问题。

## Reporting a vulnerability / 报告漏洞

Do not open a public issue for a vulnerability that exposes user files, credentials,
LAN sessions, update delivery, or arbitrary code execution. Use GitHub's
[private vulnerability reporting](https://github.com/zhangsijie03/formatmaster-desktop/security/advisories/new)
and include:

- affected version and operating system;
- minimal reproduction steps;
- impact and required attacker access;
- sanitized logs or a proof of concept;
- whether the issue is already public.

涉及用户文件、凭据、局域网会话、更新链路或任意代码执行的问题，请勿创建公开 Issue。
请使用上述 GitHub 私密漏洞报告入口，并提供受影响版本、系统、最小复现、影响范围及已
脱敏的日志。不要上传真实私人文件、密码、令牌或未脱敏路径。

Maintainers should acknowledge a complete report within seven days and coordinate
disclosure after a fix is available. This is a target, not a service-level
guarantee. / 维护者目标是在 7 天内确认完整报告，并在修复可用后协调披露；该时间为
维护目标，不构成服务等级承诺。

## Release integrity / 发布完整性

Official binaries are published only in this repository's GitHub Releases. Verify
the matching entry in `SHA256SUMS.txt`. Stable releases must be code-signed on
Windows and signed and notarized on macOS. / 官方二进制仅通过本仓库 GitHub Releases
发布，请核对 `SHA256SUMS.txt`；稳定版必须完成 Windows 代码签名，以及 macOS 签名
与公证。
