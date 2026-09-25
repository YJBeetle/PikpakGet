# PikpakGet（中文说明）

[English](README.md) | 中文

在云盘配额很小的情况下，按顺序把 PikPak 分享链接里的东西抓下来。只用 Python 标准库，
不需要浏览器、不需要 GUI 自动化、也不需要安装官方桌面客户端。

它要解决的问题是：PikPak 免费账号只有 **6 GiB** 云盘空间（6 442 450 944 字节），而一个分享
文件夹动辄上百 GB 到几十 TB。"先全部转存再慢慢下载"这条路根本走不通 —— 所以这个工具**一次只处理
一个文件**：

```
把 1 个文件转存进账号网盘的 `.pikpakget` → 下载到本地 → 核对内容 hash → 永久删除云盘副本
        → 等配额真的回落 → 下一个
```

文件进度记录在 `state.json`。所以 `Ctrl-C`、断网或重启后，重跑同一条命令会从未完成
的文件继续；启动时会清空所选账号网盘 `.pikpakget` 内的旧转存副本。

## 安装

需要 Python 3.10+、macOS 或 Linux，以及 PATH 里有 `curl`（只有多分段并发才用到，
默认的单连接不需要）。不依赖任何第三方 Python 包。

```bash
git clone git@github.com:YJBeetle/PikpakGet.git
cd PikpakGet
python3 -m pikpakget --help           # 直接在仓库目录里跑
pip install -e . && pikpakget --help  # 或者装成一个命令行工具

# 也可以直接从 release 资产装，不必克隆（资产从 v0.1.4 起才随 tag 生成，因为那是第一个
# 带上构建工作流的 tag）：
# pip install https://github.com/YJBeetle/PikpakGet/releases/download/v<版本>/pikpakget-<版本>-py3-none-any.whl
```

## 使用

```bash
# 1. 登录一个或多个账号；各账号会话分开保存，密码不落盘
python3 -m pikpakget --login first@example.com
python3 -m pikpakget --login second@example.com
python3 -m pikpakget --accounts                    # 查看账号与轮换顺序

# 2. 可选：先看清这批链接有多大，这一步完全不占云盘空间
python3 -m pikpakget links.txt --inventory --inventory-out inventory.csv

# 3. 开下
python3 -m pikpakget links.txt --dest /path/to/library
```

`links.txt` 一行一个分享链接，从上到下依次处理。`#` 开头的行忽略。可选地用
**制表符**加第二列指定落盘文件夹名，这样怎么归档都由你决定：

```
https://mypikpak.com/s/SOME_SHARE_ID	某个系列
https://mypikpak.com/s/ANOTHER_ID
```

不想写在链接文件里，也可以用 `--folder-map` 传一份 `url,folder` 的 CSV/TSV；
两边都匹配不上的行会落到 `--default-folder`。

其他常用入口：

```bash
python3 -m pikpakget links.txt --status   # 进度、实测速度、剩余时间
python3 -m pikpakget --version
python3 -m pikpakget --whoami             # 云盘用量、订阅到期
python3 -m pikpakget links.txt --dry-run  # 只排计划，不转存、下载或清理云端
python3 -m pikpakget links.txt --max-files 5   # 先试一小口
python3 -m pikpakget links.txt --account first@example.com  # 只用指定账号
```

## 参数

| 参数 | 默认 | 含义 |
|---|---|---|
| `--dest DIR` | `~/Downloads/PikPak` | 库根目录；每个分组落成 `DIR/<文件夹>/`。进度记的是绝对路径，换 `--dest` 等于开第二个库 —— 想固定下来用 `--set-config dest=...` |
| `--set-config KEY=VALUE` | — | 把 `dest` 记进 `~/.pikpakget/config.json` 后退出，下次不用带参数（`--set-config dest=` 清掉）。只有 `dest` 可记 |
| `--connections N` | `1` | 单文件用几条连接；大于 1 才启用分段下载 |
| `--gap SEC` | `20` | 文件之间歇一下，压低请求密度 |
| `--log PATH` | 进度目录里的 `grab-<日期>.log` | 传 `-` 表示只输出到终端 |
| `--repeat N` | `0`（一轮） | 多轮扫尾，回头补之前失败的；整轮零进展就停 |
| `--limit N` / `--max-files N` | `0`（不限） | 最多处理 N 个链接 / N 个文件 |
| `--inventory` | 关 | 只统计每个链接的文件数/字节/最大单文件，不下载 |
| `--doctor` | 关 | 换机器前的预检：Python、curl、FIPS 下的 SHA-1、目录与剩余空间、大小写折叠、会话与配额、云端残留副本；不占额度，有阻塞项时退出码 1 |
| `--verify` | 关 | 用云端内容 hash 复核所有已下载文件；对不上的改名 `.unverified` 保留（绝不删除），有不符则退出码 1 |
| `--dry-run` | 关 | 不转存、下载或清理云端；账号会话可能自动续期 |
| `--no-delete` | 关 | 本次下载完暂不删云盘副本；下次实际下载启动仍会清空临时文件夹 |
| `--purge-trash` | 关 | 允许在配额卡住时清空整个回收站 |
| `--account NAME` | 自动选择 | 只用指定账号；可传显示名称或 `--accounts` 列出的 ID |
| `--accounts` | — | 按轮换顺序列出已登录账号 |
| `--folder-map FILE` | 无 | 给没有第二列的链接行提供 `url,folder` 映射 |

## 配额的真实行为（实测）

下面这些都是在免费账号上实测出来的，规划大任务之前值得先看：

- **回收站算额度。** `usage_in_trash` 是 `usage` 的一部分，所以只 `batchTrash`
  一点空间都释放不出来；必须 `batchDelete` 永久删除 —— 而且接口调用之后还有几秒
  延迟，所以工具会在开始下一个文件前**轮询配额确认真的回落**。
- **分享只能拆开转存。** 转存一个文件夹 id 会连同整棵子树一起复制，装不下就直接
  失败；所以先遍历清单（免费且瞬时），再按单个文件 id 逐个转存。
- **比配额还大的文件根本拿不到。** 6 GiB 的云盘放不下 7.1 GiB 的文件，没有任何工具
  能绕过这点。工具会把它标成 `too_big` 跳过，并在 `--inventory` 里计入
  `unfetchable`，而不是转存到一半失败把云盘堵死。
- **离线任务位有限**（`quota.cloud_download`，免费是 3）。一次一个文件的顺序流程
  远在上限之内。
- **每天还有 20 GB 的下行流量上限，比存储额度先到。** 撞到它的那次请求返回的是普通
  的 `HTTP 400` 加一段推销文案 —— 看起来像某个文件坏了，其实是账号今天到顶了。工具
  认得这个错误，归还该文件的尝试次数，释放账号锁并尝试下一个未锁定的账号。
  所有账号都触顶时停止，稍后重跑同一命令即可继续。
- **登录被拒通常是出口地址的问题，不是密码。** PikPak 会直接回 `AccessProhibited`
  （HTTP 400）—— 实测有一台机器直连地址被拒，换到另一个出口几秒后就登录成功。标准
  `*_proxy` 环境变量两半都吃（API 走 `urllib`、分段走 `curl`）—— 但**系统级/桌面代理
  设置两边都不读**，所以同一台机器"浏览器能开、这里被拒"是完全可能的。`--doctor` 会把
  当前实际生效的情况打出来；走代理下载的字节同样计入那个每日上限。
- **限速是账号级的，不是连接级的。** 长时间跑下来，单连接实测 0.13–0.7 MB/s（会随
  时段下滑）。4 条分段合计只有约 0.25 MiB/s，而且 4 段里有 2 段**一个字节都没拿到**
  —— 也就是说分段最多带来约 1.5× 收益，多余的通道会被饿死。因此默认改成稳定的
  单连接；需要自行尝试分段时再指定 `--connections 4`。

当某一轮完全没有进展时，工具会用一个 1 字节的 range 请求去**探测** CDN，因为两种
失败原因的应对恰好相反：**被拒**（HTTP 4xx/5xx）是策略信号，于是整轮降级为单连接并
保持；**被饿死**（2xx 但没字节）只是带宽整形的运气问题，于是按退避重试分段，而不是
一个接一个地放弃文件。

按这个规划时间：一位作者主文件夹的 405 GiB，在 0.13–0.6 MiB/s 下大约需要一到四周
连续运行。

## 不做风控的靶子

这里没有任何"绕过计费"或隐藏自身的东西：用的就是官方移动端和已开源 SDK 使用的同一
套 HTTP 接口和公开应用客户端常量，服务端要验证码就照给。在此之上工具还主动降速：

- `--connections > 1` 时，通道是一条一条开的（间隔 3 秒）而不是瞬间全开，且真被拒
  就把整轮降到单连接；
- 被拒或卡死的分段按 **60s → 300s → 900s** 重试，不会立刻猛撞；
- 遇到 `429/503/slow down` 按 **30s → 900s** 退避，并丢弃缓存的验证码 token；
- 连续 3 次达到风控级别的失败就**整轮停手**并提示一小时后再来，而不是一直撞到账号
  被标记；
- 撞上**每日下行流量上限**时立即切到下一个未锁定账号；所有账号都用尽后停手；
- `--gap` 在每个文件之间歇 20 秒，`--limit` / `--max-files` 让你按批有意地做。

如果被限流了，把 `--connections` 降到 `1` 并把 `--gap` 调大。

## 删除

每个账号的网盘根目录使用专用的 `.pikpakget` 文件夹。实际下载进程取得本地库锁和账号锁
之后，会**永久删除该文件夹内的全部内容**，再开始转存；文件夹本身保留。请勿把个人文件
放进去。`--dry-run`、`--inventory`、`--status` 和 `--doctor` 不执行这项清理。
每个文件下载完成后也会清理其云端副本。`--purge-trash` 仍需显式开启，因为它清空的是
账号的**整个回收站**。

## 文件名

一个链接里的所有文件都平铺落在 `--dest/<文件夹>/` 下，`<文件夹>` 来自链接文件。
远端名字是别人的目录结构，所以不同子目录里同名的文件很常见：第二个会存成
`<父目录名> - <文件名>`，再冲突就 `... (2)`，并且这个选择会记进 `state.json`，
保证续跑时写到同一个路径而不会下出第二份。

## 日志

终端里看到的同样内容会追加到进度目录里的 `grab-<日期>.log` —— 因为多天的任务活得
比终端久。`--log -` 可以关掉。每个库的进度、锁和日志都在 `<库>/.pikpakget/`。
仓库目录里现在什么都不写。

## 目录放在哪儿

| 目录 | 装什么 |
|---|---|
| `~/.pikpakget/` | `accounts.json`、`config.json`；旧版顶层 `device_id` 不再使用 |
| `~/.pikpakget/accounts/<账号ID>/` | 该账号独立的 `device_id`、`session.json`（均为 0600）和设备级账号锁 `lock` |
| `<库>/.pikpakget/` | `state.json`、单实例锁 `lock`、日志；默认库也一样 |

背后两条规则：库靠路径识别，所以把 `--dest` 指到新地方就是开第二个库，而不是接着跑；
每个库带着自己的**进度**，登录数据则留在设备上的 `~/.pikpakget/`。
已有账号若缺少账号目录下的 `device_id`，须重新执行该账号的 `--login`；不会拿新设备号
继续使用旧会话。退出账号会删除其会话和设备号，再次添加时生成新的设备号。

## 完整性校验

每个文件在 `.part` 改名成正式文件之前，都要用 PikPak 自己的内容 hash 核对一遍。那个
`hash` **不是**文件的 SHA-1，而是"按固定大小切块、每块取 SHA-1、再把这些摘要串起来取
SHA-1"，切块大小由上传者当时用的客户端决定：26 个文件的库里出现了三种尺寸（1 MiB 15
个、2 MiB 8 个、512 KiB 1 个），所以 `stream.verify_content` 按候选尺寸逐个试，命中
的记下来，下个文件先拿它试。

这道闸拦的是光核字节数看不见的损坏：两个写入者写同一段，留下的是"长度正好、中间错位"
的文件。

哪个尺寸都对不上时，工具**不**据此判定文件坏了 —— 规则是逆推的，上传端的分片尺寸也没
上界。它先在剩余重试次数内重下，最后一次仍不匹配就**保留字节**，改名成
`<名字>.unverified`，记录里标成 unverified 并在日志里喊一声。想再要一次就去删掉那个
隔离文件。`--verify` 会复核整个库：隔离的文件一旦算得出来就放回原名，原本算得出来的
被隔离并退回队列 —— 所以"重跑下载命令"确实就是修复手段。新的一份核对通过后，被它取代
的隔离字节会被删掉；在那之前记录同时指向两份，不会留下没人引用的孤儿文件。

## 安全说明

- 非默认库会把自己的 `state.json` 放在 `<库>/.pikpakget/` 里。进度文件记着真实的分享
  链接、云盘 file id 和本地路径，所以这个名字在 `.gitignore` 里是**不限层级**地忽略的
  —— 别把任何一份提交上去。
- `~/.pikpakget/accounts/<账号ID>/` 存着各账号会话（`access_token`、一次一换的 `refresh_token`），已在
  `.gitignore` 内，会话文件写成 `0600`；`--logout` 会删掉它。
- `pikpakget/api.py` 里的 `CLIENT_ID` / `CLIENT_SECRET` 是官方客户端的**公开应用
  常量**（客户端本体和已开源的 SDK 里都带着），不是你的账号凭据。除
  `user.mypikpak.com` / `api-drive.mypikpak.com` 外不会向任何地方发送数据。
- 没有遥测，没有第三方 HTTP 库，除 PikPak 之外没有任何网络请求。

## 代码结构

```
pikpakget/api.py       HTTP 客户端：会话、验证码签名、分享/云盘/回收站接口
pikpakget/accounts.py  设备账号列表与账号锁
pikpakget/stream.py    单流断点续传、多分段并发、内容 hash 规则
pikpakget/pipeline.py  链接解析、状态日志、配额逻辑、status/inventory/verify
pikpakget/cli.py       参数解析、单实例锁、信号处理
tests/test_pure.py     153 项纯逻辑测试；不涉及真实账号、不联网
tests/test_accounts.py   8 项多账号与锁测试；使用合成账号
tests/test_rotation.py   2 项账号切换与全部被锁测试；使用合成账号
tests/test_transfer.py   5 项真下载测试：本地 HTTP + 真 curl
utils/tg_export/       TG 导出 -> 按链接去重 -> 按作者/系列分组 -> 生成下载清单
```

## 开发

```bash
python3 -m unittest discover -s tests -t . -v
```

测试刻意不碰任何真实账号或分享数据，夹具全是合成 id。你加用例时请保持这一点。

## 已知限制

直说，因为每一条都真的咬过人：

- **hash 规则是逆推出来的，没有文档。** 校验依赖对 `hash` 字段的一份逆向理解（分片
  SHA-1 的 SHA-1，分片大小靠候选列表试，而这个列表已经被迫扩过一次）。对不上的文件最多
  重下 3 次，之后改名成 `.unverified` 保留 —— 不会因为一个猜测就删你的字节，但云端 hash
  本身就是旧的时会白烧这 3 次流量。传输有 TLS 保证，服务端那份自洽的坏副本没法防。
- **对 PikPak 服务本身的调用只在 macOS 上实际跑过。** 字节传输那一层 —— 四段并发
  `curl`、拼接、续传、越界分段规则、内容 hash —— 有真下载的集成测试兜着，每次 CI 都在
  Linux（Python 3.10–3.14）上跑一遍，所以这些路径在 Linux 上也成立；但登录、验证码、
  转存、配额这条链路只从 macOS 打过真实服务。Windows 不支持：命令行能起来
  （`--version`、`--help` 可用），但真正的运行会在拿单实例锁之前用一句人话拒绝 ——
  那是 POSIX 的 `fcntl`。
- **饿死会自动降级。** 连续两个文件的分段只拿到 2xx 却拿不到字节时，本次运行不再
  使用分段 —— 每个文件白等 60+300+900 秒退避，比老实用一条连接下还慢。
- **"被拒→降级"这条路径还没在真实环境里触发过。** HTTP 4xx/5xx 后降单连接有单测覆盖，
  但目前观察到的都是"饿死"（连不上带宽）而不是"被拒"，所以这个应对是按响应形状设计
  的，不是经验总结。
- **单个文件大于云盘配额就拿不到**，任何工具都一样：得先能塞进云盘才谈得上下载。这类
  文件会被跳过并计入 `--inventory` 的 `unfetchable`。
- **长期稳定性未证实。** 多天运行是设计目标、续传路径有日志保障，但第一次真实的多日
  任务正在进行中。
- **上面的速度数字只是某个晚上的观测**，随时段漂移，请当成量级而不是容量。
- **贴日志前先脱敏。** `grab-*.log` 与 `state.json` 里有你的分享链接和下载文件名，
  `session.json` 里是令牌；`SECURITY.md` 给了一行清理命令，也说明了哪些东西是刻意公开的。
- **没发到 PyPI。** 每个 release 都会把打好的 wheel 与 sdist 挂到自己的资产上（见
  `.github/workflows/release-assets.yml`），`pip install <资产地址>` 即可；从克隆装是
  `pip install -e .`。

## 换一台机器之前

`python3 -m pikpakget --doctor --dest <你打算存放的目录>`
回答的是"这台机器撑得住吗"：Python 版本底线、`curl`（只有分段下载需要它，
`--connections 1` 不需要）、解释器的 SHA-1 能不能用（FIPS 会拒）、两个目录是否存在可写且
空间够、锁有没有被别的实例占着、会话还剩多久，以及配额和云盘上的残留副本。它不需要 links
文件、不占额度，下载进行中也能跑。退出码 1 表示有必须先处理的项，所以 cron 包装脚本可以
拿它当闸门。

## 平台说明

按 macOS 编写，单实例锁用 `fcntl`，路径语义是 POSIX 的；Windows 上会以一句说明拒绝
运行，而不是抛 ImportError。每个路径成分会被截断到 200 UTF-8 字节以内，以躲开
`NAME_MAX`。

**大小写折叠的卷已经处理。** macOS 默认 APFS 和多数 SMB/CIFS 挂载（NAS 共享最常见的
接法）把 `Movie.mp4` 与 `movie.mp4` 当成同一个路径，ext4 则当成两个。目的卷只探测一次；
在折叠卷上，已用文件名的"换个大小写"会按普通重名让路，而不是把前者覆盖掉。

## 许可

MIT
