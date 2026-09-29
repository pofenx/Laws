#!/usr/bin/env python3
"""Collect bounded Shandong local regulations from the provincial Justice Department website.

Source: http://sft.shandong.gov.cn/channels/ch04192/  (法律法规)
Only scrapes articles whose title matches regulation patterns (条例/办法/规定/细则).
National laws are skipped.
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
BASE = 'http://sft.shandong.gov.cn'
LIST = BASE + '/channels/ch04192/'
DOMAIN = 'sft.shandong.gov.cn'
JURISDICTION = '山东'
CATEGORY = '地方性法规'
PUBLISHER = '山东省司法厅(法律法规栏目)'
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; LegalCorpusResearch/1.0)'}

# Match only Shandong-specific regulation titles, not national laws
# National laws typically start with 中华人民共和国
_SHANDONG_RE = re.compile(r'^(山东省|山东).*?(条例|办法|规定|细则|规章)$')
_NATIONAL_RE = re.compile(r'^中华人民共和国|^国务院关于|^国家|^最高')

def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

def fetch(url, limit=3_000_000):
    if not url.startswith('http://sft.shandong.gov.cn') and not url.startswith('https://sft.shandong.gov.cn'):
        raise ValueError(f'unapproved host: {url}')
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urlopen(Request(url, headers=HEADERS), timeout=25, context=ctx) as r:
        b = r.read(limit + 1)
        if len(b) > limit:
            raise ValueError('oversized resource')
        return b

def is_shandong_regulation(title):
    """Return True if title matches a Shandong-specific regulation, not a national law."""
    if _NATIONAL_RE.search(title):
        return False
    return bool(_SHANDONG_RE.search(title))

def extract_body(raw):
    """Extract the article body text from the detail page HTML."""
    # Find the content div - it's the one with the most text
    html_text = raw.decode('utf-8', 'replace')

    # Strategy: find all <div>...</div> blocks and pick the one with the most Chinese text
    # that contains the pattern 第X条
    import re as _re
    # Find the div containing the article
    # The content is in a div with class or id that we need to find
    # From testing: the content is in a div that we found by searching for 第X条 patterns

    # Find the main content area
    # Look for a div that contains 第...条 and has substantial text
    divs = _re.findall(r'<div[^>]*>(.*?)</div>', html_text, _re.S)
    best = None
    best_len = 0
    for d in divs:
        text = _re.sub(r'<[^>]+>', '', d).strip()
        text = text.replace('\xa0', ' ')
        if len(text) > best_len and '第' in text and '条' in text:
            best = text
            best_len = len(text)

    if not best:
        raise ValueError('no article body found')

    # Clean up the text
    best = _re.sub(r'\n[ \t]*\n+', '\n\n', best).strip()
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
        # Pattern: <a href="http://sft.shandong.gov.cn/articles/ch04192/YYYYMM/uuid.shtml" title="...">...</a>
        # or: <a href="...">Title</a>
        links = re.findall(
            r'href="(http://sft\.shandong\.gov\.cn/articles/ch04192/[^"]+)"[^>]*>([^<]+)</a>',
            listing
        )

        seen = set()
        for url, raw_title in links:
            if url in seen:
                continue
            seen.add(url)
            title = raw_title.strip()

            if not is_shandong_regulation(title):
                continue

            if candidates >= max_items:
                break
            candidates += 1

            try:
                raw = fetch(url)
                body = extract_body(raw)

                if len(body) < 300:
                    raise ValueError('body too short')

                # Verify it actually contains article text
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
