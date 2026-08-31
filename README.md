# 本地招投标资产管理系统

本项目是面向本地资料的招投标资产管理 Web 应用。当前用户终态链路为：

```text
资料采集 → 人工核实 → 确认入库 → 资产库
```

资产库默认展示项目列表，支持检索，并可查看项目基础信息与关联资料详情。当前 UI 确认成功以 Repository / Relation 沉淀并可从资产库查看为终态，不调用或要求 Excel Writer。Excel 兼容 Writer、旧双状态字段与兼容代码、正式工作簿仅作为独立兼容边界保留。

## Windows 本地安装与启动

依次运行 `setup-local.cmd`、`start-local.cmd --check`、`start-local.cmd`。

`setup-local.cmd` 要求 Python 3.12，仅在 `.venv` 不存在时创建环境，绝不删除或重建已有环境。唯一服务启动入口是 `start-local.cmd`；它固定调用 `.\.venv\Scripts\python.exe -m src.operator_ui.local_runtime` 并只绑定 `127.0.0.1`。直接运行 `python -m src.operator_ui.app` 会拒绝启动。完整参数和安全边界见 [发布说明](docs/RELEASE.md)。

## 当前发布层边界

当前已实现 Windows localhost-only 启动、只读 Preflight、workspace 隔离、单实例保护、版本和最小离线打包脚本。脚本尚未完成干净 Windows 安装验收；当前没有 GitHub Release、tag、部署或自动更新。

## 目录

- `src/`：业务模块与操作界面。
- `tests/`：隔离测试。
- `config/`：项目配置。
- `docs/`：当前产品、架构、数据、决策、操作和路线文档。
- `docs/history/`：纳入 Git 的精选历史资料。
- `docs/archive/`：被忽略的原始历史包。
- `archived_files/`：被忽略的外部业务资料。

## 核心文档

1. [执行规则](AGENTS.md)
2. [当前任务](CURRENT_TASK.md)
3. [稳定状态](PROJECT_STATUS.md)
4. [产品模型](docs/PRODUCT_MODEL.md)
5. [架构](docs/ARCHITECTURE.md)
6. [数据模型](docs/DATA_MODEL.md)
7. [操作规则](docs/OPERATIONS.md)
8. [决策日志](docs/DECISION_LOG.md)
9. [路线](docs/ROADMAP.md)
10. [Windows 发布说明](docs/RELEASE.md)

默认依次读取 `AGENTS.md`、`CURRENT_TASK.md` 和 `PROJECT_STATUS.md`，再按任务需要选择其他专项事实源；历史资料不作为默认上下文。
