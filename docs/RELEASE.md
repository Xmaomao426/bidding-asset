# Windows 本地发布层

当前版本：以根目录 [VERSION](../VERSION) 为唯一来源。

## 安装与启动

1. 在 Windows 安装 Python 3.12，并确保 `py -3.12` 可用。
2. 运行 `setup-local.cmd`。脚本只在 `.venv` 不存在时创建环境；已有环境不会删除或重建。
3. 运行 `start-local.cmd --check`，按聚合结果补齐必需依赖、Windows User scope 模型名和 API 密钥。
4. Preflight 通过后运行 `start-local.cmd`。服务默认固定绑定 `127.0.0.1:5000`。

可用参数为 `--check`、`--version`、`--port <端口>` 和 `--workspace <目录>`；默认端口为 5000，`--port 5010` 是有效覆盖示例；没有 `--host`。同一 workspace 同时只允许一个实例。显式 workspace 不改变当前目录，所有运行数据路径必须位于该 workspace 内。

请将开发包解压到当前用户可写目录后运行。默认 workspace 是应用根目录；也可通过 `--workspace` 选择一个已存在的显式 workspace。Preflight 对 workspace 保持零写入，只有未来由用户主动发起的既有业务操作才会写入所选 workspace。

## Preflight 与安全边界

- `--check` 只读，不创建目录、锁、日志或数据。
- 检查 Windows、Python 3.12、根虚拟环境、依赖、VERSION、workspace、现有关键 JSON、Chrome、LibreOffice、OCR、端口、上传上限和模型配置；缺失时分别说明无头 URL 捕获/重试、旧 `.doc` 转换或扫描/图片 PDF OCR 不可用。
- 模型与 API 密钥从 Windows User scope 刷新到当前进程；输出不显示、记录或散列密钥。
- 服务只绑定 `127.0.0.1`，使用 Waitress、workspace mutex、请求来源限制和精确 endpoint/method allowlist。
- 默认上传上限为 256 MiB；可用 Windows User scope `BIDDING_ASSET_MAX_UPLOAD_MIB` 调整为 1..1024 MiB。

## 构建边界

`scripts/build_release.ps1` 仅复制 `release/manifest.txt` 白名单，在临时 staging 中构建并复核 ZIP，输出到 `dist/`。包内禁止正式数据、工作簿、归档资料、环境、Git、虚拟环境、DevFlow、测试、历史、日志、上传、备份和临时文件。

`setup-local.cmd` 安装脚本已完成静态审核，但开发包尚未完成干净 Windows 安装验收。当前干净 Windows 安装验收状态为 `NOT RUN`，同机隔离安装为 `PARTIAL PASS`；干净 Windows 验收必须在正式 `0.1.0` 前作为独立任务完成。

## Private Draft 内部预览

当前 private GitHub 仓库已创建 [Draft prerelease](https://github.com/Xmaomao426/bidding-asset/releases/tag/untagged-759094676713542487ea)，标签为 `v0.1.0-dev`。它仅用于内部预览，不是 public、stable、latest 或正式 `0.1.0`，不得发布为正式 Release。

指定下载仅限该 Draft Release 上传的两个附件：`bidding-asset-windows-0.1.0-dev.zip` 与 `bidding-asset-windows-0.1.0-dev.zip.sha256`。ZIP 为 299714 bytes，SHA-256 为 `0a0395edee4722b948f87b9e536fdca6f830ec63517e7f548269c0b52bef1398`。不要把 GitHub 自动生成的源码归档当作安装包。

远端采用从已验证发布包构造的 fresh sanitized history，不包含本地源仓库历史、DevFlow 或正式业务数据。Git transport 失败后使用了有界 REST fallback 完成当前内部预览；这不改变安装方式、ZIP 内容或校验值。仍不使用 GitHub Actions、自动发布、自动更新或部署。
