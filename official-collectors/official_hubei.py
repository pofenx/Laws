#!/usr/bin/env python3
"""Bounded official Hubei local-regulations collector.

Source: 湖北省人大「湖北法规库」 http://119.36.213.154:8088/fgk/
  - listing  GET /fgk/RulesList.jsp?ExtendNum1=<cat>&Effective=1&nowPage=<N>
        ExtendNum1=1 -> 地方性法规·法规性决定 (省本级 + 各地市 + 法规性决定)
        ExtendNum1=2 -> 自治条例·单行条例
        one <tr> per row: 序号 / 标题(a->index_xq.jsp?Rileid=N) / 公布日期 / 通过日期 / 施行日期 / 时效性
        每页 20 条；页脚 "第 x/24 页 共 470 条"；分页 nowPage=N。
  - detail   GET /fgk/index_xq.jsp?Rileid=<N>   (HTTP 200, 正文约数千字符)
        正文位于  <div class="con"><pre>...</pre></div>（纯文本层，UTF-8）。
        元信息（公布机关/通过/施行/公布/时效性）在 <table class="tab"> 中。

范围：湖北省地方性法规（含各地市与法规性决定）。该法规库**无独立"政府规章"栏目**
（菜单仅有 全部法规·法规性决定 / 地方性法规·法规性决定 / 自治条例·单行条例），故本采集器
以「地方性法规」为主源；自治条例·单行条例可用 --cat zizhi 选择。

只采官网详情页 <pre> 原文正文（不用检索摘要、不采解读/草案），每条默认 validity='待核验'：
官网列表的"时效性"码不直接映射为 有效/已废止，需另核公布/修改/废止依据才改判。
限速且有界：pages<=20、每页<=50 条、间隔>=1s（IP 站点响应慢，fetch 用长超时 + 重试）；
正文过短或无"第X条"的条目跳过并记录。哈希(SHA-256)去重；重采正文变化才重置效力为待核验。
"""
import argparse
import hashlib
import html
import json
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path

from official_gov import DDL, stamp  # shared official_documents/official_revisions/official_runs schema + UTC stamp

DB = Path(__file__).resolve().parent / 'laws.sqlite3'
HOST = '119.36.213.154:8088'
HOSTNAME = '119.36.213.154'  # urllib.hostname strips the port; compare against this
BASE = f'http://{HOST}/fgk'
LIST = BASE + '/RulesList.jsp'
DETAIL = BASE + '/index_xq.jsp'
DOMAIN = HOST
JURISDICTION = '湖北'
# 时效性码不直接映射；统一默认待核验。
PUBLISHER = '湖北省人大常委会(湖北法规库)'
UA = 'Mozilla/5.0 (compatible; LegalCorpusResearch/1.0)'
REFERER = BASE + '/index.jsp'
TIMEOUT = 30          # IP 站点响应慢：长超时
RETRIES = 3
MIN_BODY = 300         # 正文字符下限
MAX_ITEMS_PER_PAGE = 50  # 站点每页 20 条，天然远小于此上限
MAX_PAGES = 20
_DELAY_MIN = 1.0       # 间隔>=1s（比其它采集器更保守，因 IP 站点慢）

# 栏目选择：地方性法规·法规性决定 / 自治条例·单行条例
CATS = {
    'dfxfg': (1, '地方性法规'),
    'zizhi': (2, '自治条例·单行条例'),
}

_ARTICLE = re.compile(r'第[0-9一二三四五六七八九十百千零两]+条')


def fetch(url, limit=3_000_001):
    """GET with long timeout + bounded retries (IP site is slow / flaky)."""
    if not url.startswith('http://'):
        raise ValueError(f'unexpected scheme: {url}')
    p = urllib.parse.urlparse(url)
    if p.hostname != HOSTNAME:
        raise ValueError(f'unapproved host: {p.hostname}')
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Referer': REFERER})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                if r.status != 200:
                    raise ValueError(f'HTTP {r.status}')
                return r.read(limit).decode('utf-8', 'replace')
        except Exception as e:  # noqa: BLE001 - retry transient network/timeout errors
            last = e
            if attempt < RETRIES:
                time.sleep(1.5 * attempt)
    raise ValueError(f'fetch failed after {RETRIES} tries: {type(last).__name__}: {str(last)[:120]}')


def listing_url(cat, page):
    num1 = CATS[cat][0]
    return f'{LIST}?ExtendNum1={num1}&Effective=1&nowPage={page}'


def parse_listing(raw):
    """Return [{'rileid','url','title','pub','pass','effect','validity'}, ...] in DOM order."""
    rows = []
    for tr in re.findall(r'<tr[^>]*>.*?</tr>', raw, re.S):
        a = re.search(r'index_xq\.jsp\?Rileid=(\d+)"[^>]*>(.*?)</a>', tr, re.S)
        if not a:
            continue
        rileid = a.group(1)
        title = re.sub(r'<[^>]+>', '', a.group(2))
        title = html.unescape(re.sub(r'\s+', ' ', title)).strip()
        if not title:
            continue
        dates = re.findall(r'>(\d{4}-\d{2}-\d{2})', tr)
        eff = re.search(r'lbtd">([^<]*?(?:有效|无效|废止|已失效|失效)[^<]*?)<', tr)
        rows.append({
            'rileid': rileid,
            'url': f'{DETAIL}?Rileid={rileid}',
            'title': title,
            'pub': dates[0] if len(dates) > 0 else None,
            'pass': dates[1] if len(dates) > 1 else None,
            'effect': dates[2] if len(dates) > 2 else None,
            'validity': eff.group(1).strip() if eff else None,
        })
    # Enforce per-page bound (site yields ~20; keep defensively).
    return rows[:MAX_ITEMS_PER_PAGE]


def detail_body(raw):
    """Return (page_title, body_text) from the official detail page (the <pre> text layer)."""
    m = re.search(r'<pre>(.*?)</pre>', raw, re.S)
    title = ''
    tm = re.search(r'<h2[^>]*>(.*?)</h2>', raw, re.S)
    if tm:
        title = html.unescape(re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', tm.group(1)))).strip()
    if not m:
        return title, ''
    t = m.group(1)
    t = html.unescape(t)
    t = t.replace('\r', '')
    lines = [re.sub(r'[ \t\u3000\xa0]+', ' ', ln).strip() for ln in t.splitlines()]
    lines = [ln for ln in lines if ln]
    return title, '\n'.join(lines).strip()


def collect(pages=2, delay=1.0, max_items=20, cat='dfxfg'):
    con = sqlite3.connect(DB, timeout=30)
    con.execute('PRAGMA foreign_keys=ON')
    con.executescript(DDL)
    num1, cat_name = CATS[cat]
    ts = stamp()
    run = con.execute('INSERT INTO official_runs(source,started_at,pages) VALUES(?,?,?)',
                      (f'湖北法规库·{cat_name}', ts, pages)).lastrowid
    con.commit()
    candidates = inserted = updated = skipped = errors = 0
    details = []
    seen = set()
    try:
        for page in range(1, pages + 1):
            if candidates >= max_items:
                break
            raw = fetch(listing_url(cat, page))
            rows = parse_listing(raw)
            if not rows:
                # Could be a beyond-range page (nowPage past 末页); treat as end-of-listing, not an error.
                break
            for row in rows:
                if candidates >= max_items:
                    break
                url = row['url']
                if url in seen or not row['title']:
                    continue
                seen.add(url)
                # Only official detail pages on the right host, in the fgk path.
                p = urllib.parse.urlparse(url)
                if p.hostname != HOSTNAME or not p.path.startswith('/fgk/') or 'Rileid=' not in p.query:
                    errors += 1
                    details.append(f'unexpected detail URL: {url}')
                    continue
                candidates += 1
                try:
                    d = fetch(url)
                    dt, body = detail_body(d)
                    if len(body) < MIN_BODY or not _ARTICLE.search(body):
                        raise ValueError('official article body missing/short')
                    sha = hashlib.sha256(body.encode()).hexdigest()
                    title = (row['title'] or dt or '').strip()
                    # 官网列表"时效性"码不直接映射；默认待核验。
                    old = con.execute('SELECT id, sha256 FROM official_documents WHERE source_url=?',
                                      (url,)).fetchone()
                    if old:
                        con.execute('''UPDATE official_documents SET title=?,body=?,sha256=?,
                          publisher=?,publication_date=?,last_seen_at=?,
                          validity=CASE WHEN sha256<>? THEN '待核验' ELSE validity END,
                          validity_evidence_url=CASE WHEN sha256<>? THEN NULL ELSE validity_evidence_url END,
                          validity_checked_at=CASE WHEN sha256<>? THEN NULL ELSE validity_checked_at END
                          WHERE id=?''',
                          (title, body, sha, PUBLISHER, row['pub'],
                           stamp(), sha, sha, sha, old[0]))
                        docid = old[0]
                        if sha != old[1]:
                            updated += 1
                    else:
                        docid = con.execute('''INSERT INTO official_documents
                          (source_url,source_domain,title,jurisdiction,category,publisher,
                           publication_date,body,sha256,validity,first_seen_at,last_seen_at)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                          (url, DOMAIN, title, JURISDICTION, cat_name, PUBLISHER,
                           row['pub'], body, sha, '待核验', stamp(), stamp())).lastrowid
                        inserted += 1
                    con.execute('INSERT OR IGNORE INTO official_revisions(document_id,sha256,body,captured_at) VALUES(?,?,?,?)',
                                (docid, sha, body, stamp()))
                    con.commit()
                except Exception as e:  # noqa: BLE001
                    msg = str(e)
                    if 'missing/short' in msg:
                        skipped += 1
                        details.append(f'SKIP {url}: {msg[:150]}')
                    else:
                        errors += 1
                        details.append(f'{url}: {msg[:150]}')
                time.sleep(delay)
            if page < pages:
                time.sleep(delay)
    except Exception as e:  # noqa: BLE001
        errors += 1
        details.append(f'LIST: {type(e).__name__}: {str(e)[:200]}')
    finally:
        con.execute('UPDATE official_runs SET finished_at=?,candidates=?,inserted=?,updated=?,errors=?,error_details=? WHERE id=?',
                    (stamp(), candidates, inserted, updated, errors, json.dumps(details, ensure_ascii=False), run))
        con.commit()
        con.close()
    result = {'source': f'湖北法规库·{cat_name}', 'cat': cat, 'pages': pages,
              'candidates': candidates, 'inserted': inserted, 'updated': updated,
              'skipped': skipped, 'errors': errors, 'details': details[:5]}
    print(json.dumps(result, ensure_ascii=False))
    if errors:
        raise SystemExit(1)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--pages', type=int, default=2, help='listing pages to walk (1..20)')
    p.add_argument('--max-items', type=int, default=20, help='total items cap (1..50)')
    p.add_argument('--delay', type=float, default=_DELAY_MIN, help='seconds between requests (>=1.0)')
    p.add_argument('--cat', choices=sorted(CATS), default='dfxfg',
                   help='dfxfg=地方性法规(默认); zizhi=自治条例·单行条例')
    a = p.parse_args()
    if not 1 <= a.pages <= MAX_PAGES or not 1 <= a.max_items <= 50 or a.delay < _DELAY_MIN:
        p.error(f'pages 1..{MAX_PAGES}; max-items 1..50; delay >= {_DELAY_MIN}s')
    collect(a.pages, a.delay, a.max_items, a.cat)
