---
name: autonomous-loop
description: 管理 macOS Pi 研发 Loop 的项目接入、持续运行、任务恢复和 GitHub 交付。
---

# Pi Development Loop

阅读 [Loop 操作手册](../../loop/README.md)，从目标项目的 Git 主工作区运行 `python3 -B .agents/loop/loop.py`。

项目配置位于 `.loop.toml`，运行状态位于 `outputs/pi-loop/`。使用 `doctor` 检查环境，使用 `status`、`task show` 和 `history` 查询任务与证据。项目任务先经过隔离工作区检查和独立审查，再由 GitHub 同步交付。
