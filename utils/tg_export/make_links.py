#!/usr/bin/env python3
"""Turn the clustering result into ordered download lists for pikpakget.

Reads `pikpak_links_dedup.csv` (what cluster.py writes) and emits one
file per chosen cluster, or a merged batch, as `<share url>\t<destination folder>`
lines — the exact input format `python3 -m pikpakget <list>` expects. It changes
no data, only picks and orders.

    python3 utils/tg_export/make_links.py                 # list available clusters
    python3 utils/tg_export/make_links.py --all           # one list per cluster
    python3 utils/tg_export/make_links.py A系列 B系列 --merged 第一批
"""
import argparse
import collections
import csv
import os

# 与 cluster.py 一致：就在 shell 的当前目录里读写
DEFAULT_CATALOGUE = 'pikpak_links_dedup.csv'
DEFAULT_OUT_DIR = '.'


def load(path):
    if not os.path.exists(path):
        raise SystemExit(f'找不到聚类结果 {path}\n'
                         '先生成：python3 utils/tg_export/cluster.py <导出目录>')
    rows = csv.DictReader(open(path, encoding='utf-8-sig'))
    by_cluster = collections.OrderedDict()
    for row in rows:
        cluster = (row.get('聚类名称') or '').strip()
        url = (row.get('分享链接') or '').strip()
        if not cluster or not url:
            continue
        order = int((row.get('聚类ID') or 'C000')[1:])
        by_cluster.setdefault(cluster, []).append((order, url, cluster))
    for items in by_cluster.values():
        items.sort()
    return by_cluster


def write_list(name, items, out_dir, folder_override=None):
    """One list file. The folder column defaults to the cluster name, which is the
    whole point: the catalogue's grouping becomes the library's layout."""
    path = os.path.join(out_dir, f'{name.replace("/", "_")}.txt')
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(f'# {len(items)} 条链接，来自 pikpak_links_dedup.csv\n')
        handle.write(f'# 下载： python3 -m pikpakget {os.path.basename(path)}'
                     f' --dest <库目录>\n')
        for _, url, cluster in items:
            handle.write(f'{url}\t{folder_override or cluster}\n')
    return path, len(items)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('clusters', nargs='*', help='聚类名称，可多个')
    parser.add_argument('--all', action='store_true', help='每个聚类各出一份列表')
    parser.add_argument('--merged', metavar='NAME', help='把选中的聚类合成一份列表')
    parser.add_argument('--folder', help='覆盖目标文件夹名（默认用聚类名称）')
    parser.add_argument('--catalogue', default=DEFAULT_CATALOGUE)
    parser.add_argument('--out-dir', default=DEFAULT_OUT_DIR)
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    by_cluster = load(args.catalogue)
    if args.merged:
        if not args.clusters:
            parser.error('--merged 需要至少一个聚类名')
        items = [item for name in args.clusters for item in by_cluster.get(name, [])]
        if not items:
            parser.error('这些聚类名在目录里不存在：' + ' '.join(args.clusters))
        path, count = write_list(args.merged, items, args.out_dir, args.folder)
        print(f'写出 {path}  {count} 条')
        return
    names = list(by_cluster) if args.all else args.clusters
    if not names:
        print('可用聚类（前 40 个，按链接数）：')
        ranked = sorted(by_cluster.items(), key=lambda kv: -len(kv[1]))
        print('  ' + '  '.join(f'{name}({len(items)})' for name, items in ranked[:40]))
        return
    for name in args.clusters:
        if name not in by_cluster:
            print(f'跳过未知聚类: {name}')
    for name in names:
        if name in by_cluster:
            path, count = write_list(name, by_cluster[name], args.out_dir, args.folder)
            print(f'写出 {path}  {count} 条')


if __name__ == '__main__':
    main()
