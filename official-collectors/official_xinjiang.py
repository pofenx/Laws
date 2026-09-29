#!/usr/bin/env python3
"""Bounded official Xinjiang regulations collector.

Source: 新疆维吾尔自治区法规规章规范性文件库 (http://220.171.42.55:4965/home)
  The site is a Vue SPA (xinjiang-public-address) fronting a JSON REST API at
  /prod-api. Endpoints (no auth, plain HTTP):
    - POST /prod-api/extranet/regulation/search      (JSON body, filetypeId filter)
    - GET  /prod-api/extranet/config/filetype         (category tree -> ids/counts)
    - GET  /prod-api/gx-attachment/attachment/obtain/download?id=<attId>&module=gx-regulationdb-intranet
        -> raw document bytes (docx/doc/pdf)
    - GET  /prod-api/gx-attachment/attachment/obtain/preview-word2pdf/<attId>?module=...
        -> server-side Word->PDF (text layer readable by pymupdf); used for legacy .doc

Body text comes ONLY from the official attached document (Word/PDF), extracted
locally. No search snippets, no third-party text. Every row is stored with
validity='待核验' (we do not assert validity); a hash change on re-capture
resets validity to 待核验. Bounded: page-size<=200, delay>=0.5s, total record
cap via --max-records.
"""
import argparse
import hashlib
import html
import io
import json
import re
import sqlite3
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import pymupdf

DB = Path(__file__).resolve().parent / 'laws.sqlite3'
BASE = 'http://220.171.42.55:4965/prod-api'
ENTRY = 'http://220.171.42.55:4965'
DOMAIN = '220.171.42.55:4965'
MODULE = 'gx-regulationdb-intranet'
JURISDICTION = '新疆'
PUBLISHER = '新疆维吾尔自治区法规规章规范性文件库(自治区人大/政府)'

# Category ids from /extranet/config/filetype (authoritative).
CATS = {
    'fg':   '3759a86ae9f96357b875238b5d2dfeb2',   # 自治区地方性法规 (294)
    'auto': 'f42d2052225d6a01c64b3587d9a90d30',   # 自治区自治条例、单行条例 (0)
    'gz':   'b36315f2832c2e1cb169e0195bd44461',   # 政府规章 parent (267: 自治区143+州市124)
}

# Reuse the shared official_documents / official_revisions / official_runs schema.
from official_gov import DDL, stamp  # noqa: E402

MIN_BODY = 200          # chars; real regulations are far longer
PAGENO = re.compile(r'^\s*[—\-–]?\s*\d{1,4}\s*[—\-–]?\s*$')


def _req(url, post=None, headers=None):
    h = {'User-Agent': 'Mozilla/5.0 (compatible; LegalCorpusResearch/1.0)',
         'Referer': ENTRY + '/home'}
    if headers:
        h.update(headers)
    data = None
    if post is not None:
        data = json.dumps(post).encode('utf-8')
        h['Content-Type'] = 'application/json'
    with urllib.request.urlopen(urllib.request.Request(url, headers=h, data=data),
                                timeout=60) as r:
        if r.status != 200:
            raise ValueError(f'HTTP {r.status}')
        return r.read(30_000_001)


def search_page(filetype_id, page_num, page_size):
    body = {'pageNum': page_num, 'pageSize': page_size, 'total': 0,
            'searchField': 'title', 'searchValue': '', 'searchMode': 'like',
            'orderByColumn': 'newPassDate', 'filetypeId': filetype_id,
            'timeliness': None, 'formulateMode': None, 'fileLevel': None,
            'officeId': None, 'passYear': None}
    d = json.loads(_req(BASE + '/extranet/regulation/search', post=body).decode('utf-8'))
    if d.get('code') != 200:
        raise ValueError(f'search error: code={d.get("code")} msg={d.get("msg")}')
    data = d.get('data') or {}
    return data.get('rows') or [], int(data.get('total') or 0)


def _clean(text):
    text = text.replace('\u00a0', ' ')
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text)
    lines = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or PAGENO.match(ln):
            continue
        lines.append(ln)
    return '\n'.join(lines).strip()


def _docx_text(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read('word/document.xml').decode('utf-8')
    xml = xml.replace('</w:p>', '\n')
    return _clean(xml)


def _pdf_text(data):
    doc = pymupdf.open(stream=data)
    return _clean(''.join(p.get_text() for p in doc))


def _word2pdf_text(att_id):
    raw = _req(f'{BASE}/gx-attachment/attachment/obtain/preview-word2pdf/{att_id}?module={MODULE}')
    if not raw.startswith(b'%PDF'):
        raise ValueError('word2pdf did not return a PDF')
    return _pdf_text(raw)


def extract_body(files):
    """Pick the most authoritative attached file and return its text.

    Preference: docx (direct), then doc (server word->pdf), then pdf (direct).
    fileClass 4 = body, 9 = repeal/annex; prefer 4.
    """
    if not files:
        raise ValueError('no attached document')
    ordered = sorted(files, key=lambda f: ({'4': 0, '9': 1}.get(f.get('fileClass'), 2)))
    by_type = {}
    for f in ordered:
        by_type.setdefault(f.get('attType'), f)
    if 'docx' in by_type:
        raw = _req(f'{BASE}/gx-attachment/attachment/obtain/download?id={by_type["docx"]["attId"]}&module={MODULE}')
        return _docx_text(raw)
    if 'doc' in by_type:
        return _word2pdf_text(by_type['doc']['attId'])
    if 'pdf' in by_type:
        raw = _req(f'{BASE}/gx-attachment/attachment/obtain/download?id={by_type["pdf"]["attId"]}&module={MODULE}')
        if not raw.startswith(b'%PDF'):
            raise ValueError('pdf download was not a PDF')
        return _pdf_text(raw)
    raise ValueError(f'unreadable attachment types: {sorted(f.get("attType") for f in files)}')


def collect(categories=('fg', 'gz'), max_records=30, page_size=100, delay=1.0):
    con = sqlite3.connect(DB, timeout=30)
    con.execute('PRAGMA foreign_keys=ON')
    con.executescript(DDL)
    ts = stamp()
    run = con.execute('INSERT INTO official_runs(source,started_at,pages) VALUES(?,?,?)',
                      ('新疆维吾尔自治区法规规章规范性文件库', ts, 0)).lastrowid
    con.commit()
    candidates = inserted = updated = errors = 0
    details = []
    try:
        remaining = max_records
        pages_done = 0
        for cat in categories:
            if cat not in CATS:
                raise ValueError(f'unknown category {cat}')
            filetype_id = CATS[cat]
            page = 1
            seen_ids = set()
            while remaining > 0:
                rows, total = search_page(filetype_id, page, page_size)
                pages_done += 1
                if not rows:
                    break
                for row in rows:
                    if remaining <= 0:
                        break
                    rid = (row.get('id') or '').strip()
                    title = html.unescape(re.sub('<[^>]*>', '', row.get('title') or '')).strip()
                    if not rid or rid in seen_ids:
                        continue
                    seen_ids.add(rid)
                    ftype = (row.get('filetypeVo') or {}).get('name') or ''
                    category = ftype or ('地方性法规' if cat in ('fg', 'auto') else '政府规章')
                    btype = row.get('businessType') or ''
                    src_url = f'{ENTRY}/document-detail?id={rid}&businessType={urllib.parse.quote(btype)}'
                    candidates += 1
                    try:
                        body = extract_body(row.get('regulationFiles') or [])
                        if len(body) < MIN_BODY:
                            raise ValueError(f'official article body too short ({len(body)} chars)')
                        sha = hashlib.sha256(body.encode()).hexdigest()
                        old = con.execute('SELECT id, sha256 FROM official_documents WHERE source_url=?',
                                          (src_url,)).fetchone()
                        pub = row.get('publishDate') or row.get('newPassDate') or row.get('passDate')
                        if old:
                            con.execute('''UPDATE official_documents SET title=?,body=?,sha256=?,
                              publisher=?,publication_date=?,last_seen_at=?,
                              validity=CASE WHEN sha256<>? THEN '待核验' ELSE validity END,
                              validity_evidence_url=CASE WHEN sha256<>? THEN NULL ELSE validity_evidence_url END,
                              validity_checked_at=CASE WHEN sha256<>? THEN NULL ELSE validity_checked_at END
                              WHERE id=?''',
                              (title, body, sha, PUBLISHER, pub, stamp(), sha, sha, sha, old[0]))
                            docid = old[0]
                            if sha != old[1]:
                                updated += 1
                        else:
                            docid = con.execute('''INSERT INTO official_documents
                              (source_url,source_domain,title,jurisdiction,category,publisher,
                               publication_date,document_number,body,sha256,validity,first_seen_at,last_seen_at)
                              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                              (src_url, DOMAIN, title, JURISDICTION, category, PUBLISHER,
                               pub, row.get('releaseNum'), body, sha, '待核验', stamp(), stamp())).lastrowid
                            inserted += 1
                        con.execute('INSERT OR IGNORE INTO official_revisions(document_id,sha256,body,captured_at) VALUES(?,?,?,?)',
                                    (docid, sha, body, stamp()))
                        con.commit()
                        remaining -= 1
                    except Exception as e:
                        errors += 1
                        details.append(f'{src_url}: {str(e)[:160]}')
                    time.sleep(delay)
                if len(rows) < page_size:
                    break
                page += 1
                time.sleep(delay)
        con.execute('UPDATE official_runs SET pages=? WHERE id=?', (pages_done, run))
    except Exception as e:
        errors += 1
        details.append(f'RUN: {type(e).__name__}: {str(e)[:200]}')
    finally:
        con.execute('UPDATE official_runs SET finished_at=?,candidates=?,inserted=?,updated=?,errors=?,error_details=? WHERE id=?',
                    (stamp(), candidates, inserted, updated, errors, json.dumps(details, ensure_ascii=False), run))
        con.commit()
        con.close()
    result = {'source': '新疆维吾尔自治区法规规章规范性文件库', 'categories': list(categories),
              'max_records': max_records, 'candidates': candidates, 'inserted': inserted,
              'updated': updated, 'errors': errors, 'details': details[:8]}
    print(json.dumps(result, ensure_ascii=False))
    if errors:
        raise SystemExit(1)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description='Bounded Xinjiang official regulations collector')
    p.add_argument('--categories', default='fg,gz', help='comma list of fg,auto,gz (default fg,gz)')
    p.add_argument('--max-records', type=int, default=30)
    p.add_argument('--page-size', type=int, default=100)
    p.add_argument('--delay', type=float, default=1.0)
    a = p.parse_args()
    if not 1 <= a.max_records <= 500 or not 1 <= a.page_size <= 200 or a.delay < 0.5:
        p.error('max-records 1..500; page-size 1..200; delay >=0.5s')
    cats = [c.strip() for c in a.categories.split(',') if c.strip()]
    collect(cats, a.max_records, a.page_size, a.delay)
