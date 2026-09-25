#!/usr/bin/env python3
"""Dedupe PikPak share links found in a Telegram chat export and cluster them by
author/series, writing utf-8-sig CSVs.

The vocabulary this clusters on is the channel's own set of hashtags, so the group
count stays finite instead of one-per-file-name. Everything on top of that —
content tags, name merges (typos, "X studio" vs "X"), the suffix rules and the
bucket names — is data: it lives in vocabulary.csv next to this script, which is
gitignored because it is somebody's catalogue.
"""
import csv
import collections
import glob
import html
import os
import re
import unicodedata
from datetime import datetime

MESSAGE_START = re.compile(r'<div class="message (?:default|service)')
DIV = re.compile(r'<(/?)div\b[^>]*>')
DATE = re.compile(r'class="pull_right date details" title="([^"]+)"')
FROM_NAME = re.compile(r'<div class="from_name">\s*(.*?)\s*</div>', re.S)
HTTP_URL = re.compile(r'href="(https?://[^"]+)"')
PIKPAK_URL = re.compile(r'https?://(?:www\.)?mypikpak\.com/s/', re.I)
ANCHOR = re.compile(r'<a\b([^>]*)>(.*?)</a>', re.S | re.I)
HASHTAG_ANCHOR = re.compile(r'onclick="return ShowHashtag\(&quot;(.*?)&quot;\)"')
ENTITIES = [('«', '«'), ('»', '»'), ('&quot;', '"'), ('&#39;', "'"),
            ('&amp;', '&'), ('&lt;', '<'), ('&gt;', '>'), ('&nbsp;', ' ')]


def strip_tags(fragment):
    text = re.sub(r'<br\s*/?>', '\n', fragment)
    text = re.sub(r'<[^>]+>', '', text)
    for src, dst in ENTITIES:
        text = text.replace(src, dst)
    return text


def message_blocks(html_text):
    marks = [m.start() for m in MESSAGE_START.finditer(html_text)] + [len(html_text)]
    return [html_text[marks[i]:marks[i + 1]] for i in range(len(marks) - 1)]


def text_div(block):
    pos = block.find('<div class="text">')
    if pos < 0:
        return None
    start = block.find('>', pos) + 1
    depth = 1
    for tag in DIV.finditer(block, start):
        if tag.group(1):
            depth -= 1
            if depth == 0:
                return block[start:tag.start()]
        else:
            depth += 1
    return block[start:]


def parse_datetime(raw):
    # "18.03.2026 11:06:34 UTC+08:00"
    return datetime.strptime(raw.split(' UTC')[0], '%d.%m.%Y %H:%M:%S')


def link_segments(raw_text):
    """Yield (url, label) for every http link, label being the trailing lines
    up to the next link so multi-link summary posts stay attributed."""
    urls = []

    def mark_link(match):
        found = HTTP_URL.search(match.group(1))
        if not found:
            return match.group(0)
        url = html.unescape(found.group(1))
        if not PIKPAK_URL.match(url):
            return match.group(0)
        marker = f'\x00LINK{len(urls)}\x00'
        urls.append(url)
        visible = strip_tags(match.group(2)).strip()
        label = '' if visible.startswith('http') else visible
        return f'\n{marker}\n{label}\n'

    marked = ANCHOR.sub(mark_link, raw_text)
    lines = [line.strip() for line in strip_tags(marked).split('\n') if line.strip()]
    if len(urls) == 1:
        label_lines = [line for line in lines if not line.startswith('http')
                       and not line.startswith('\x00LINK') and 'PikPak App' not in line]
        return [(urls[0], ' '.join(label_lines))]
    # Anchor positions identify boundaries even when the displayed text is a title.
    segments = []
    current = None
    for line in lines:
        if line.startswith('\x00LINK'):
            if current:
                segments.append(current)
            current = [urls[int(line[5:-1])], []]
        elif current is not None and 'PikPak App' not in line:
            current[1].append(line)
    if current:
        segments.append(current)
    return [(u, ' '.join(lab)) for u, lab in segments]


def load_records(export_dir):
    records = []
    for path in sorted(glob.glob(f'{export_dir}/messages*.html')):
        with open(path, encoding='utf-8') as handle:
            html_text = handle.read()
        for block in message_blocks(html_text):
            if 'message default' not in block[:60]:
                continue
            raw_text = text_div(block)
            if raw_text is None:
                continue
            date = DATE.search(block)
            msg_id = re.search(r'id="message(\d+)"', block)
            stamp = parse_datetime(date.group(1)) if date else None
            hashtags = [strip_tags(h).strip() for h in HASHTAG_ANCHOR.findall(block)]
            for url, label in link_segments(raw_text):
                records.append({
                    'file': path.split('/')[-1],
                    'message_id': int(msg_id.group(1)) if msg_id else None,
                    'stamp': stamp,
                    'url': url,
                    'label': label,
                    'hashtags': hashtags,
                    'text': strip_tags(raw_text),
                })
    return records


def canon(value):
    value = unicodedata.normalize('NFKC', value)
    value = value.replace('（', '(').replace('）', ')')
    value = re.sub(r'\s+', ' ', value).strip()
    return value


BOILERPLATE = re.compile(r'复制链接后打开\s*PikPak\s*App.*?保存当前文件')
PLUS = re.compile(r'[+＋]\s*\d+')
ANN_TAIL = re.compile(
    r'(整理|编号|校对|修正|补齐|补漏|补全|新增|更新|重编|顺位|仅编号|完结|并补齐空缺|整理编号)[\d一二三四五六七八九十]*$')
DATE_PREFIX = re.compile(r'^[\d.]+\s*[,，]?\s*')
FILE_EXT = re.compile(r'\.(mp4|mkv|avi|zip|rar|7z|pdf|epub|txt)$', re.I)
MIXED = re.compile(r'等\s*\d+\s*个文件')
SIZE = re.compile(r'(\d+(?:\.\d+)?)\s*([TG])\b', re.I)

ALIAS_LOOKUP = {}

# The vocabulary this tool clusters on is somebody's catalogue: which content
# labels exist, which studio suffixes mean "same author", what the catch-all
# buckets are called. All of it lives in vocabulary.csv (`kind,name,value`) next
# to this script, gitignored. The defaults here are an *empty* vocabulary:
# parsing, dedupe and hashtag grouping still work, only the channel-specific
# merging and tagging stand down.
NO_MATCH = re.compile(r'[^\s\S]')
HERE = os.path.dirname(os.path.abspath(__file__))
CONTENT_TAGS = []
NON_AUTHOR_TAGS = set()
RULES = {'suffix_root': NO_MATCH, 'directory_hints': NO_MATCH,
         'unnumbered': NO_MATCH, 'generic_batch': NO_MATCH}
GROUPS = {'directory_group': '大合集', 'unnumbered_group': '未归类散更',
          'unknown_group': '未识别归属'}


def load_vocabulary(path):
    """Fill the vocabulary globals from a `kind,name,value` CSV. kind is one of
    alias (name merge), tag (content label -> regex), list (|-joined set),
    rule (named regex), bucket (group display name)."""
    counts = collections.Counter()
    rows = []
    if path and os.path.exists(path):
        with open(path, encoding='utf-8-sig', newline='') as handle:
            rows = list(csv.DictReader(handle))
    for row in rows:
        kind = (row.get('kind') or '').strip()
        name = (row.get('name') or '').strip()
        value = (row.get('value') or '').strip()
        if kind not in ('alias', 'tag', 'list', 'rule', 'bucket') or not name:
            continue
        counts[kind] += 1
        if kind == 'alias':
            ALIAS_LOOKUP[root_key(name)] = root_key(value)
        elif kind == 'tag':
            CONTENT_TAGS.append((name, re.compile(value, re.I)))
        elif kind == 'list' and name == 'non_author_tags':
            NON_AUTHOR_TAGS.update(part for part in value.split('|') if part)
        elif kind == 'rule' and name in RULES:
            RULES[name] = re.compile(value, re.I)
        elif kind == 'bucket' and name in GROUPS:
            GROUPS[name] = value
    return counts



def root_key(name):
    text = re.sub(r'\([^()]*\)', '', canon(name))
    text = re.sub(r'[\s_·.]+', '', text)
    text = re.sub(r'\d{2,4}$', '', text)
    return text.casefold()



BRAND = re.compile(r'^[【\[]([^】\]]{1,14})[】\]]')
ORDINAL = re.compile(r'第[一二三四五六七八九十百\d]{1,4}[部集]$')
# a label that is really a file title or a sentence, not an author/series name
NOT_A_NAME = re.compile(r'《|】|\.mp4|\.mkv|等\s*\d+\s*个|[\u4e00-\u9fff]{15,}')
# "甲工作室、乙、丙等5个文件" -> the member series of a mixed batch
MEMBER_LIST = re.compile(r'^([^等]{2,60}?)等\s*\d+\s*[个部]?\s*(?:视频|文件|zip|压缩包)?')


def clean_label(label):
    text = canon(BOILERPLATE.sub('', label))
    text = re.sub(r'^#+', '', text)
    text = PLUS.sub(' ', text)
    text = re.sub(r' ?([()（）])', r'\1', text)
    text = re.sub(r'\s+', ' ', text).strip(' .,、:：-—_')
    return text


def series_head(label):
    """Author/studio name of a label: the 【brand】 when present, else the first
    token (everything after it is the file title or an update annotation)."""
    text = clean_label(label)
    if not text:
        return ''
    text = DATE_PREFIX.sub('', text)
    text = re.sub(r'^#+', '', text)
    text = PLUS.sub(' ', text)
    text = re.sub(r'\s+', ' ', text).strip(' .,、:：-—_')
    brand = BRAND.match(text)
    if brand:
        head = brand.group(1)
    else:
        head = re.split(r'《|【|◤|＃|#', text)[0].strip()
        tokens = head.split(' ')
        if len(tokens) > 1 and re.fullmatch(r'[A-Za-z]+', tokens[0]) \
                and re.fullmatch(r'[A-Z][a-z]{2,}', tokens[1]):
            head = ' '.join(tokens[:2])          # two-word latin names stay together
        else:
            head = tokens[0]
        head = ORDINAL.sub('', head)
        head = ANN_TAIL.sub('', head).strip() or head
    return re.sub(r'\s+', ' ', head).strip(' .,、:：-—_【(')


def cluster_root(name):
    key = root_key(name)
    key = ALIAS_LOOKUP.get(key, key)
    root = key
    while len(root) > 2:
        stripped = RULES['suffix_root'].sub('', root).strip()
        if not stripped or stripped == root:
            break
        root = stripped
    return root


def url_names(occurrences, vocabulary):
    """Author/series name for one link, resolved against the channel's own
    hashtag vocabulary so the cluster set stays finite: the message hashtag wins,
    then the member list of a mixed batch, then the leading name of the text.
    Top-level folders and dated dump batches that name no author at all go to
    their own buckets."""
    candidates = collections.Counter()
    batch_members = collections.Counter()
    for rec in occurrences:
        tags = [canon(tag.lstrip('#')) for tag in rec['hashtags'] if canon(tag.lstrip('#'))]
        tags = [t for t in tags if not RULES['unnumbered'].search(t)]
        if not tags:
            head = series_head(rec['label'])
            listed = MEMBER_LIST.match(head)
            if listed and '、' in listed.group(1):
                tags = [t.strip() for t in listed.group(1).split('、') if t.strip()]
            elif head:
                tags = [head]
        if not tags:
            continue
        candidates[tags[0]] += 1
        batch_members.update(tags[1:])
    if not candidates:
        return None, batch_members
    primary = min(candidates.items(), key=lambda kv: (-kv[1], len(kv[0])))[0]
    if primary in NON_AUTHOR_TAGS:
        batch_members[primary] += 1
        rest = [name for name, _ in candidates.most_common() if name not in NON_AUTHOR_TAGS]
        primary = rest[0] if rest else GROUPS['unknown_group']
    root = cluster_root(primary)
    latest = clean_label(occurrences[-1]['label'])
    if RULES['directory_hints'].search(latest):
        primary = GROUPS['directory_group']
    elif RULES['unnumbered'].search(primary) or RULES['generic_batch'].match(primary):
        primary = GROUPS['unnumbered_group']
    elif root not in vocabulary and NOT_A_NAME.search(primary):
        primary = GROUPS['unknown_group']
    elif root in vocabulary:
        primary = vocabulary[root]
    return primary, batch_members


def build_vocabulary(records):
    """root -> the channel's own most-used hashtag spelling for it."""
    counts = collections.Counter(
        canon(tag.lstrip('#')) for rec in records for tag in rec['hashtags']
        if canon(tag.lstrip('#')) and canon(tag.lstrip('#')) not in NON_AUTHOR_TAGS
        and not RULES['unnumbered'].search(canon(tag.lstrip('#'))))
    by_root = collections.defaultdict(list)
    for name, n in counts.items():
        by_root[cluster_root(name)].append((-n, bool(re.search(r'\d', name)), len(name), name))
    return {root: min(spellings)[3] for root, spellings in by_root.items()}


def classify(label, batch_members, primary):
    text = clean_label(label)
    blob = text + ' ' + ' '.join(batch_members)
    if primary == GROUPS['directory_group']:
        return '大合集目录'
    if primary == GROUPS['unnumbered_group'] or MIXED.search(blob) or len(batch_members) > 1:
        return '混合批次'
    if SIZE.search(text) and '文件' not in text and re.search(r'\d+(\.\d+)?\s*[TG]\b', text, re.I):
        return '大合集目录'
    if FILE_EXT.search(text):
        return '单文件'
    return '系列文件夹'


def tags_of(blob):
    return [tag for tag, pat in CONTENT_TAGS if pat.search(blob)]


def main(export_dir, out_dir, vocab_path):
    counts = load_vocabulary(vocab_path)
    summary = '  '.join(f'{kind} {counts[kind]}' for kind in
                        ('alias', 'tag', 'rule', 'bucket', 'list') if counts[kind])
    print(f'词表: {summary or "空，只按 hashtag 和格式规则聚类，不做名称合并"}'
          + (f'  <- {vocab_path}' if counts else ''))
    records = [r for r in load_records(export_dir) if 'mypikpak.com' in r['url']]
    if not records:
        raise SystemExit(f'{export_dir} 中没有找到 PikPak 分享链接（请检查 messages*.html）')
    by_url = collections.defaultdict(list)
    for rec in records:
        by_url[rec['url']].append(rec)

    vocabulary = build_vocabulary(records)
    rows = []
    for url, occ in by_url.items():
        occ.sort(key=lambda r: r['stamp'] or datetime.min)
        dated = [rec['stamp'] for rec in occ if rec['stamp'] is not None]
        primary, batch_members = url_names(occ, vocabulary)
        notes = [n for n in (clean_label(r['label']) for r in occ) if n]
        primary = primary or (clean_label(occ[0]['label']) or '(未标注)')
        blob = ' '.join(notes) + ' ' + ' '.join(batch_members)
        rows.append({
            '聚类名称': primary,
            '_root': cluster_root(primary),
            '资源名称': max(notes, key=len) if notes else primary,
            '最新标注': notes[-1] if notes else '',
            '分享链接': url,
            '分享ID': url.rstrip('/').split('/')[-1],
            '类型': classify(occ[-1]['label'], batch_members, primary),
            '内容标签': ';'.join(tags_of(blob)),
            '关联系列': ';'.join(name for name, _ in batch_members.most_common(8)),
            '出现次数': len(occ),
            '首次时间': min(dated) if dated else None,
            '末次时间': max(dated) if dated else None,
            '跨度天数': (max(dated) - min(dated)).days if dated else '',
            '消息ID': ';'.join(str(r['message_id']) for r in occ[:25]),
            '导出文件': ';'.join(sorted({r['file'] for r in occ})),
        })

    groups = collections.defaultdict(list)
    for row in rows:
        groups[row['_root']].append(row)
    ordered = sorted(groups.items(),
                     key=lambda kv: (-len(kv[1]), -sum(r['出现次数'] for r in kv[1]), kv[0][0]))
    final = []
    for cid, (root, members) in enumerate(ordered, 1):
        members.sort(key=lambda r: (r['首次时间'] or datetime.max, r['分享ID']))
        names = collections.Counter(m['聚类名称'] for m in members)
        display = vocabulary.get(root) or min(
            names, key=lambda n: (-names[n], bool(re.search(r'\d', n)), len(n)))
        for seq, row in enumerate(members, 1):
            row['聚类ID'] = f'C{cid:03d}'
            row['聚类名称'] = display
            row['聚类链接数'] = len(members)
            row['组内序号'] = f'{seq}/{len(members)}'
            final.append(row)

    os.makedirs(out_dir, exist_ok=True)
    columns = ['聚类ID', '聚类名称', '聚类链接数', '组内序号', '资源名称', '分享链接', '分享ID',
               '类型', '内容标签', '关联系列', '出现次数', '首次时间', '末次时间', '跨度天数',
               '最新标注', '消息ID', '导出文件']
    main_csv = f'{out_dir}/pikpak_links_dedup.csv'
    with open(main_csv, 'w', newline='', encoding='utf-8-sig') as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        for row in final:
            out = dict(row)
            for key in ('首次时间', '末次时间'):
                out[key] = out[key].strftime('%Y-%m-%d %H:%M') if out[key] else ''
            writer.writerow(out)

    summary_csv = f'{out_dir}/pikpak_clusters.csv'
    with open(summary_csv, 'w', newline='', encoding='utf-8-sig') as fh:
        writer = csv.writer(fh)
        writer.writerow(['聚类ID', '聚类名称', '链接数', '去重后链接数占比', '消息出现总次数',
                         '首次时间', '末次时间', '类型分布', '内容标签'])
        for cid, (root, members) in enumerate(ordered, 1):
            display = members[0]['聚类名称']
            types = collections.Counter(m['类型'] for m in members)
            tags = collections.Counter(t for m in members for t in m['内容标签'].split(';') if t)
            writer.writerow([
                f'C{cid:03d}', display, len(members), f'{len(members)/len(rows):.1%}',
                sum(m['出现次数'] for m in members),
                min((m['首次时间'] for m in members if m['首次时间']),
                    default=None).strftime('%Y-%m-%d')
                if any(m['首次时间'] for m in members) else '',
                max((m['末次时间'] for m in members if m['末次时间']),
                    default=None).strftime('%Y-%m-%d')
                if any(m['末次时间'] for m in members) else '',
                ';'.join(f'{k}x{v}' for k, v in types.most_common()),
                ';'.join(k for k, _ in tags.most_common(6)),
            ])

    print(f'链接行 {len(records)} 条 -> 去重后 {len(rows)} 个唯一链接, 聚类 {len(ordered)} 组')
    print(f'写出 {main_csv} / {summary_csv}')
    size_hist = collections.Counter(len(m) for _, m in ordered)
    print('聚类规模分布 (链接数: 组数):', dict(sorted(size_hist.items())))
    print('单链接聚类:', size_hist[1])
    print('类型分布:', collections.Counter(r['类型'] for r in rows).most_common())
    print('Top 15 聚类:')
    for cid, (root, members) in enumerate(ordered[:15], 1):
        print(f"  C{cid:03d} {members[0]['聚类名称']:<16} 链接{len(members):>2} 出现{sum(m['出现次数'] for m in members):>3}")


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('export',
                        help='Telegram HTML export folder containing messages*.html')
    parser.add_argument('--out-dir', default='.',
                        help='where the two CSVs go (default: here)')
    parser.add_argument('--vocabulary', default=os.path.join(HERE, 'vocabulary.csv'),
                        help='CSV of tags, name merges and bucket names (default: '
                             'vocabulary.csv next to this script, which is gitignored)')
    args = parser.parse_args()
    main(args.export, args.out_dir, args.vocabulary)
