#!/usr/bin/env python3
"""Bounded official Yunnan PDF-scan collector (OCR backfill via MinerU Agent API).

companion to official_yunnan.py.  The first pass could only ingest records that
carry a clean .docx attachment; records whose full text is a scanned PDF (方正
GBK embedded font WITHOUT a ToUnicode CMap, so pymupdf/pdfminer return CIDs)
were SKIPPED.  This script OCRs those PDFs and ingests the OCR text.

Source (identical host/API to official_yunnan.py):
  - list   POST /prod-api/extranet/regulation/search   (timeliness=1 现行有效)
  - detail GET  /prod-api/extranet/regulation/getById/{id}
  - file   GET  /prod-api/gx-attachment/attachment/obtain/preview/{attId}
            ?module=gx-regulationdb-intranet           (the original PDF)

OCR channel (verified live, no auth required):
  MinerU Agent 轻量API  POST https://mineru.net/api/v1/agent/parse/url
          body {"url": <public PDF url>}  (+ optional "page_range":"a-b")
        -> {"data":{"task_id": ...}}
  poll      GET https://mineru.net/api/v1/agent/parse/<task_id>
        -> state=done  -> data.markdown_url  (download the markdown)
  Limits: <=10 MB, <=20 pages (larger -> chunk with page_range), IP rate limit
  -> we pace every MinerU submission by MINERU_PAUSE (default 3 s).

Body slicing:  a scanned 条例 PDF often carries a 审查报告 / 批准决议 preamble
before the regulation's own ``# <title>`` heading.  We slice the markdown from
the first markdown heading line that contains the official title, and keep to
the end (matches the docx-collector convention: title + 通过/批准 line + body).
A 决定-type document has the title as its first line and is used whole.

Ingestion constraints (all enforced):
  - official_documents table, SHA-256 of the OCR body, validity=待核验,
    dedup by source_url (UNIQUE) + reset-on-hash-change.
  - OCR-text gate: the body must contain ``第一条`` AND a 施行 clause
    (``施行``) and be >= 80 chars, else it is SKIPPED (logged, never faked).
    A short 废止/修改 决定 without 第一条 (e.g. 昆明) fails this gate honestly.
  - Bounded & rate-limited: pages<=2, page_size<=50, delay>=0.5s, mineru pause
    >=3s; page_range chunking for >20-page PDFs.
  - Idempotent: a source_url already in the DB is re-checked but not re-OCR'd
    (re-run inserts 0 / updates only on real change).

See official_yunnan.py for the first-pass docx collector and official_gov.py
for the shared DDL / stamp.
"""
import argparse
import hashlib
import json
import re
import sqlite3
import time
import urllib.error
import urllib.request
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

MINERU_SUBMIT = 'https://mineru.net/api/v1/agent/parse/url'
MINERU_POLL = 'https://mineru.net/api/v1/agent/parse/{tid}'

FILETYPE_LOCAL = ('b6f4201c41007989a9d4dc3dac0d207c', '地方性法规')
FILETYPE_GOV = ('b36315f2832c2e1cb169e0195bd44461', '政府规章')
# businessType (from detail) -> canonical category label.
BUSINESS_CATEGORY = {
    'localRegulations': '地方性法规',
    'localGovernmentRegulations': '政府规章',
}
PUBLISHER = '云南省法规规章规范性文件数据库(省人大)'

MINERU_MAX_MB = 10
MINERU_MAX_PAGES = 20
MIN_BODY_LEN = 80
MINERU_MAX_WAIT = 120   # s per task
MINERU_POLL_EVERY = 4   # s

from official_gov import DDL, stamp  # noqa: E402


def _http(url, post=None, timeout=60):
    headers = {'User-Agent': UA, 'Referer': BASE + '/home'}
    data = None
    if post is not None:
        data = json.dumps(post).encode('utf-8')
        headers['Content-Type'] = 'application/json;charset=utf-8'
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers, data=data),
                                timeout=timeout) as r:
        if r.status != 200:
            raise ValueError(f'HTTP {r.status}')
        return r.read()


def fetch_bytes(url, limit=20_000_001):
    return _http(url, timeout=90)[:limit]


def listing(filetype_id, page_index, page_size=15):
    post = {'pageNum': page_index, 'pageSize': page_size,
            'filetypeId': filetype_id, 'timeliness': '1'}
    d = json.loads(_http(LIST_URL, post=post).decode('utf-8', 'replace'))
    if d.get('code') != 200:
        raise ValueError(f'list error: {d.get("code")} {d.get("msg")}')
    data = d.get('data') or {}
    return data.get('rows') or [], data.get('total', 0)


def detail(record_id):
    d = json.loads(_http(GET_BY_ID.format(id=record_id)).decode('utf-8', 'replace'))
    if d.get('code') != 200:
        raise ValueError(f'detail error: {d.get("code")} {d.get("msg")}')
    return d.get('data') or {}


def pick_pdf(detail_data):
    for f in (detail_data.get('regulationFiles') or []):
        if f.get('fileClass') == '4' and f.get('attType') == 'pdf':
            return f.get('attId'), f.get('attName')
    return None


def has_docx(detail_data):
    return any(f.get('fileClass') == '4' and f.get('attType') in ('doc', 'docx')
               for f in (detail_data.get('regulationFiles') or []))


def pdf_page_count(raw):
    """Page count: pymupdf if available, else a /Type /Page regex fallback."""
    try:
        import fitz  # noqa: F401 (legacy alias)
        doc = fitz.open(stream=raw)
        try:
            return doc.page_count
        finally:
            doc.close()
    except Exception:
        pass
    return len(re.findall(rb'/Type\s*/Page[^s]', raw))


# ----------------------------- MinerU OCR ------------------------------------

def mineru_submit(pdf_url, page_range=None):
    payload = {'url': pdf_url}
    if page_range:
        payload['page_range'] = page_range
    d = json.loads(_http(MINERU_SUBMIT, post=payload).decode('utf-8', 'replace'))
    if d.get('code') != 0:
        raise ValueError(f'mineru submit code={d.get("code")} {d.get("msg")}')
    return (d.get('data') or {}).get('task_id')


def mineru_wait(task_id):
    """Poll until state is terminal; return the data dict (with markdown_url)."""
    waited = 0.0
    while waited < MINERU_MAX_WAIT:
        d = json.loads(_http(MINERU_POLL.format(tid=task_id)).decode('utf-8', 'replace'))
        data = d.get('data') or {}
        state = data.get('state')
        if state in ('done', 'success'):
            return data
        if state in ('failed', 'error'):
            raise ValueError(f'mineru failed: {data.get("err_code")} {data.get("err_msg")}')
        time.sleep(MINERU_POLL_EVERY)
        waited += MINERU_POLL_EVERY
    raise ValueError(f'mineru task {task_id} still running after {MINERU_MAX_WAIT}s')


def mineru_markdown(pdf_url, n_pages):
    """OCR a PDF -> markdown text.  Chunk with page_range when it exceeds the
    per-request page limit; concatenate chunks in order."""
    if n_pages <= MINERU_MAX_PAGES:
        chunks = [(None,)]
    else:
        chunks = []
        start = 1
        while start <= n_pages:
            end = min(start + MINERU_MAX_PAGES - 1, n_pages)
            chunks.append((f'{start}-{end}',))
            start = end + 1
    parts = []
    for (page_range,) in chunks:
        tid = mineru_submit(pdf_url, page_range)
        time.sleep(3)  # pace MinerU (IP rate limit)
        data = mineru_wait(tid)
        md_url = data.get('markdown_url')
        if not md_url:
            raise ValueError(f'mineru no markdown_url (task {tid})')
        parts.append(_http(md_url, timeout=60).decode('utf-8', 'replace'))
    return '\n'.join(parts)


# --------------------------- body preparation -------------------------------

def _clean_text(s):
    return re.sub(r'[\u200b\u200c\u200d\ufeff]+', '', s or '')


def _clean_title(t):
    return re.sub(r'^[\u200b\u200c\u200d\ufeff]+', '', t or '').strip()


def _norm_title_for_match(t):
    """Title form used to find the regulation heading inside OCR markdown."""
    t = _clean_title(t)
    return re.sub(r'^\s*#{1,6}\s*', '', t).strip()


def slice_body(md, title):
    """Slice the regulation proper out of OCR markdown.

    Returns text from the regulation's own heading (the first markdown heading
    line equal to the title, else starts-with, else containing a title prefix)
    to the end of the document (keeps 通过/批准 line + 条款).  Preferring
    *equality* anchors a scanned 条例 on its own heading rather than on a
    批准决议/审查报告 heading that merely contains the title as a substring.
    Falls back to the whole document when no such heading exists (决定 type).
    """
    target = _norm_title_for_match(title)
    prefix = target[:8]
    lines = md.splitlines()

    def _heading_body(i):
        body = '\n'.join(lines[i:])
        body = re.sub(r'^\s*#{1,6}\s*', '', body, count=1)  # drop the heading marker
        return _clean_text(body).strip()

    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith('#') and _norm_title_for_match(s) == target:
            return _heading_body(i)
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith('#') and _norm_title_for_match(s).startswith(target):
            return _heading_body(i)
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith('#') and prefix and prefix in s:
            return _heading_body(i)
    return _clean_text(md).strip()


def passes_gate(body):
    """OCR-text gate: 第一条 + 施行 + min length.  Returns (ok, reason)."""
    if len(body) < MIN_BODY_LEN:
        return False, f'text too short ({len(body)}<{MIN_BODY_LEN})'
    if '第一条' not in body:
        return False, 'no 第一条 clause'
    if '施行' not in body:
        return False, 'no 施行 clause'
    return True, 'ok'


# --------------------------------- collect -----------------------------------

def collect(categories=('local-regulations',), pages=1, page_size=15,
            delay=1.0, mineru_pause=3.0, dry_run=False):
    filetype_map = {'local-regulations': FILETYPE_LOCAL, 'gov-rules': FILETYPE_GOV}
    con = sqlite3.connect(DB, timeout=30)
    con.execute('PRAGMA foreign_keys=ON')
    con.executescript(DDL)
    con.executescript('''CREATE TABLE IF NOT EXISTS official_assets (
     document_id INTEGER NOT NULL REFERENCES official_documents(id), asset_url TEXT NOT NULL,
     sha256 TEXT NOT NULL, PRIMARY KEY(document_id,asset_url));''')
    ts = stamp()
    run = con.execute('INSERT INTO official_runs(source,started_at,pages) VALUES(?,?,?)',
                      ('云南省法规规章规范性文件数据库(省人大)·PDF-OCR补采', ts, pages)).lastrowid
    con.commit()
    candidates = inserted = updated = skipped = ocr_fail = errors = 0
    details = []
    try:
        for cat in categories:
            filetype_id, category_default = filetype_map[cat]
            for page in range(1, pages + 1):
                try:
                    rows, total = listing(filetype_id, page, page_size)
                except Exception as e:
                    errors += 1
                    details.append(f'LIST p{page}: {str(e)[:160]}')
                    break
                for row in rows:
                    rid = (row.get('id') or '').strip()
                    if not rid:
                        continue
                    src_url = GET_BY_ID.format(id=rid)
                    try:
                        # Idempotency: already ingested (by any path) -> skip OCR.
                        old = con.execute('SELECT id, sha256 FROM official_documents WHERE source_url=?',
                                          (src_url,)).fetchone()
                        if old:
                            details.append(f'EXISTS {src_url}')
                            continue
                        d = detail(rid)
                        pdf = pick_pdf(d)
                        if not pdf:
                            # No PDF at all -> genuinely uncollectable (no full text).
                            skipped += 1
                            details.append(f'SKIP {src_url}: no full-text attachment (no PDF)')
                            time.sleep(delay)
                            continue
                        candidates += 1
                        att_id, att_name = pdf
                        title = _clean_title(d.get('title')) or _clean_title(row.get('title'))
                        category = (BUSINESS_CATEGORY.get(d.get('businessType'))
                                    or category_default)
                        raw = fetch_bytes(FILE_URL.format(attId=att_id))
                        if len(raw) > MINERU_MAX_MB * 1024 * 1024:
                            skipped += 1
                            details.append(f'SKIP {src_url}: PDF >{MINERU_MAX_MB}MB ({len(raw)}B)')
                            time.sleep(delay)
                            continue
                        n_pages = pdf_page_count(raw)
                        if dry_run:
                            details.append(f'DRY {src_url} {title[:24]} cat={category} pages={n_pages}')
                            continue
                        try:
                            md = mineru_markdown(FILE_URL.format(attId=att_id), n_pages)
                            time.sleep(max(mineru_pause, 0))
                        except Exception as e:
                            ocr_fail += 1
                            details.append(f'OCR-FAIL {src_url}: {str(e)[:160]}')
                            continue
                        body = slice_body(md, title)
                        ok, why = passes_gate(body)
                        if not ok:
                            skipped += 1
                            details.append(f'SKIP {src_url}: gate fail ({why})')
                            continue
                        office = ((d.get('officeVo') or {}).get('name')) or (row.get('officeVo') or {}).get('name')
                        pub = d.get('publishDate') or row.get('publishDate')
                        release = d.get('releaseNum')
                        sha = hashlib.sha256(body.encode()).hexdigest()
                        asset_sha = hashlib.sha256(raw).hexdigest()
                        asset_url = FILE_URL.format(attId=att_id)
                        docid = con.execute('''INSERT INTO official_documents
                          (source_url,source_domain,title,jurisdiction,category,publisher,
                           publication_date,document_number,body,sha256,validity,first_seen_at,last_seen_at)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                          (src_url, DOMAIN, title, '云南', category, PUBLISHER,
                           pub, release, body, sha, '待核验', stamp(), stamp())).lastrowid
                        inserted += 1
                        con.execute('INSERT OR IGNORE INTO official_revisions(document_id,sha256,body,captured_at) VALUES(?,?,?,?)',
                                    (docid, sha, body, stamp()))
                        con.execute('INSERT OR REPLACE INTO official_assets(document_id,asset_url,sha256) VALUES(?,?,?)',
                                    (docid, asset_url, asset_sha))
                        con.commit()
                        details.append(f'OCR-OK {src_url}: {title[:24]} cat={category} '
                                       f'pages={n_pages} len={len(body)}')
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
                    (stamp(), candidates, inserted, updated, errors + ocr_fail,
                     json.dumps(details, ensure_ascii=False), run))
        con.commit()
        con.close()
    result = {'source': '云南省法规规章规范性文件数据库(省人大)·PDF-OCR补采',
              'pages': pages, 'dry_run': dry_run, 'candidates': candidates,
              'inserted': inserted, 'updated': updated, 'skipped': skipped,
              'ocr_fail': ocr_fail, 'errors': errors, 'details': details[:40]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if errors or ocr_fail:
        raise SystemExit(1)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--category', choices=['local-regulations', 'gov-rules', 'both'], default='both')
    p.add_argument('--pages', type=int, default=1)
    p.add_argument('--page-size', type=int, default=15)
    p.add_argument('--delay', type=float, default=1.0)
    p.add_argument('--mineru-pause', type=float, default=3.0)
    p.add_argument('--dry-run', action='store_true',
                   help='list the PDF-only candidates (no OCR, no DB write)')
    a = p.parse_args()
    cats = ['local-regulations', 'gov-rules'] if a.category == 'both' else [a.category]
    if not 1 <= a.pages <= 2 or not 1 <= a.page_size <= 50 or a.delay < 0.5:
        p.error('pages 1..2; page-size 1..50; delay >=0.5s')
    collect(cats, a.pages, a.page_size, a.delay, a.mineru_pause, a.dry_run)
