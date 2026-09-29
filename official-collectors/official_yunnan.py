#!/usr/bin/env python3
"""Bounded official Yunnan regulations collector (省人大 法规规章规范性文件数据库).

Source: 云南省法规规章规范性文件数据库 (http://lf.ynrd.gov.cn/home)
  The frontend is a Vue SPA (ant-design-vue) whose data API is mounted at
  ``/prod-api`` on the same host (identical platform to 山西 fgsjk.sxpc.gov.cn /
  新疆 220.171.42.55:4965). Reverse-engineered endpoints (verified live):

  - list   POST /prod-api/extranet/regulation/search
            body {"pageNum":1,"pageSize":N,"filetypeId":<id>,"timeliness":"1"}
            filetype ids (GET /prod-api/extranet/config/filetype):
              地方性法规 = b6f4201c41007989a9d4dc3dac0d207c
              政府规章   = b36315f2832c2e1cb169e0195bd44461
            rows carry id/title/officeVo/publishDate/expiryDate/timeliness.
  - detail GET  /prod-api/extranet/regulation/getById/{id}
            returns the full record; the regulation body is NOT inline
            (clauseVos is always null) — it is a ``fileClass=4`` attachment in
            ``regulationFiles`` (attType ``doc``/.docx and/or ``pdf``).
  - file   GET  /prod-api/gx-attachment/attachment/obtain/preview/{attId}
            ?module=gx-regulationdb-intranet  -> the original .docx (clean
            Unicode text layer).  The companion PDF's text layer is 方正 GBK
            embedded WITHOUT a ToUnicode CMap, so pymupdf/pdfminer both return
            CIDs; no OCR engine is available on this host, so the .docx is the
            only reliably-extractable source.

Scope & honesty
  - Only ``timeliness=1`` (现行有效) records are ingested: those carry the
    full text, and we store the currently-effective version with
    validity='待核验'.
  - A record is ingested only when a .docx/.doc attachment exists and yields
    >= 300 chars of clean text.  PDF-only or text-less records are SKIPPED and
    logged (never faked).  ~88% of sampled 现行有效 records in both target
    categories carry a .docx.
  - Bounded & rate-limited (pages<=10, page_size<=50, delay>=0.5s).
  - ``validity`` defaults to ``待核验``; a hash change on re-capture resets it.
  - ``source_url`` is the stable official detail endpoint
    ``http://lf.ynrd.gov.cn/prod-api/extranet/regulation/getById/{id}``
    (the SPA has no per-record deep link — detail renders same-page).

Body text comes from the official .docx attachment only.  See official_gov.py
for the shared DDL / stamp; official_zhejiang.py / official_shanghai.py for the
POST + attachment conventions this file follows.
"""
import argparse
import html as _html
import hashlib
import json
import re
import sqlite3
import time
import urllib.parse
import urllib.request
import zipfile
from io import BytesIO
from datetime import datetime, timezone
from pathlib import Path

DB = Path(__file__).resolve().parent / 'laws.sqlite3'
UA = 'Mozilla/5.0 (compatible; LegalCorpusResearch/1.0)'
BASE = 'http://lf.ynrd.gov.cn'
API = BASE + '/prod-api'
LIST_URL = f'{API}/extranet/regulation/search'
GET_BY_ID = API + '/extranet/regulation/getById/{id}'
FILE_URL = f'{API}/gx-attachment/attachment/obtain/preview/{{attId}}?module=gx-regulationdb-intranet'
DOMAIN = 'lf.ynrd.gov.cn'
MODULE = 'gx-regulationdb-intranet'

FILETYPE_LOCAL = ('b6f4201c41007989a9d4dc3dac0d207c', '地方性法规')
FILETYPE_GOV = ('b36315f2832c2e1cb169e0195bd44461', '政府规章')
# 现行有效 -> the version whose text is present; we store it with validity 待核验.
TIMELINESS_EFFECTIVE = '1'
JURISDICTION = '云南'
PUBLISHER = '云南省法规规章规范性文件数据库(省人大)'

# Reuse the official_documents / official_revisions / official_runs schema.
from official_gov import DDL, stamp  # noqa: E402


def fetch(url, referer=None, post=None, limit=20_000_001):
    headers = {'User-Agent': UA, 'Referer': BASE + '/home'}
    data = None
    if post is not None:
        data = json.dumps(post).encode('utf-8')
        headers['Content-Type'] = 'application/json;charset=utf-8'
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers, data=data),
                                timeout=40) as r:
        if r.status != 200:
            raise ValueError(f'HTTP {r.status}')
        b = r.read(limit + 1)
        if len(b) > limit:
            raise ValueError('oversized attachment')
        return b


def listing(filetype_id, page_index, page_size=15):
    post = {'pageNum': page_index, 'pageSize': page_size,
            'filetypeId': filetype_id, 'timeliness': TIMELINESS_EFFECTIVE}
    d = json.loads(fetch(LIST_URL, post=post).decode('utf-8', 'replace'))
    if d.get('code') != 200:
        raise ValueError(f'list error: {d.get("code")} {d.get("msg")}')
    data = d.get('data') or {}
    return data.get('rows') or [], data.get('total', 0)


def detail(record_id):
    d = json.loads(fetch(GET_BY_ID.format(id=record_id)).decode('utf-8', 'replace'))
    if d.get('code') != 200:
        raise ValueError(f'detail error: {d.get("code")} {d.get("msg")}')
    return d.get('data') or {}


def pick_docx(detail_data):
    """Return (attId, attName) of the fileClass=4 .doc/.docx attachment, else None."""
    for f in (detail_data.get('regulationFiles') or []):
        if f.get('fileClass') == '4' and f.get('attType') in ('doc', 'docx'):
            return f.get('attId'), f.get('attName')
    return None


def pick_pdf(detail_data):
    for f in (detail_data.get('regulationFiles') or []):
        if f.get('fileClass') == '4' and f.get('attType') == 'pdf':
            return f.get('attId'), f.get('attName')
    return None


def docx_text(raw):
    """Extract plain text from a .docx byte string (word/document.xml).

    Paragraphs become newlines; all other markup is stripped.  Deterministic,
    so re-extraction of the same file is byte-stable (idempotency).
    """
    with zipfile.ZipFile(BytesIO(raw)) as z:
        xml = z.read('word/document.xml').decode('utf-8', 'replace')
    xml = re.sub(r'</w:p>', '\n', xml)
    xml = re.sub(r'<[^>]+>', '', xml)
    text = _html.unescape(xml)
    lines = [ln.strip() for ln in text.splitlines()]
    return '\n'.join(ln for ln in lines if ln)


def _clean_text(s):
    # The site's docx files carry stray zero-width chars on some lines.
    return re.sub(r'[\u200b\u200c\u200d\ufeff]+', '', s or '')


def _clean_title(t):
    # The site pads titles with a leading zero-width char in some rows.
    return re.sub(r'^[\u200b\u200c\u200d\ufeff]+', '', t or '').strip()


def collect(categories=('local-regulations',), pages=1, page_size=15, delay=1.0):
    filetype_map = {'local-regulations': FILETYPE_LOCAL, 'gov-rules': FILETYPE_GOV}
    con = sqlite3.connect(DB, timeout=30)
    con.execute('PRAGMA foreign_keys=ON')
    con.executescript(DDL)
    con.executescript('''CREATE TABLE IF NOT EXISTS official_assets (
     document_id INTEGER NOT NULL REFERENCES official_documents(id), asset_url TEXT NOT NULL,
     sha256 TEXT NOT NULL, PRIMARY KEY(document_id,asset_url));''')
    ts = stamp()
    run = con.execute('INSERT INTO official_runs(source,started_at,pages) VALUES(?,?,?)',
                      ('云南省法规规章规范性文件数据库(省人大)', ts, pages)).lastrowid
    con.commit()
    inserted = updated = skipped = errors = 0
    details = []
    try:
        for cat in categories:
            filetype_id, category = filetype_map[cat]
            for page in range(1, pages + 1):
                try:
                    rows, total = listing(filetype_id, page, page_size)
                except Exception as e:
                    errors += 1
                    details.append(f'LIST {category} p{page}: {str(e)[:160]}')
                    break
                for row in rows:
                    rid = (row.get('id') or '').strip()
                    if not rid:
                        continue
                    src_url = GET_BY_ID.format(id=rid)
                    try:
                        d = detail(rid)
                        doc = pick_docx(d)
                        if not doc:
                            pdf = pick_pdf(d)
                            note = ('pdf-only(no text layer/OCR)' if pdf
                                    else 'no full-text attachment')
                            skipped += 1
                            details.append(f'SKIP {src_url}: {note}')
                            time.sleep(delay)
                            continue
                        att_id, att_name = doc
                        raw = fetch(FILE_URL.format(attId=att_id))
                        asset_sha = hashlib.sha256(raw).hexdigest()
                        try:
                            body = _clean_text(docx_text(raw))
                        except Exception:
                            pdf = pick_pdf(d)
                            skipped += 1
                            details.append(f'SKIP {src_url}: doc/.doc attachment not extractable '
                                           f'(unparseable;{" pdf only" if pdf else " no pdf fallback"})')
                            time.sleep(delay)
                            continue
                        if len(body) < 300:
                            skipped += 1
                            details.append(f'SKIP {src_url}: docx text too short ({len(body)})')
                            time.sleep(delay)
                            continue
                        office = ((d.get('officeVo') or {}).get('name')) or (row.get('officeVo') or {}).get('name')
                        title = _clean_title(d.get('title')) or _clean_title(row.get('title'))
                        pub = d.get('publishDate') or row.get('publishDate')
                        release = d.get('releaseNum')
                        sha = hashlib.sha256(body.encode()).hexdigest()
                        asset_url = FILE_URL.format(attId=att_id)
                        old = con.execute('SELECT id, sha256 FROM official_documents WHERE source_url=?',
                                          (src_url,)).fetchone()
                        if old:
                            con.execute('''UPDATE official_documents SET title=?,body=?,sha256=?,
                              publisher=?,publication_date=?,document_number=?,last_seen_at=?,
                              validity=CASE WHEN sha256<>? THEN '待核验' ELSE validity END,
                              validity_evidence_url=CASE WHEN sha256<>? THEN NULL ELSE validity_evidence_url END,
                              validity_checked_at=CASE WHEN sha256<>? THEN NULL ELSE validity_checked_at END
                              WHERE id=?''',
                              (title, body, sha, PUBLISHER, pub, release,
                               stamp(), sha, sha, sha, old[0]))
                            docid = old[0]
                            if sha != old[1]:
                                updated += 1
                        else:
                            docid = con.execute('''INSERT INTO official_documents
                              (source_url,source_domain,title,jurisdiction,category,publisher,
                               publication_date,document_number,body,sha256,validity,first_seen_at,last_seen_at)
                              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                              (src_url, DOMAIN, title, JURISDICTION, category, PUBLISHER,
                               pub, release, body, sha, '待核验', stamp(), stamp())).lastrowid
                            inserted += 1
                        con.execute('INSERT OR IGNORE INTO official_revisions(document_id,sha256,body,captured_at) VALUES(?,?,?,?)',
                                    (docid, sha, body, stamp()))
                        con.execute('INSERT OR REPLACE INTO official_assets(document_id,asset_url,sha256) VALUES(?,?,?)',
                                    (docid, asset_url, asset_sha))
                        con.commit()
                    except Exception as e:
                        errors += 1
                        details.append(f'{src_url}: {str(e)[:160]}')
                    time.sleep(delay)
                if page < pages:
                    time.sleep(delay)
    except Exception as e:
        errors += 1
        details.append(f'RUN: {type(e).__name__}: {str(e)[:200]}')
    finally:
        con.execute('UPDATE official_runs SET finished_at=?,candidates=?,inserted=?,updated=?,errors=?,error_details=? WHERE id=?',
                    (stamp(), inserted + updated + skipped, inserted, updated, errors,
                     json.dumps(details, ensure_ascii=False), run))
        con.commit()
        con.close()
    result = {'source': '云南省法规规章规范性文件数据库(省人大)', 'pages': pages,
              'inserted': inserted, 'updated': updated, 'skipped': skipped, 'errors': errors,
              'details': details[:8]}
    print(json.dumps(result, ensure_ascii=False))
    if errors:
        raise SystemExit(1)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--category', choices=['local-regulations', 'gov-rules', 'both'], default='local-regulations')
    p.add_argument('--pages', type=int, default=1)
    p.add_argument('--page-size', type=int, default=15)
    p.add_argument('--delay', type=float, default=1.0)
    a = p.parse_args()
    cats = ['local-regulations', 'gov-rules'] if a.category == 'both' else [a.category]
    if not 1 <= a.pages <= 10 or not 1 <= a.page_size <= 50 or a.delay < 0.5:
        p.error('pages 1..10; page-size 1..50; delay >=0.5s')
    collect(cats, a.pages, a.page_size, a.delay)
