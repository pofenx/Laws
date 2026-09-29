#!/usr/bin/env python3
"""Collect bounded Hunan local regulations from the provincial Justice Department website.

Source: http://sft.hunan.gov.cn/sft/xxgk_71079/zcfg/dfxfg/index.html  (地方性法规规章)
Only scrapes articles whose title matches regulation patterns.
"""
import argparse
import hashlib
import json
import re
import sqlite3
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from official_gov import DDL

DB = Path(__file__).resolve().parent / 'laws.sqlite3'
BASE = 'http://sft.hunan.gov.cn'
LIST = BASE + '/sft/xxgk_71079/zcfg/dfxfg/index.html'
DOMAIN = 'sft.hunan.gov.cn'
JURISDICTION = '湖南'
CATEGORY = '地方性法规'
PUBLISHER = '湖南省司法厅(地方性法规规章栏目)'
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; LegalCorpusResearch/1.0)'}

_REG_RE = re.compile(r'(条例|办法|规定|细则|规章)$')
_NATIONAL_RE = re.compile(r'^中华人民共和国|^国务院关于|^国家|^最高')

def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

def fetch(url, limit=3_000_000):
    if not url.startswith('http://sft.hunan.gov.cn') and not url.startswith('https://sft.hunan.gov.cn'):
        raise ValueError(f'unapproved host: {url}')
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urlopen(Request(url, headers=HEADERS), timeout=25, context=ctx) as r:
        b = r.read(limit + 1)
        if len(b) > limit:
            raise ValueError('oversized resource')
        return b

def is_hunan_regulation(title):
    """Return True if title matches a Hunan regulation, not a national law or government document."""
    if _NATIONAL_RE.search(title):
        return False
    if not _REG_RE.search(title):
        return False
    # Exclude government notices and non-regulation documents
    if re.search(r'(通知|函|批复|意见|方案|计划|报告|纪要|通报|公告|决定|命令|令$|公告$|关于.*的复函)', title):
        return False
    return True

def extract_body(raw):
    """Extract the article body text from the detail page HTML."""
    html_text = raw.decode('utf-8', 'replace')

    # Find the content div - the one with the most text containing 第X条
    divs = re.findall(r'<div[^>]*>(.*?)</div>', html_text, re.S)
    best = None
    best_len = 0
    for d in divs:
        text = re.sub(r'<[^>]+>', '', d).strip()
        text = text.replace('\xa0', ' ')
        if len(text) > best_len and '第' in text and '条' in text:
            best = text
            best_len = len(text)

    if not best:
        raise ValueError('no article body found')

    best = re.sub(r'\n[ \t]*\n+', '\n\n', best).strip()
    best = best.replace('\xa0', ' ')
    return best

def collect(max_items=20, delay=1.0):
    con = sqlite3.connect(DB, timeout=30)
    con.execute('PRAGMA foreign_keys=ON')
    con.executescript(DDL)
    rid = con.execute(
        'INSERT INTO official_runs(source,started_at,pages) VALUES(?,?,?)',
        (PUBLISHER, stamp(), 1)
    ).lastrowid
    con.commit()
    inserted = updated = errors = skipped = 0
    candidates = 0
    details = []
    try:
        listing_raw = fetch(LIST)
        listing = listing_raw.decode('utf-8', 'replace')

        # Find all article links in the listing
        # Pattern: <a href="/sft/xxgk_71079/zcfg/dfxfg/YYYYMM/tYYYYMMDD_XXXXXX.html" title="...">
        links = re.findall(
            r'href="(/sft/xxgk_71079/zcfg/dfxfg/\d{6}/t\d{8}_\d+\.html)"[^>]*>([^<]+)</a>',
            listing
        )

        seen = set()
        for path, raw_title in links:
            url = BASE + path
            if url in seen:
                continue
            seen.add(url)
            title = raw_title.strip()

            if not is_hunan_regulation(title):
                continue

            if candidates >= max_items:
                break
            candidates += 1

            try:
                raw = fetch(url)
                body = extract_body(raw)

                if len(body) < 300:
                    raise ValueError('body too short')

                if not re.search(r'第[0-9一二三四五六七八九十百千零两]+条', body):
                    raise ValueError('no article text pattern found')

                sha = hashlib.sha256(body.encode()).hexdigest()
                old = con.execute(
                    'SELECT id,sha256 FROM official_documents WHERE source_url=?', (url,)
                ).fetchone()

                if old:
                    did = old[0]
                    con.execute('''
                        UPDATE official_documents SET title=?,body=?,sha256=?,last_seen_at=?,
                        validity=CASE WHEN sha256<>? THEN '待核验' ELSE validity END,
                        validity_evidence_url=CASE WHEN sha256<>? THEN NULL ELSE validity_evidence_url END,
                        validity_checked_at=CASE WHEN sha256<>? THEN NULL ELSE validity_checked_at END
                        WHERE id=?
                    ''', (title, body, sha, stamp(), sha, sha, sha, did))
                    if old[1] != sha:
                        updated += 1
                else:
                    did = con.execute('''
                        INSERT INTO official_documents
                          (source_url,source_domain,title,jurisdiction,category,publisher,
                           body,sha256,first_seen_at,last_seen_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?)
                    ''', (url, DOMAIN, title, JURISDICTION, CATEGORY, PUBLISHER,
                            body, sha, stamp(), stamp())).lastrowid
                    inserted += 1

                con.execute(
                    'INSERT OR IGNORE INTO official_revisions(document_id,sha256,body,captured_at) VALUES(?,?,?,?)',
                    (did, sha, body, stamp())
                )
                con.commit()
            except ValueError as e:
                if str(e) in ('body too short', 'no article text pattern found', 'no article body found'):
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
        con.execute(
            'UPDATE official_runs SET finished_at=?,candidates=?,inserted=?,updated=?,errors=?,error_details=? WHERE id=?',
            (stamp(), candidates, inserted, updated, errors,
             json.dumps(details, ensure_ascii=False), rid)
        )
        con.commit()
        con.close()

    result = {
        'source': LIST,
        'candidates': candidates,
        'inserted': inserted,
        'updated': updated,
        'skipped': skipped,
        'errors': errors,
        'details': details[:5]
    }
    print(json.dumps(result, ensure_ascii=False))
    if errors:
        raise SystemExit(1)

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--max-items', type=int, default=20)
    p.add_argument('--delay', type=float, default=1.0)
    a = p.parse_args()
    if a.max_items < 1 or a.max_items > 50:
        p.error('max-items 1..50')
    if a.delay < 0.5:
        p.error('delay >= 0.5s')
    collect(a.max_items, a.delay)
