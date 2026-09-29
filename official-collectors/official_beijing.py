#!/usr/bin/env python3
"""Bounded official Beijing People's Congress local-regulation collector.

Source: 北京市人民代表大会常务委员会 “地方性法规” 栏目
  - listing  GET https://www.bjrd.gov.cn/rdzl/dfxfgk/dfxfg/
      each row is a <ul> under div.table_tr.clearfix:
        li.w60 > a[title][href]  -> regulation name + relative detail link
        li.w20 (1)               -> 颁布日期 (promulgation date)
        li.w20 (2)               -> 施行日期 (effective date)
  - detail   GET <base>/<href>   e.g. .../202609/t20260913_4861613.html
      body lives in <div id="zhengwen"> (trs_web editor), plain text layer.

Body text comes from the official detail page only (no search snippets, no
third-party text). Every row defaults to validity='待核验'; a hash change on a
re-capture resets validity to 待核验. Rate-limited and bounded (max 20 items,
delay>=0.5s). Filters out 目录 / 解读 / 草案 entries; only real detail links.
"""
import argparse
import hashlib
import json
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from lxml import html

from official_gov import DDL, stamp  # shared official_* schema + UTC stamp

DB = Path(__file__).resolve().parent / 'laws.sqlite3'
UA = 'Mozilla/5.0 (compatible; LegalCorpusResearch/1.0)'
BASE = 'https://www.bjrd.gov.cn'
LIST = BASE + '/rdzl/dfxfgk/dfxfg/'
DETAIL_SUBPATH = '/rdzl/dfxfgk/dfxfg/'
DOMAIN = 'www.bjrd.gov.cn'
JURISDICTION = '北京'
CATEGORY = '地方性法规'
PUBLISHER = '北京市人民代表大会常务委员会'
MAX_ITEMS = 20


def fetch(url, limit=3_000_001):
    if not url.startswith('https://'):
        raise ValueError('not https')
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': UA}),
                                 timeout=25) as r:
        if r.status != 200:
            raise ValueError(f'HTTP {r.status}')
        return r.read(limit)


def clean_title(raw):
    """Strip zero-width/soft hyphens, collapse whitespace, trim."""
    t = raw.replace('\u200b', '').replace('\ufeff', '').replace('\u00ad', '').replace('\u200e', '')
    return re.sub(r'\s+', ' ', t).strip()


def is_regulation_title(title):
    """Keep only directly-promulgated local regulations; drop 目录/解读/草案 etc."""
    if not title:
        return False
    if re.search(r'(草案|征求意见稿|修正草案|解读|释义|答记者问|目录|清单|汇总|通知|公告|决定$|废止)', title):
        return False
    # Must look like a regulation: ends with a regulation-type suffix or a 决定 modifying one.
    return bool(re.search(r'(条例|规定|办法)$', title))


def parse_listing(raw):
    """Return list of dicts: title, url, promulgate_date, effective_date."""
    tree = html.fromstring(raw)
    uls = tree.xpath('//div[contains(@class,"table_tr")]//ul')
    rows = []
    for ul in uls:
        a = ul.xpath('./li/a')
        if not a:
            continue
        link = a[0]
        href = (link.get('href') or '').strip()
        title = clean_title(link.get('title') or link.text_content() or '')
        if not href or not title:
            continue
        url = urllib.parse.urljoin(LIST, href)
        dates = []
        for li in ul.xpath('./li'):
            txt = re.sub(r'\s+', ' ', li.text_content()).strip()
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}', txt):
                dates.append(txt)
        prom = dates[0] if len(dates) >= 1 else None
        eff = dates[1] if len(dates) >= 2 else None
        rows.append({'title': title, 'url': url, 'promulgate_date': prom, 'effective_date': eff})
    return rows


def detail_body(url):
    """Return (title_from_page, body_text) from the official detail page (text layer)."""
    raw = fetch(url).decode('utf-8', 'replace')
    tree = html.fromstring(raw)
    nodes = tree.xpath('//*[@id="zhengwen"]')
    if not nodes:
        # fallback: the trs_web editor container
        nodes = tree.xpath('//div[contains(@class,"trs_web")]')
    if not nodes:
        raise ValueError('no article body container on detail page')
    text = '\n'.join(n.text_content() for n in nodes if n is not None)
    text = re.sub(r'[ \t\u3000]+', ' ', text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    body = '\n'.join(lines)
    # Keep operative text: from the first 第X条 onward (drops any leading header).
    m = re.search(r'第[一二三四五六七八九十百千0-9]+条', body)
    if m:
        body = body[m.start():].strip()
    mtitle = re.search(r'<title[^>]*>(.*?)</title>', raw, re.S)
    page_title = clean_title(mtitle.group(1)) if mtitle else ''
    # The page <title> is "<regulation>_地方性法规_北京市人民代表大会常务委员会".
    if page_title and '_' in page_title:
        page_title = page_title.split('_')[0]
    return page_title, body


def collect(max_items=20, delay=1.0):
    con = sqlite3.connect(DB, timeout=30)
    con.execute('PRAGMA foreign_keys=ON')
    con.executescript(DDL)
    ts = stamp()
    run = con.execute('INSERT INTO official_runs(source,started_at,pages) VALUES(?,?,?)',
                     ('北京市人大常委会地方性法规栏目', ts, 1)).lastrowid
    con.commit()
    candidates = inserted = updated = skipped = errors = 0
    details = []
    try:
        rows = parse_listing(fetch(LIST).decode('utf-8', 'replace'))
        seen_url = set()
        for row in rows:
            title = row['title']
            url = row['url']
            if url in seen_url:
                continue
            seen_url.add(url)
            # Only official detail pages under the column path, on the right host.
            p = urllib.parse.urlparse(url)
            if p.hostname != DOMAIN or not p.path.startswith(DETAIL_SUBPATH) or not p.path.endswith('.html'):
                continue
            if not is_regulation_title(title):
                continue
            if candidates >= max_items:
                break
            candidates += 1
            try:
                page_title, body = detail_body(url)
                if len(body) < 300:
                    raise ValueError('official article body too short')
                final_title = clean_title(title) or clean_title(page_title)
                sha = hashlib.sha256(body.encode()).hexdigest()
                old = con.execute('SELECT id, sha256 FROM official_documents WHERE source_url=?',
                                  (url,)).fetchone()
                if old:
                    con.execute('''UPDATE official_documents SET title=?,body=?,sha256=?,
                      publisher=?,publication_date=?,last_seen_at=?,
                      validity=CASE WHEN sha256<>? THEN '待核验' ELSE validity END,
                      validity_evidence_url=CASE WHEN sha256<>? THEN NULL ELSE validity_evidence_url END,
                      validity_checked_at=CASE WHEN sha256<>? THEN NULL ELSE validity_checked_at END
                      WHERE id=?''',
                      (final_title, body, sha, PUBLISHER, row['promulgate_date'],
                       stamp(), sha, sha, sha, old[0]))
                    docid = old[0]
                    if sha != old[1]:
                        updated += 1
                else:
                    docid = con.execute('''INSERT INTO official_documents
                      (source_url,source_domain,title,jurisdiction,category,publisher,
                       publication_date,body,sha256,validity,first_seen_at,last_seen_at)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                      (url, DOMAIN, final_title, JURISDICTION, CATEGORY, PUBLISHER,
                       row['promulgate_date'], body, sha, '待核验', stamp(), stamp())).lastrowid
                    inserted += 1
                con.execute('INSERT OR IGNORE INTO official_revisions(document_id,sha256,body,captured_at) VALUES(?,?,?,?)',
                            (docid, sha, body, stamp()))
                con.commit()
            except Exception as e:
                msg = str(e)
                if 'body too short' in msg:
                    skipped += 1
                    details.append(f'SKIP {url}: {msg[:150]}')
                else:
                    errors += 1
                    details.append(f'{url}: {msg[:150]}')
            time.sleep(delay)
    except Exception as e:
        errors += 1
        details.append(f'LIST: {e}')
    finally:
        con.execute('UPDATE official_runs SET finished_at=?,candidates=?,inserted=?,updated=?,errors=?,error_details=? WHERE id=?',
                    (stamp(), candidates, inserted, updated, errors,
                     json.dumps(details, ensure_ascii=False), run))
        con.commit()
        con.close()
    result = {'source': '北京市人大常委会地方性法规栏目', 'max_items': max_items,
              'candidates': candidates, 'inserted': inserted, 'updated': updated,
              'skipped': skipped, 'errors': errors, 'details': details[:5]}
    print(json.dumps(result, ensure_ascii=False))
    if errors:
        raise SystemExit(1)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--max-items', type=int, default=20)
    p.add_argument('--delay', type=float, default=1.0)
    a = p.parse_args()
    if not 1 <= a.max_items <= MAX_ITEMS or a.delay < 0.5:
        p.error(f'max-items 1..{MAX_ITEMS}; delay >=0.5s')
    collect(a.max_items, a.delay)
