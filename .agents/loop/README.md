# Pi Development Loop

Pi Loop 在 macOS 上持续处理 GitHub 工程任务。每次执行使用独立的 Pi 会话，任务状态、检查点、验证结果和会话事件保存在项目的 SQLite 数据库中。

运行路径是巡检或接单、冻结验收与文件范围、实现、项目检查、独立审查、PR、CI、合并和关闭 Issue。GitHub 同步与本地任务分别调度，远端不可用时本地队列继续运行。

## 准备

需要 Python 3.11 或更新版本、Git、GitHub CLI、Pi 1.6.1 和已配置的 Pi 模型。项目的 `origin` 获取和推送地址需要指向同一个 GitHub 仓库。Pi 和 GitHub CLI 分别使用各自已配置的身份。

将 Loop、项目配置和 CI 工作流提交到目标分支的 Git 基线。修改项目根目录的 `.loop.toml`，设置仓库、目标分支、检查命令及授权修改范围。`project.auto_paths` 允许巡检任务自动修改的路径，`project.allowed_paths` 是全部任务的路径范围。项目检查以参数数组运行，并在 macOS 沙箱内禁用网络。

自动巡检仅运行配置了 `scan_failure` 的检查。标记应当是该检查失败时稳定出现的文本。Loop 会对同一基线运行两次检查，确认退出失败和标记后才派发巡检。下面是单元测试的配置示例。

```toml
[checks.unit]
argv = ["python3", "-B", "-m", "unittest", "discover", "-s", "tests", "-v"]
scan_failure = "AssertionError:"
```

根据项目实际检查结果选取标记。当前 igarle 配置注册了 Loop 自身的测试，尚未给业务代码注册自动巡检检查。

GitHub Actions 使用 `github.workflow` 和 `github.required_job` 指定必需的工作流与任务名。下面的配置通过提交状态接入外部 CI。

```toml
[github]
repository = "owner/repository"
branch = "master"
operator_ids = []
workflow = ".github/workflows/ci.yml"
required_job = "ci/test"
ci_source = "status"
status_actor = 123456
```

`status_actor` 是发布该状态的 GitHub 用户 ID。默认 `ci_source` 为 `actions`。GitHub 和 Pi 的密钥由各自的工具读取。模型所需的额外环境变量可以在 `[pi]` 的 `provider_env` 数组中逐项列出。

## 操作

在目标仓库根目录运行以下命令。

```bash
python3 -B .agents/loop/loop.py doctor --live --github
python3 -B .agents/loop/loop.py init
python3 -B .agents/loop/loop.py run --once
python3 -B .agents/loop/loop.py status
```

`doctor --live` 实际调用 Pi，验证扩展与结构化结果。`init` 绑定仓库 ID、当前 GitHub 操作者和目标分支。`run --once --local` 只推进一轮本地工作。`run` 持续运行。

合并前会再次查询目标分支和有效规则。已保护的分支须启用管理员也不能绕过的保护、严格的最新基线检查，并将配置的任务列为必需检查。当前版本遇到有效 ruleset 时会等待操作者处理；如果无法读取保护或规则，也不会自动合并。`doctor --github` 中的 `branch_rule_access` 可用于诊断读取权限。

登录状态下使用以下命令安装 macOS LaunchAgent。

```bash
python3 -B .agents/loop/loop.py service install
python3 -B .agents/loop/loop.py service status
python3 -B .agents/loop/loop.py service uninstall
```

LaunchAgent 在登录后启动并在进程退出时重启。插电时 `caffeinate -s` 防止系统自动睡眠。

## 接收任务与持续沟通

本地任务使用 JSON 文件，包含 `title`、`objective`、`constraints` 和 `acceptance_criteria` 四个非空字符串。

```bash
python3 -B .agents/loop/loop.py task add --spec /path/to/task.json
python3 -B .agents/loop/loop.py task show TASK_ID
python3 -B .agents/loop/loop.py history TASK_ID
python3 -B .agents/loop/loop.py history TASK_ID --evidence EVENT_ID
```

GitHub Issue 使用 `## Objective`、`## Constraints` 和 `## Acceptance criteria` 三个章节。绑定操作者使用以下命令获取与当前 Issue 版本绑定的授权文本。

```bash
python3 -B .agents/loop/loop.py prepare-issue ISSUE_NUMBER
```

将命令输出的 `/agent start <摘要>` 作为 Issue 评论发布。修改 Issue 后，对新内容再次生成并发布授权。需要人工决策时，Loop 在 Issue 中提供选项。操作者回复 `CHOOSE 决策ID A`，其中 `A` 替换为选择的选项。`task reply TASK_ID 决策ID A` 可以本地回复。

运行中的本地任务可通过 `task revise TASK_ID --spec /path/to/task.json` 更新。已绑定 Issue 的任务以 Issue 和新的授权评论为准。`task retry TASK_ID` 恢复需要环境修复的任务，`task rollback TASK_ID` 将候选退回同一需求版本的上一已验证提交并重新检查。

## 故障恢复与经验

SQLite 状态位于 `outputs/pi-loop/state.sqlite3`，Git 检查点保存在 `refs/pi-loop/checkpoints/`。任务事件包括 Pi 轮次、工具结果、检查结果、审查和 GitHub 操作回执。`status` 显示当前阶段、阻塞原因和结果待确认的远端写入。

GitHub 请求中断后，Loop 通过 Issue 标记、远端分支、PR 和提交确认结果。创建结果仍不明确时会保留待确认状态。操作者核实远端后，可停止 Loop 并通过 `task clear-intent TASK_ID KEY` 清除待确认操作，再启动同步。

同类执行失败默认重试 3 次。单任务的 GitHub 操作也独立退避并持久化重试次数；耗尽后任务进入 `blocked`，其他任务继续。GitHub 整体断线使用独立退避，本地队列仍可运行。任务内 Harness 改进在固定任务和基线上比较并独立审查，通过后仅影响当前任务。以下命令可查看、采纳和撤销跨任务经验。

```bash
python3 -B .agents/loop/loop.py learn list
python3 -B .agents/loop/loop.py learn adopt METHOD_ID
python3 -B .agents/loop/loop.py learn revoke METHOD_ID
```

## 验证 Loop

```bash
python3 -B -m unittest discover -s .agents/loop/tests -v
```

测试在临时 Git 仓库运行。GitHub 测试仓库与真实 Pi 端到端验收独立记录，最终以远端 PR、CI 状态及合并结果为准。
