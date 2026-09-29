#!/usr/bin/env python3
"""Bounded official Zhejiang regulations collector (省人大 法规规章规范性文件库).

Source: 浙江省人大常委会 浙江省法规规章规范性文件库 (https://zcfg.zjrd.gov.cn/)
  - listing POST https://zhengce.zj.gov.cn/policyweb/httpservice/getPolicy.do
      (catalogid=10003 = 地方性法规; rows carry iid/title/organization/filenumber/pubtime)
  - detail  GET https://zhengce.zj.gov.cn/policyweb/httpservice/showinfo.do?infoid=<iid>
      (returns the regulation HTML body, plain text layer)

Body text comes from the official detail page only (no search snippets, no
third-party text). Every row defaults to validity='待核验'; a hash change on a
re-capture resets validity to 待核验. Rate-limited and bounded (pages<=20,
items<=50, delay>=0.5s).
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
from datetime import datetime, timezone
from pathlib import Path

DB = Path(__file__).resolve().parent / 'laws.sqlite3'
UA = 'Mozilla/5.0 (compatible; LegalCorpusResearch/1.0)'
LIST_URL = 'https://zhengce.zj.gov.cn/policyweb/httpservice/getPolicy.do'
DETAIL_URL = 'https://zhengce.zj.gov.cn/policyweb/httpservice/showinfo.do'
LIST_REFFER = 'https://zcfg.zjrd.gov.cn/list.htm?catalogid=10003'
DOMAIN = 'zcfg.zjrd.gov.cn'          # 省人大法规库入口(对外来源)
CATALOG_FG = '10003'                 # 地方性法规
JURISDICTION = '浙江'
CATEGORY = '地方性法规'
PUBLISHER = '浙江省人大常委会(浙江省法规规章规范性文件库)'

# Reuse the official_documents / official_revisions / official_runs schema.
from official_gov import DDL, stamp  # noqa: E402


def fetch(url, referer=None, post=None):
    headers = {'User-Agent': UA}
    if referer:
        headers['Referer'] = referer
    data = None
    if post is not None:
        data = urllib.parse.urlencode(post).encode('utf-8')
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers, data=data),
                                timeout=25) as r:
        if r.status != 200:
            raise ValueError(f'HTTP {r.status}')
        return r.read(3_000_001)


def listing(page_index, page_size=10):
    post = {'title': '', 'catalogid': CATALOG_FG,
            'pageIndex': page_index, 'pageSize': page_size}
    d = json.loads(fetch(LIST_URL, referer=LIST_REFFER, post=post).decode('utf-8'))
    params = d.get('params') or {}
    pl = params.get('policyList') or params
    if not pl.get('data'):
        raise ValueError(f'listing returned no rows: {json.dumps(d, ensure_ascii=False)[:160]}')
    return pl['data']


def detail_body(iid):
    """Return (title, body_text) from the official detail HTML (text layer only)."""
    raw = fetch(f'{DETAIL_URL}?infoid={iid}', referer=LIST_REFFER).decode('utf-8', 'replace')
    m = re.search(r'<title[^>]*>(.*?)</title>', raw, re.S)
    title = html.unescape(m.group(1)).strip() if m else ''
    t = re.sub(r'<script.*?</script>', '', raw, flags=re.S)
    t = re.sub(r'<style.*?</style>', '', t, flags=re.S)
    t = re.sub(r'<[^>]+>', '\n', t)
    t = t.replace('&nbsp;', ' ').replace('&amp;', '&')
    lines = [ln.strip() for ln in re.sub(r'\n\s*\n+', '\n', html.unescape(t)).splitlines() if ln.strip()]
    body = '\n'.join(lines)
    # Trim boilerplate: keep from the first 第X条 / 第X章 to the end of the operative text.
    start = body.find('第一条')
    if start < 0:
        start = body.find('第1条')
    if start < 0:
        start = body.find('第一条 ')
    tail = body.find('打印')
    if tail < 0:
        tail = body.find('关闭')
    if tail < 0:
        tail = len(body)
    body = body[start:tail].strip() if start >= 0 else body.strip()
    return title, body


def pub_date(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime('%Y-%m-%d')
    except (TypeError, ValueError):
        return None


def collect(pages=2, page_size=10, delay=1.0):
    con = sqlite3.connect(DB, timeout=30)
    con.execute('PRAGMA foreign_keys=ON')
    con.executescript(DDL)
    ts = stamp()
    run = con.execute('INSERT INTO official_runs(source,started_at,pages) VALUES(?,?,?)',
                      ('浙江省法规规章规范性文件库(省人大)', ts, pages)).lastrowid
    con.commit()
    candidates = inserted = updated = errors = 0
    details = []
    try:
        seen_iid = set()
        for page in range(1, pages + 1):
            rows = listing(page, page_size)
            for row in rows:
                iid = (row.get('iid') or '').strip()
                title = html.unescape(re.sub('<[^>]*>', '', row.get('title', ''))).strip()
                if not iid or not title:
                    continue
                if iid in seen_iid:
                    continue
                seen_iid.add(iid)
                src_url = f'https://{DOMAIN}/detail.htm?infoid={iid}'  # traceable official entry
                try:
                    dt, body = detail_body(iid)
                    if len(body) < 300:
                        raise ValueError('official article body too short')
                    sha = hashlib.sha256(body.encode()).hexdigest()
                    old = con.execute('SELECT id, sha256 FROM official_documents WHERE source_url=?',
                                     (src_url,)).fetchone()
                    pub = pub_date(row.get('pubtime'))
                    if old:
                        con.execute('''UPDATE official_documents SET title=?,body=?,sha256=?,
                          publisher=?,publication_date=?,document_number=?,last_seen_at=?,
                          validity=CASE WHEN sha256<>? THEN '待核验' ELSE validity END,
                          validity_evidence_url=CASE WHEN sha256<>? THEN NULL ELSE validity_evidence_url END,
                          validity_checked_at=CASE WHEN sha256<>? THEN NULL ELSE validity_checked_at END
                          WHERE id=?''',
                          (dt or title, body, sha, PUBLISHER, pub, row.get('filenumber'),
                           stamp(), sha, sha, sha, old[0]))
                        docid = old[0]
                        if sha != old[1]:
                            updated += 1
                    else:
                        docid = con.execute('''INSERT INTO official_documents
                          (source_url,source_domain,title,jurisdiction,category,publisher,
                           publication_date,document_number,body,sha256,validity,first_seen_at,last_seen_at)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                          (src_url, DOMAIN, dt or title, JURISDICTION, CATEGORY, PUBLISHER,
                           pub, row.get('filenumber'), body, sha, '待核验', stamp(), stamp())).lastrowid
                        inserted += 1
                    con.execute('INSERT OR IGNORE INTO official_revisions(document_id,sha256,body,captured_at) VALUES(?,?,?,?)',
                                (docid, sha, body, stamp()))
                    con.commit()
                    candidates += 1
                except Exception as e:
                    errors += 1
                    details.append(f'{src_url}: {str(e)[:160]}')
                time.sleep(delay)
            if page < pages:
                time.sleep(delay)
    except Exception as e:
        errors += 1
        details.append(f'LIST: {type(e).__name__}: {str(e)[:200]}')
    finally:
        con.execute('UPDATE official_runs SET finished_at=?,candidates=?,inserted=?,updated=?,errors=?,error_details=? WHERE id=?',
                    (stamp(), candidates, inserted, updated, errors, json.dumps(details, ensure_ascii=False), run))
        con.commit()
        con.close()
    result = {'source': '浙江省法规规章规范性文件库(省人大)', 'pages': pages, 'candidates': candidates,
              'inserted': inserted, 'updated': updated, 'errors': errors, 'details': details[:5]}
    print(json.dumps(result, ensure_ascii=False))
    if errors:
        raise SystemExit(1)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--pages', type=int, default=1)
    p.add_argument('--page-size', type=int, default=10)
    p.add_argument('--delay', type=float, default=1.0)
    a = p.parse_args()
    if not 1 <= a.pages <= 20 or not 1 <= a.page_size <= 50 or a.delay < 0.5:
        p.error('pages 1..20; page-size 1..50; delay >=0.5s')
    collect(a.pages, a.page_size, a.delay)
