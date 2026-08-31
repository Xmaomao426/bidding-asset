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

`setup-local.cmd` 安装脚本已完成静态审核，但开发包尚未完成干净 Windows 安装验收。该验收必须在 `0.1.0` 前作为独立任务完成。

GitHub 仓库保持 private。只有在后续独立授权的干净安装与发布验收通过后，维护者才可手工创建 Draft Release，并附加版本化 ZIP 与 SHA 文件。不使用 GitHub Actions、自动发布或自动更新；当前任务不 push、不 tag，也不创建 Release。
