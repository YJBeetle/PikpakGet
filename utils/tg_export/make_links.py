#!/usr/bin/env python3
"""Build one pikpakget download list from selected clusters.

Run in the directory containing pikpak_links_dedup.csv:

    make_links.py                      # show all group IDs and names
    make_links.py '系列甲' '系列乙' -o 第一批.txt
    make_links.py all -o 全部.txt
"""
import argparse
import collections
import csv
import os
import shlex

# 与 cluster.py 一致：就在 shell 的当前目录里读写
DEFAULT_CATALOGUE = 'pikpak_links_dedup.csv'
DEFAULT_OUTPUT = 'links.txt'


def load(path):
    if not os.path.exists(path):
        raise SystemExit(f'找不到分组结果 {path}\n'
                         '先生成：python3 utils/tg_export/cluster.py <导出目录>')
    by_cluster = collections.OrderedDict()
    with open(path, encoding='utf-8-sig', newline='') as handle:
        for position, row in enumerate(csv.DictReader(handle)):
            cluster_id = (row.get('分组ID') or '').strip()
            name = (row.get('分组名称') or '').strip()
            url = (row.get('分享链接') or '').strip()
            if not cluster_id or not name or not url:
                continue
            group = by_cluster.setdefault(cluster_id, {'name': name, 'items': []})
            if group['name'] != name:
                raise SystemExit(f'分组 CSV 中 {cluster_id} 的名称不一致')
            group['items'].append((position, url, name))
    return by_cluster


def write_list(path, items):
    """Keep the CSV order and use each cluster's name as its destination folder."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(f'# {len(items)} 条链接，来自 pikpak_links_dedup.csv\n')
        handle.write(f'# 下载： python3 -m pikpakget {shlex.quote(path)}'
                     f' --dest <库目录>\n')
        for _, url, name in items:
            handle.write(f'{url}\t{name}\n')
    return path, len(items)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     usage='%(prog)s [list | all | 分组名或编号 ...] [-o FILE]',
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('selection', nargs='*', metavar='GROUP',
                        help='list：列出分组；all：全部；其余填完整分组名或 C001 这样的编号')
    parser.add_argument('-o', '--output', metavar='FILE',
                        help='生成的清单文件（默认：links.txt）')
    args = parser.parse_args()
    listing = not args.selection or args.selection == ['list']
    if listing and args.output:
        parser.error('列出分组时不需要 -o；请选择编号或 all 后再生成清单')
    if not listing and ('list' in args.selection or
                        ('all' in args.selection and args.selection != ['all'])):
        parser.error('list 或 all 必须单独使用')
    by_cluster = load(DEFAULT_CATALOGUE)
    if listing:
        print(f'可用分组：{len(by_cluster)} 组')
        for cluster_id, group in by_cluster.items():
            print(f'{cluster_id}  {group["name"]}  {len(group["items"])} 条')
        return
    if args.selection == ['all']:
        selected = list(by_cluster)
    else:
        selected = []
        for requested in args.selection:
            if requested in by_cluster:
                cluster_id = requested
            else:
                matches = [cluster_id for cluster_id, group in by_cluster.items()
                           if group['name'] == requested]
                if not matches:
                    parser.error(f'没有分组 {requested!r}；先不带参数查看完整名称')
                if len(matches) > 1:
                    parser.error(f'分组名 {requested!r} 对应多个编号：'
                                 f'{"、".join(matches)}；请改用编号')
                cluster_id = matches[0]
            if cluster_id in selected:
                parser.error(f'分组 {requested!r} 重复选择')
            selected.append(cluster_id)
    items = [item for cluster_id in selected for item in by_cluster[cluster_id]['items']]
    path, count = write_list(args.output or DEFAULT_OUTPUT, items)
    print(f'写出 {path}：{len(selected)} 组、{count} 条链接')


if __name__ == '__main__':
    main()
