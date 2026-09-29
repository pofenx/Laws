#!/usr/bin/env python3
"""每日将 legal-corpus 官方采集结果同步到 GitHub fork pofenx/Laws。

流程：导出 official_documents → 与远端 tree 逐文件对比（本地算 blob sha，零冗余上传）→
有变化才创建 tree/commit → 快进更新 master（绝不 force）。有界：单次变更文件数上限 400。
"""
import base64, hashlib, json, pathlib, re, sqlite3, sys, time, urllib.request, urllib.error

REPO = 'pofenx/Laws'
API = 'https://api.github.com'
CFG = pathlib.Path('/home/ubuntu/.hermes/config.yaml')
LC = pathlib.Path(__file__).resolve().parent
EXPORT = LC / 'export_official'
LIMIT_FILES = 400
DATA_PREFIX = 'DLC/'
COLL_PREFIX = 'official-collectors/'
NATIONAL_PREFIX = '行政法规/'
# 导出会写到的全部目录前缀（全国→行政法规/，省市→DLC/，采集器→official-collectors/）。
# existing 必须覆盖这所有前缀，否则未被跟踪的路径每次都被当作"变更"，产生空提交、非幂等。
EXPORT_PREFIXES = (NATIONAL_PREFIX, DATA_PREFIX, COLL_PREFIX)

TOKEN_PAT = re.compile(r'(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})')


def get_token():
    for m in TOKEN_PAT.findall(CFG.read_text(errors='replace')):
        req = urllib.request.Request(f'{API}/user', headers={'Authorization': f'Bearer {m}', 'User-Agent': 'legal-corpus-sync'})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                if json.load(r).get('login') == 'pofenx':
                    return m
        except Exception:
            continue
    raise SystemExit('no usable GitHub token for pofenx')


TOK = get_token()


def api(path, method='GET', body=None, timeout=60, retries=5):
    for a in range(retries):
        req = urllib.request.Request(API + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={'Authorization': f'Bearer {TOK}', 'Accept': 'application/vnd.github+json',
                                              'User-Agent': 'legal-corpus-sync'})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors='replace')[:300]
        except Exception:
            # 网络/大读取(IncompleteRead)抖动：指数退避重试，避免单次抖动中断每日任务
            if a == retries - 1:
                raise
            time.sleep(3 * (a + 1))


def blob_sha(content: bytes) -> str:
    return hashlib.sha1(b'blob %d\x00' % len(content) + content).hexdigest()


def export():
    """将 official_documents 转成 LawRefBook 目录结构：全国→行政法规/，省市→DLC/{j}地方法规/地方性法规/{j}/"""
    U = LC / 'upstream/Laws-24f29392293ce672d608e19c2bffff10401fe6a8'
    up_files = {q.name for q in U.rglob('*.md')} if U.exists() else set()
    if EXPORT.exists():
        import shutil
        shutil.rmtree(EXPORT)
    EXPORT.mkdir(parents=True)
    con = sqlite3.connect(LC / 'laws.sqlite3')
    con.row_factory = sqlite3.Row
    rows = list(con.execute('select * from official_documents order by jurisdiction, id'))
    used = set()
    from collections import Counter
    counts = Counter()
    for r in rows:
        d = dict(r)
        j = d.get('jurisdiction') or '未知'
        t = (d.get('title') or f"doc{d['id']}").strip()
        ct = re.sub(r'[（(]\d{4}[)）]\s*$', '', t).strip()
        dpub = (d.get('publication_date') or '').replace('.', '-').replace('/', '-').strip() or None
        safe = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', ct)
        while len(safe.encode('utf-8')) > 200:  # 文件名≤255字节(中文3字节/字),留日期与扩展名余量
            safe = safe[:-1]
        fn = f'{safe}({dpub}).md' if dpub else f'{safe}.md'
        td = '行政法规' if j == '全国' else f'DLC/{j}地方法规/地方性法规/{j}'
        if td.startswith('DLC') and fn in up_files:
            counts[j] += 0
            continue  # 同版已在原仓库
        body = d.get('body') or ''
        lines = body.lstrip().split('\n')
        if lines and lines[0].strip().replace(' ', '') == ct.replace(' ', ''):
            body = '\n'.join(lines[1:]).lstrip('\n')
        parts = [f'# {ct}', '']
        if dpub:
            parts.append(f'{dpub}公布'); parts.append('')
        parts += ['<!-- INFO END -->', '',
                  f"> 来源（官网原文）：{d.get('source_url', '')} ｜ 发布机构：{d.get('publisher') or '—'} ｜ "
                  f"效力状态：{d.get('validity') or '待核验'}（未经核验） ｜ SHA-256：`{d.get('sha256', '')}`", '',
                  body.strip(), '']
        rel = f'{td}/{fn}'
        (EXPORT / rel).parent.mkdir(parents=True, exist_ok=True)
        (EXPORT / rel).write_text('\n'.join(parts), encoding='utf-8')
        if rel.rsplit('/', 1)[-1] != '_index.md':
            counts[j] += 1
    # 采集器+调研
    coll = EXPORT / 'official-collectors'
    coll.mkdir(exist_ok=True)
    for s in ['sync.py', 'official_gov.py', 'official_shanghai.py', 'official_shanghai_rd.py', 'official_zhejiang.py',
              'official_beijing.py', 'official_jiangsu.py', 'official_shandong.py', 'official_hunan.py',
              'official_hubei.py', 'official_yunnan.py', 'official_yunnan_pdf.py', 'official_xinjiang.py',
              'official_guangdong.py', 'official_fujian.py',
              'legal_github_sync.py', 'status.py']:
        f = LC / s
        if f.exists():
            (coll / s).write_bytes(f.read_bytes())
    rd = LC / 'research'
    if rd.exists():
        (coll / 'research').mkdir(exist_ok=True)
        for f in rd.glob('*.json'):
            if f.name in ('candidates.json', 'east.json', 'north.json', 'southwest.json'):
                (coll / 'research' / f.name).write_bytes(f.read_bytes())
    return sum(counts.values()), dict(counts)


def readme_with_section(counts):
    dist = '、'.join(f'{k} {v}' for k, v in sorted(counts.items(), key=lambda x: -x[1]))
    total = sum(counts.values())
    section = (f'\n## 🔄 进行中的工作：官方原文持续采集与合并\n\n'
               f'> 本仓库在原项目基础上，持续从**官方来源**重新采集法律法规原文，并**直接合并进对应的法律分类目录**。'
               f'采集、验证、提交由自动化流程持续运行，本仓库将不断持续更新。\n\n'
               f'### 合并进展\n\n- 已合并 **{total} 篇**官方原文（{dist}）；另有同版重复跳过。'
               f'每篇注明来源官网 URL、发布机构、效力状态与 SHA-256；效力状态未经权威证据核验前一律标「待核验」，不标「有效/废止」。\n'
               f'- 采集器（`official-collectors/`）：有界、限速的独立脚本与 `status.py` 统计工具。\n'
               f'- 候选来源调研（`official-collectors/research/`）：全国 31 个省级行政区官方法规库入口清单，持续验证接入中。\n\n'
               f'### 说明\n\n- 合并进分类目录的官方法规文件均带来源标注，与原项目文本可按文件头区分。\n'
               f'- 数据仅供学习研究，不构成法律意见；引用请以官方公布为准。\n')
    return section


def main():
    n_docs, counts = export()
    s, base = api(f'/repos/{REPO}/commits/master')
    if s != 200:
        print(json.dumps({'status': 'error', 'stage': 'get-base', 'http': s, 'body': str(base)[:200]}))
        sys.exit(1)
    base_sha, base_tree = base['sha'], base['commit']['tree']['sha']
    s, tree = api(f'/repos/{REPO}/git/trees/{base_tree}?recursive=1')
    if s != 200:
        print(json.dumps({'status': 'error', 'stage': 'get-tree', 'http': s, 'body': str(tree)[:200]}))
        sys.exit(1)
    existing = {t['path']: t['sha'] for t in tree.get('tree', []) if t['type'] == 'blob'
                and (t['path'].startswith(EXPORT_PREFIXES) or t['path'] == 'README.md')}

    wanted = {}
    for f in EXPORT.rglob('*'):
        if f.is_file():
            rel = f.relative_to(EXPORT).as_posix()
            wanted[rel] = f.read_bytes()
    s, rd = api(f'/repos/{REPO}/contents/README.md?ref=master')
    old_readme = base64.b64decode(rd['content']).decode('utf-8') if s == 200 else ''
    marker = '\n## 🔄 进行中的工作'
    cut = old_readme.find(marker)
    head = old_readme[:cut] if cut >= 0 else old_readme.rstrip('\n')
    new_readme = head + readme_with_section(counts)

    changes, deletions = {}, []
    for path, content in wanted.items():
        sha = blob_sha(content)
        if existing.get(path) != sha:
            changes[path] = content
    if blob_sha(new_readme.encode()) != existing.get('README.md'):
        changes['README.md'] = new_readme.encode()
    # 仅对我们自有的 official-collectors/ 做删除对账；DLC/ 与行政法规/ 属原仓库内容，永不删除
    for path in existing:
        if path.startswith(COLL_PREFIX) and path not in wanted:
            deletions.append(path)

    total = len(changes) + len(deletions)
    if total == 0:
        print(json.dumps({'status': 'unchanged', 'docs': n_docs, 'tip': base_sha[:12]}, ensure_ascii=False))
        return
    if total > LIMIT_FILES:
        print(json.dumps({'status': 'aborted', 'reason': f'{total} changes exceed limit {LIMIT_FILES}'}, ensure_ascii=False))
        sys.exit(1)

    entries = []
    for path, content in sorted(changes.items()):
        s, b = api(f'/repos/{REPO}/git/blobs', 'POST', {'content': base64.b64encode(content).decode(), 'encoding': 'base64'})
        if s != 201:
            print(json.dumps({'status': 'error', 'stage': 'blob', 'path': path, 'http': s, 'body': str(b)[:200]}, ensure_ascii=False))
            sys.exit(1)
        entries.append({'path': path, 'mode': '100644', 'type': 'blob', 'sha': b['sha']})
    for path in deletions:
        entries.append({'path': path, 'mode': '100644', 'type': 'blob', 'sha': None})

    s, t = api(f'/repos/{REPO}/git/trees', 'POST', {'base_tree': base_tree, 'tree': entries})
    if s != 201:
        print(json.dumps({'status': 'error', 'stage': 'tree', 'http': s, 'body': str(t)[:200]}))
        sys.exit(1)
    msg = (f"官方原文自动同步: {n_docs}篇({len(changes)}更新/新增, {len(deletions)}删除); "
           f"效力均待核验; 来源与哈希见 official-data/index.json")
    s, cm = api(f'/repos/{REPO}/git/commits', 'POST',
                {'message': msg, 'tree': t['sha'], 'parents': [base_sha],
                 'author': {'name': 'pofenx', 'email': 'pofenx@users.noreply.github.com'}})
    if s != 201:
        print(json.dumps({'status': 'error', 'stage': 'commit', 'http': s, 'body': str(cm)[:200]}))
        sys.exit(1)
    s, rf = api(f'/repos/{REPO}/git/refs/heads/master', 'PATCH', {'sha': cm['sha'], 'force': False})
    if s != 200:
        print(json.dumps({'status': 'error', 'stage': 'ref', 'http': s, 'body': str(rf)[:200]}))
        sys.exit(1)
    print(json.dumps({'status': 'pushed', 'docs': n_docs, 'changed': len(changes), 'deleted': len(deletions),
                      'commit': cm['sha'][:12]}, ensure_ascii=False))


if __name__ == '__main__':
    main()
