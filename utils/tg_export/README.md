# Telegram 导出整理工具

这里的两个脚本只整理链接，不下载文件。先从 Telegram 的 HTML 聊天导出中提取 PikPak 链接、去重并分组，再生成 PikpakGet 能读取的 `链接<TAB>本地文件夹` 清单。

## 使用

在希望存放结果的目录中运行：

```bash
# 1. 解析导出的 messages*.html
python3 /path/to/PikpakGet/utils/tg_export/cluster.py /path/to/ChatExport

# 2. 查看分组，再选择要下载的分组
python3 /path/to/PikpakGet/utils/tg_export/make_links.py
python3 /path/to/PikpakGet/utils/tg_export/make_links.py C001 C002 -o links.txt

# 3. 开始下载
python3 -m pikpakget links.txt --dest /path/to/library
```

`cluster.py` 默认在当前目录生成：

- `pikpak_links_dedup.csv`：去重后的链接及其分组、出现次数、时间等信息。
- `pikpak_clusters.csv`：各分组的链接数量和统计信息。

`make_links.py` 从当前目录的 `pikpak_links_dedup.csv` 读取数据。不带参数时列出分组；可用完整分组名或列表中的 `C001` 等编号选择分组。`all` 会把所有分组写入一份 `all.txt`，`-o FILE` 可指定输出文件。生成同名文件会覆盖原文件。

## 可选词表

`cluster.py` 默认读取脚本旁边的 `vocabulary.csv`。没有这个文件也能解析、去重和按标签分组；名称合并及部分分类规则会减少。复制 `vocabulary.example.csv` 可查看三列格式：`kind,name,value`。

| `kind` | 用途 |
|---|---|
| `alias` | 合并同一名称的不同写法 |
| `tag` | 标记内容类别 |
| `list` | 指定不应当作作者名的词 |
| `rule` | 调整名称清理与分组规则 |
| `bucket` | 命名未识别或混合内容的分组 |

词表和生成的 CSV 可能包含私人聊天内容、真实链接或系列名，分享之前请检查。运行 `python3 cluster.py --help` 和 `python3 make_links.py --help` 可查看完整参数。
