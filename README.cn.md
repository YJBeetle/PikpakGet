# PikpakGet

[English](README.md) | 中文

PikpakGet 按顺序下载 PikPak 分享链接中的文件。它先把单个文件转存到账号网盘，下载并核对内容后清理云端副本，再处理下一个文件。这样可以下载总大小超过网盘可用空间的分享；**单个文件仍必须放得进当前账号的网盘**。

支持多个账号。一个账号的下行流量用完后，程序会尝试下一个未被本机进程占用的账号。下载任务本身仍然是顺序执行，不会同时用多个账号下载。

## 安装

需要 Python 3.10+ 和 macOS 或 Linux。默认单连接下载不需要额外依赖；使用 `--connections` 分段下载时需要 `curl`。

```bash
git clone https://github.com/YJBeetle/PikpakGet.git
cd PikpakGet
python3 -m pikpakget --help
# 或安装为命令：python3 -m pip install -e .
```

## 开始下载

```bash
# 登录一个或多个账号；会话保存到各自的账号目录，密码不落盘
python3 -m pikpakget --login first@example.com
python3 -m pikpakget --login second@example.com
python3 -m pikpakget --accounts

# 查看计划，然后下载
python3 -m pikpakget links.txt --dry-run --dest /path/to/library
python3 -m pikpakget links.txt --dest /path/to/library
```

`links.txt` 每行一个分享链接。空行和以 `#` 开头的行会跳过。用制表符在链接后面写文件夹名，可以指定它在本地的归档位置：

```text
https://mypikpak.com/s/SHARE_ID_1	电影
https://mypikpak.com/s/SHARE_ID_2	剧集
```

没有指定文件夹的链接会放进 `(unfiled)`。也可以通过 `--folder-map` 传入 `url,folder` 格式的 CSV 或 TSV 文件。

**首次实际下载前，请确认账号网盘根目录的 `.pikpakget` 文件夹只用于本工具。** 每次开始使用一个账号下载时，程序都会永久删除该账号这个文件夹内的全部内容。`--dry-run`、`--inventory`、`--status`、`--verify` 和 `--doctor` 不执行云端清理。`--purge-trash` 会清空账号的整个回收站，只有显式开启才会执行。

## 常用命令

| 命令 | 用途 |
|---|---|
| `python3 -m pikpakget --accounts` | 查看已登录账号及使用顺序 |
| `python3 -m pikpakget --logout --account NAME` | 删除指定账号的本地会话和设备 ID |
| `python3 -m pikpakget --whoami --account NAME` | 查看该账号的云盘用量和订阅到期日 |
| `python3 -m pikpakget links.txt --inventory` | 统计分享文件、总大小及放不进云盘的文件 |
| `python3 -m pikpakget links.txt --dry-run` | 枚举下载计划，不转存、下载或清理云端 |
| `python3 -m pikpakget links.txt --status` | 查看此下载目录的进度 |
| `python3 -m pikpakget links.txt --verify` | 复核已下载文件的内容 hash |
| `python3 -m pikpakget --doctor` | 检查目录、锁、会话及网盘状态 |

常用参数：

| 参数 | 默认值 | 作用 |
|---|---|---|
| `--dest DIR` | `~/Downloads/PikPak` | 本地下载库；每个链接落在 `DIR/<文件夹>/` |
| `--set-config dest=DIR` | 未设置 | 记住默认下载库；`dest=` 清除设置 |
| `--account NAME` | 自动选择 | 只用指定账号，不自动切换；可填账号名称或 `--accounts` 显示的 ID |
| `--connections N` | `1` | 单文件连接数；大于 1 时使用 `curl` 分段下载 |
| `--max-files N` | 不限 | 限制本次处理的文件数；失败尝试也可能占用名额 |
| `--limit N` | 不限 | 本次只处理前 N 个链接 |
| `--repeat N` | `0` | 最多扫描 N 轮；0 表示只扫一轮 |
| `--gap SEC` | `20` | 文件之间的等待秒数 |
| `--no-delete` | 关闭 | 暂时保留本次转存的云端副本；下一次实际下载启动时仍会清理 |
| `--purge-trash` | 关闭 | 空间不足时允许清空整个账号回收站 |
| `--log -` | 写入日志文件 | 只输出到终端 |

其他选项见 `python3 -m pikpakget --help`。

## 进度和账号

本地下载库的进度、锁和日志在 `DIR/.pikpakget/`。同一个库一次只允许一个下载进程写入。账号资料保存在运行设备的 `~/.pikpakget/`：

```text
~/.pikpakget/
├── accounts.json                  # 账号名称和使用顺序
└── accounts/<账号ID>/
    ├── device_id                 # 该账号专用
    ├── session.json              # 登录令牌
    └── lock                      # 本设备上的账号锁
```

账号 ID 来自服务端用户 ID 的哈希，同一账号重新登录会沿用自己的设备 ID。换一个 `--dest` 就是另一套下载进度。账号锁只在本机生效：多台设备同时使用同一账号或网盘文件夹，程序无法互相协调。

程序会保存已完成文件的记录，重跑同一命令可继续未完成部分。已完成的分享也会重新读取清单，以发现后来新增的文件。遇到每日下行流量限制时，当前账号会退出本轮，程序尝试下一个可用账号；全部账号被占用时会报告“没有可用账号”。

## 文件校验与限制

下载先写入 `.part`，完成后尝试用分享提供的内容 hash 验证。这个 hash 的分块规则是从样本推断的；没有候选分块大小能匹配时，不等于字节一定损坏。程序会在重试用尽后把文件保留为 `.unverified`，不会把它当作已验证文件。

- 单个文件大于账号可用网盘空间时会跳过。
- `--connections 1` 是默认值；分段下载可能因服务端限速而更慢。
- 网盘 API、配额和风控行为可能变化。多账号轮换不能保证每个账号都能成功下载。
- Windows 原生环境不支持所需的 POSIX 文件锁；可在 Linux、macOS 或 WSL 中运行。
- 测试覆盖本地逻辑和本地 HTTP 下载；多账号云端清理与切换尚未用真实账号完成端到端验证。

日志和进度文件包含分享链接、文件名及本地路径；会话文件包含可用的登录令牌。不要直接上传这些文件。详见 [SECURITY.md](SECURITY.md)。

## 开发

```bash
python3 -m unittest discover -s tests -t . -v
```

项目使用 MIT 许可证，见 [LICENSE](LICENSE)。
