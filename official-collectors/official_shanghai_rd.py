#!/usr/bin/env python3
"""Bounded collector for the Shanghai People's Congress '法规公布' column.

Source : https://www.shrd.gov.cn/shrd/fggb/fggb.html
Scope  : Shanghai local regulations (地方性法规), amendment/abolition decisions
         (人大常委会决定) and, where present, municipal government rules (政府规章).
Body   : extracted from the official detail page HTML text layer (.article-nr).
Dedup  : idempotent via UNIQUE(source_url); cross-source dedup against the
         pre-existing Shanghai records (18 from 城管执法局) by normalized title.
         A genuine new source_url is inserted; a repeated source_url is updated
         in place (validity reset to 待核验 only if the body hash changes).
Validity: stays 待核验 by default — publishing the original text is not proof of
         current legal force.

Bounded + rate-limited: first (statically embedded) listing page only, capped at
max_items candidates, one fetch per candidate, `delay` seconds between fetches.
"""
import argparse
import hashlib
import re
import json
import sqlite3
import time
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from lxml import html

from official_gov import DDL

DB = Path(__file__).resolve().parent / 'laws.sqlite3'
BASE = 'https://www.shrd.gov.cn'
LIST = BASE + '/shrd/fggb/fggb.html'
HOST = 'www.shrd.gov.cn'
HEADERS = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36',
           'Referer': LIST, 'Accept': 'text/html,application/xhtml+xml'}
MAX_BODY = 2_000_000          # per-resource byte cap
LIST_LIMIT = 8_000_000         # listing page cap
SOURCE_NAME = '上海市人大常委会(法规公布栏目)'


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def get(url, limit=MAX_BODY):
    p = urlparse(url)
    if p.hostname != HOST or not url.startswith('https://'):
        raise ValueError('unapproved host')
    with urlopen(Request(url, headers=HEADERS), timeout=25) as r:
        b = r.read(limit + 1)
    if len(b) > limit:
        raise ValueError('oversized resource')
    return b


def norm_title(t):
    """Whitespace-insensitive normalization for cross-source title dedup."""
    return re.sub(r'\s+', '', unescape(t or ''))


def parse_listing(raw):
    """Return ordered, URL-deduped (url, title) pairs from the static listing page.

    The first page of items is embedded in <ul id="initData"> as <div class="part">
    rows. (Later pages are JS/AX-queried and intentionally not chased — bounded.)
    """
    tree = html.fromstring(raw)
    parts = tree.xpath('//ul[@id="initData"]//div[contains(@class,"part")]//a[@href]')
    if not parts:
        parts = tree.xpath('//*[contains(@class,"part")]//a[@href]')
    seen = set()
    out = []
    for a in parts:
        href = (a.get('href') or '').strip()
        url = urljoin(BASE, href)
        title = unescape((a.text or '')).strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append((url, title))
    return out


def extract_detail(raw):
    """Return (title, publication_date, body) from an official detail page."""
    tree = html.fromstring(raw)
    t = tree.xpath('//div[contains(@class,"article-title")]/text()')
    title = unescape(t[0]).strip() if t else ''
    res = tree.xpath('//div[contains(@class,"article-resouce")]/text()')
    res = ' '.join(unescape(x).strip() for x in res) if res else ''
    m = re.search(r'(\d{4}-\d{2}-\d{2})', res)
    pubdate = m.group(1) if m else None
    nodes = tree.xpath('//div[contains(@class,"article-nr")]')
    if not nodes:
        nodes = tree.xpath('//*[contains(@class,"article-nr")]')
    if not nodes:
        raise ValueError('no article body container')
    body = '\n'.join(''.join(x.itertext()) for x in nodes).strip()
    body = re.sub(r'[ \t\u3000]+', ' ', body)
    body = re.sub(r'\n[ \t]*\n+', '\n\n', body).strip()
    return title, pubdate, body


def classify(title):
    """(category, publisher) from the document title."""
    if re.search(r'人民政府令|市政府令', title):
        return '政府规章', '上海市人民政府'
    if re.search(r'(条例|规定|办法)$', title):
        return '地方性法规', '上海市人民代表大会常务委员会'
    if re.search(r'关于修改|关于废止|关于修改.{0,40}的决定$|决定$', title):
        return '人大常委会决定', '上海市人民代表大会常务委员会'
    return '地方性法规', '上海市人民代表大会常务委员会'


def collect(max_items=20, delay=1.0):
    con = sqlite3.connect(DB, timeout=30)
    con.execute('PRAGMA foreign_keys=ON')
    con.executescript(DDL)
    con.executescript('''CREATE TABLE IF NOT EXISTS official_assets (
 document_id INTEGER NOT NULL REFERENCES official_documents(id), asset_url TEXT NOT NULL,
 sha256 TEXT NOT NULL, PRIMARY KEY(document_id,asset_url));''')
    # Cross-source dedup set: normalized titles of all pre-existing Shanghai records
    # (the 18 城管执法局 docs plus any other Shanghai rows) so a law already held from
    # another source is not re-stored under this source.
    existing = {norm_title(r[0]) for r in
                con.execute("SELECT title FROM official_documents WHERE jurisdiction='上海'")}
    rid = con.execute('INSERT INTO official_runs(source,started_at,pages) VALUES(?,?,?)',
                      (SOURCE_NAME, stamp(), 1)).lastrowid
    con.commit()
    inserted = updated = errors = skipped = deduped = 0
    details = []
    samples = []
    try:
        items = parse_listing(get(LIST, LIST_LIMIT))
        candidates = 0
        for url, list_title in items:
            if candidates >= max_items:
                break
            candidates += 1
            try:
                title, pubdate, body = extract_detail(get(url))
                if not title:
                    title = list_title
                if len(body) < 300:
                    raise ValueError('body too short')
                sha = hashlib.sha256(body.encode()).hexdigest()
                category, publisher = classify(title)
                old = con.execute('SELECT id,sha256 FROM official_documents WHERE source_url=?',
                                 (url,)).fetchone()
                if old:  # idempotent re-crawl: update in place
                    did = old[0]
                    con.execute('''UPDATE official_documents SET title=?,body=?,sha256=?,publisher=?,
                     publication_date=?,last_seen_at=?,
                     validity=CASE WHEN sha256<>? THEN '待核验' ELSE validity END,
                     validity_evidence_url=CASE WHEN sha256<>? THEN NULL ELSE validity_evidence_url END,
                     validity_checked_at=CASE WHEN sha256<>? THEN NULL ELSE validity_checked_at END
                     WHERE id=?''',
                        (title, body, sha, publisher, pubdate, stamp(), sha, sha, sha, did))
                    if old[1] != sha:
                        updated += 1
                elif norm_title(title) in existing:  # same law already held from another source
                    deduped += 1
                    details.append(f'DEDUP {url}: 已有同名(跨源去重)')
                else:
                    did = con.execute('''INSERT INTO official_documents
                     (source_url,source_domain,title,jurisdiction,category,publisher,
                      publication_date,body,sha256,first_seen_at,last_seen_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                        (url, HOST, title, '上海', category, publisher, pubdate,
                         body, sha, stamp(), stamp())).lastrowid
                    inserted += 1
                con.execute('INSERT OR IGNORE INTO official_revisions(document_id,sha256,body,captured_at) VALUES(?,?,?,?)',
                            (did, sha, body, stamp()))
                con.commit()
                if url not in samples:
                    samples.append(url)
            except ValueError as e:
                if str(e) in ('no article body container', 'body too short'):
                    skipped += 1
                    details.append(f'SKIP {url}: {e}')
                else:
                    errors += 1
                    details.append(f'{url}: {str(e)[:150]}')
            except Exception as e:
                errors += 1
                details.append(f'{url}: {str(e)[:150]}')
            time.sleep(delay)
    except Exception as e:
        errors += 1
        details.append(f'LIST: {e}')
    finally:
        con.execute('UPDATE official_runs SET finished_at=?,candidates=?,inserted=?,updated=?,errors=?,error_details=? WHERE id=?',
                    (stamp(), candidates, inserted, updated, errors, json.dumps(details, ensure_ascii=False), rid))
        con.commit()
        con.close()
    result = {'source': LIST, 'candidates': candidates, 'inserted': inserted,
              'updated': updated, 'deduped': deduped, 'skipped': skipped,
              'errors': errors, 'sample_urls': samples[:8], 'details': details[:6]}
    print(json.dumps(result, ensure_ascii=False))
    if errors:
        raise SystemExit(1)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--max-items', type=int, default=20, help='bounded candidate cap (1..60)')
    p.add_argument('--delay', type=float, default=1.0, help='seconds between fetches (>=0.5)')
    a = p.parse_args()
    if not 1 <= a.max_items <= 60 or a.delay < 0.5:
        p.error('max-items 1..60; delay >=0.5s')
    collect(a.max_items, a.delay)
