# 使用指南

[中文 README](../README.zh-CN.md) · [English guide](USAGE.en.md)

轻量的 Oracle Cloud A1 实例创建重试工具。交互式填写配置，按分钟重试，成功创建一台后停止。


## 快速开始

准备 Python 3.10+、Oracle API 签名密钥、SSH 公钥，以及主区域根 compartment 下的现有公网子网。

**Windows：** 解压项目后双击 `start.cmd`。第一次会创建独立 Python 环境并安装依赖。

**Linux / macOS：** 在项目目录运行：

```sh
sh start.sh
```

菜单：

```text
1. 配置向导
2. 检查配置（只读）
3. 启动/继续抢机
4. 查看状态
5. 请求停止
6. 修改重试时间
0. 退出
```

首次依次选 **1 → 2 → 3**。启动后保持窗口打开；`Ctrl+C` 停止并返回菜单。电脑需要保持开机且不休眠。关闭终端、关机或重启会中断进程，本工具不会自动设置开机启动。

## 配置向导

支持粘贴 Oracle 的 **Configuration file preview**，或导入已有 `~/.oci/config` 并选择 Profile。粘贴结束后另起一行输入 `.`。也可以逐项手动填写。

向导会检查 API 私钥与指纹是否匹配，然后读取可用区和现有公网子网，按编号选择。配置保存在本机 `config.ini`；只保存私钥路径，不复制私钥或保存密码。保存配置和“检查配置”都不会创建实例。

| 输入 | 从哪里获取 |
| --- | --- |
| User / Tenancy / Region / Fingerprint | Oracle 用户设置 → Tokens and keys → API keys → Configuration file preview |
| API 私钥 `.pem` | 与已注册 API 公钥配对的本地 RSA 私钥；至少 2048 位 |
| SSH 公钥 `.pub` | 用于登录新服务器的 SSH 公钥，与 API 签名密钥用途不同 |
| 公网子网 | 自动列出根 compartment 下的现有子网，也可手动填写 Subnet OCID |

API 密钥需先在 Oracle 注册。操作步骤见 [Oracle API 签名密钥文档](https://docs.oracle.com/en-us/iaas/Content/API/Concepts/apisigningkey.htm)，配置字段见 [SDK 配置文档](https://docs.oracle.com/en-us/iaas/Content/API/Concepts/sdkconfig.htm)。

如果私钥有密码，请在运行进程的环境中设置 `OCI_KEY_PASSPHRASE`；不要把密码写进配置、命令行参数或 Git。导入 OCI 配置时不会复制其中的 `pass_phrase` 字段。

## 按分钟设置

菜单 **6** 可以单独修改时间，无需重新输入账户信息。先停止正在运行的任务，修改后再启动。

```ini
[retry]
interval_minutes = 2.5
max_minutes = 120
jitter_seconds = 30
max_backoff_seconds = 900
check_capacity_first = false
```

- `interval_minutes`：每轮完成后等待的分钟数，范围 1–60，支持小数。例：`2.5` 表示 2 分 30 秒。
- `max_minutes`：本次进程最长运行时长；`0` 表示不限，否则至少 1 分钟。再次启动重新计时。
- `jitter_seconds`：每次额外随机等待 0–30 秒，设 `0` 可关闭。请求本身也需要时间，因此不是固定整点执行。
- 限流和临时错误会延长等待，最高为 `max_backoff_seconds` 加抖动；上限至少等于正常间隔。
- 到时或停止时会等待当前一轮 API 调用返回，保存未确认请求，不强行打断创建；不会删除已创建的资源。

旧配置的 `interval_seconds` 仍可读取；同时存在时优先使用 `interval_minutes`。

## 命令行

若自行管理 Python 环境，先运行 `python -m pip install -r requirements.txt`。启动脚本已自动处理依赖；也可把下列 `python grab.py` 换为 Windows 的 `start.cmd` 或 Linux/macOS 的 `sh start.sh`。

```sh
python grab.py --setup
python grab.py --check
python grab.py --resume --interval-minutes 3 --max-minutes 180
python grab.py --status
python grab.py --stop
```

时间参数只覆盖本次运行，不修改配置。`--resume` 明确清除停止标记；普通启动会尊重已有停止请求。`--once` 最多执行一轮，**可能创建实例**。

## 后台运行

后台运行示例（Linux/macOS，完成首次配置与检查后）：

```sh
mkdir -p runtime
nohup .venv/bin/python grab.py --resume > runtime/console.log 2>&1 &
```

Windows PowerShell 后台运行示例（完成 `start.cmd` 首次安装后）：

```powershell
Start-Process -FilePath "$PWD\.venv\Scripts\python.exe" -ArgumentList 'grab.py','--resume' -WorkingDirectory $PWD -WindowStyle Hidden
```

后台运行可随时另开终端查看状态、请求停止；日志在 `runtime/grab.log`。这是普通 Python 进程，不会自动重启。长时间托管可自行交给操作系统服务管理器；GitHub Actions 配置只用于测试，不运行抢机任务。

## 状态与恢复

| 文件 | 用途 |
| --- | --- |
| `runtime/grab.log` | 最近请求结果、等待时间，自动轮转 |
| `runtime/run.json` | 进程活动、更新时间、预计下次尝试 |
| `runtime/state.json` | 尝试次数、幂等令牌、实例 OCID 和成功后的公网 IP |
| `runtime/needs-attention.json` | 需要处理的凭据、权限、配额或配置问题 |

状态命令会检查操作系统锁，区别“日志最后显示等待”和“进程现在确实活着”。异常退出后，再次运行会先核对已有实例，并复用未确认请求的令牌。**不要删除 `state.json` 来解决报错**；临近令牌有效期时会停止并要求核实云端结果。

每个配置目录只能运行一个进程。修改时间不会清空尝试记录。存在历史状态时，修改账户、规格或实例名称会被阻止，避免误建第二台。同一账户不要同时运行多个独立目录的抢机任务；本地锁无法协调不同机器。

## 当前范围

- 支持配置任意 OCI 区域，但会核对它必须是租户主区域；选择一个可用区，不自动跨可用区轮询。
- A1，默认 2 OCPU / 12GB；也可选 1 OCPU / 6GB。启动盘默认 50GB。
- 自动选择兼容 A1 的 Oracle Linux 9 官方平台镜像；SSH 用户通常为 `opc`。
- 使用根 compartment 下已有公网子网；不创建 VCN、不改安全规则。自动选择子网只检查其公网属性，SSH 是否可达还取决于现有路由与安全规则。
- 创建前检查本区域所有可访问 compartment 的实例和磁盘；已有其他 A1、磁盘合计加本次超过 200GB、查询失败或权限不足时停止。
- 不升级付费账户、不删除资源。容量报告可选，默认直接调用创建 API 并在明确缺货时重试；有容量提示也不等于预订成功。

免费额度以 [Oracle Always Free 官方说明](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm) 和账户实际计费为准。2026-11-02 核对的文档给出 A1 每月 1,500 OCPU 小时、9,000 GB 小时，Always Free 租户相当于 2 OCPU / 12GB。本工具的用量检查不能覆盖本月已消耗的小时数、其他程序并发创建或未来价格变化，也不承诺一定抢到资源。

## 开发与发布

```sh
python -m unittest discover -s tests -v
python tools/build_release.py
```

构建结果为 `dist/oracle-always-free-arm-launcher.zip`。打包脚本只收录明确列出的源码、文档、空白示例配置和测试，会检查实际 OCID、私钥块等误入内容。`.gitignore` 排除本机配置、密钥、日志、运行状态和虚拟环境。请只发布源码目录或构建后的 ZIP，不上传个人运行目录。

GitHub Actions 已配置 Windows / Ubuntu、Python 3.10 / 3.13 的离线测试。测试使用模拟 API，不使用任何真实账户或密钥。

## 许可证

[Apache-2.0](../LICENSE)。分发声明见 [NOTICE.txt](../NOTICE.txt)。
