#!/usr/bin/env python3
"""Bounded official Fujian regulations collector (省人大『重要发布·法规』栏目).

Source: 福建省人大常委会门户网站 (szrd.fjrd.gov.cn)『法规』重要发布栏目
  Listing (verified live):  http://szrd.fjrd.gov.cn/zyfb/fg/
    The column page is a TRS-CMS "动静结合" (static/dynamic combined) page.
    The base ``index.html`` statically renders EVERY record of the channel
    (all 64, grouped into ``ms-visible=$showStatic(N)`` <ul> blocks).  The
    JS pagebar (prepage=10, maxStaticIndex=7) only drives dynamic fetches for
    records beyond the 7th static page; the real per-page files
    ``index_N.html`` are NOT served (HTTP 403), so the base index.html is the
    only statically-creatable listing — and it already contains the whole set.
    Newer entries are PDFs (``./YYYYMM/P0YYYYMMDD....pdf``); older entries are
    plain ``.htm`` pages (out of scope: this collector OCRs PDFs only).

Body channel: MinerU Agent lightweight API (no login, IP-rate-limited)
  - submit  POST https://mineru.net/api/v1/agent/parse/url  {"url": <pdf>}  -> task_id
  - poll    GET  https://mineru.net/api/v1/agent/parse/<task_id>
            (NOTE: polling path is ``/agent/parse/<task_id>`` — NOT ``/task/<id>``)
            -> state: running|done|failed  -> data.markdown_url
  - fetch   GET  <markdown_url>  -> OCR'd markdown of the regulation.
  Verified end-to-end on 福建省拥军优属条例 (2026-07): 第一条..第五十五条 extracted.
  Rate limit honoured by the parent's notes (>=3s between OCR submissions,
  bounded PDF size).

Scope & honesty (mirror official_gov.py / official_hunan.py)
  - Bounded & rate-limited: listing pages<=10 (this source serves 1 combined
    page), per-page PDF cap<=50, listing delay>=1s, OCR submit delay>=3s,
    OCR poll delay>=3s.
  - A PDF is ingested ONLY when its OCR'd body actually contains a
    ``第…条`` clause marker AND is long enough; otherwise it is SKIPPED and
    logged (never faked, never stored short/empty).
  - ``validity`` defaults to ``待核验``; a hash change on re-capture resets it.
  - Idempotent: dedup on ``source_url`` (the PDF's official URL) + SHA-256 of
    the OCR'd body; ``official_revisions`` is keyed UNIQUE(document_id,sha256)
    so re-capturing the identical body inserts no duplicate revision row.

Shared DDL / stamp come from official_gov.py (official_documents /
official_revisions / official_runs); the static-listing + insert/update/skip
conventions follow official_hunan.py.
"""
import argparse
import hashlib
import html as _html
import json
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from official_gov import DDL, stamp  # noqa: E402

DB = Path(__file__).resolve().parent / 'laws.sqlite3'
UA = 'Mozilla/5.0 (compatible; LegalCorpusResearch/1.0)'
BASE = 'http://szrd.fjrd.gov.cn'
LIST_URL = BASE + '/zyfb/fg/'
DOMAIN = 'szrd.fjrd.gov.cn'
JURISDICTION = '福建'
CATEGORY = '地方性法规'
PUBLISHER = '福建省人大常委会门户网站(法规栏目)'

MINERU_SUBMIT = 'https://mineru.net/api/v1/agent/parse/url'
MINERU_POLL = 'https://mineru.net/api/v1/agent/parse/{tid}'

# A regulation/decree body must contain at least one 第X条 clause and be
# long enough to be a real text (not an OCR stub / notice).
_CLAUSE_RE = re.compile(r'第[一二三四五六七八九十百千零两]+条')
_MIN_BODY = 300
_PDF_LIMIT = 10 * 1024 * 1024  # MinerU Agent cap: <=10MB
_MINERU_PAGE_CAP = 20  # MinerU Agent cap: <=20 pages per request (else -30003)
# Listing item: <li><a href="PDF-or-HTM" target="_blank" title="T">T</a><span> YYYY-MM-DD</span></li>
_ITEM_RE = re.compile(
    r'<li>\s*<a\s+href="(?P<href>[^"]+)"\s+target="_blank"\s+title="(?P<title>[^"]+)"\s*'
    r'>[^<]*</a>\s*(?:<span>\s*(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})\s*</span>)?\s*</li>', re.S)


def _headers(referer=None):
    h = {'User-Agent': UA, 'Accept': '*/*'}
    if referer:
        h['Referer'] = referer
    return h


def _is_allowed(url):
    p = urllib.parse.urlparse(url)
    if p.scheme not in ('http', 'https'):
        return False
    if p.hostname == DOMAIN:                      # listing + PDFs (szrd.fjrd.gov.cn)
        return True
    if p.hostname == 'mineru.net':                # OCR channel
        return True
    if p.hostname and p.hostname.endswith('openxlab.org.cn'):  # markdown CDN
        return True
    return False


def _get(url, referer=None, method='GET', limit=1_000_001):
    if not _is_allowed(url):
        raise ValueError(f'unapproved host: {url}')
    req = urllib.request.Request(url, headers=_headers(referer), method=method)
    with urllib.request.urlopen(req, timeout=40) as r:
        if r.status != 200:
            raise ValueError(f'HTTP {r.status}')
        if method == 'GET':
            b = r.read(limit + 1)
            if len(b) > limit:
                raise ValueError('oversized resource')
            return b
        return None  # HEAD: body unused


def _post_json(url, payload, timeout=40):
    if not _is_allowed(url):
        raise ValueError(f'unapproved host: {url}')
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode('utf-8'),
        headers=_headers('https://mineru.net/') | {'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status != 200:
            raise ValueError(f'HTTP {r.status}')
        return json.loads(r.read().decode('utf-8', 'replace'))


def _get_json(url, timeout=40):
    """GET a JSON endpoint (used for MinerU polling, which requires GET)."""
    if not _is_allowed(url):
        raise ValueError(f'unapproved host: {url}')
    with urllib.request.urlopen(
            urllib.request.Request(url, headers=_headers('https://mineru.net/'), method='GET'),
            timeout=timeout) as r:
        if r.status != 200:
            raise ValueError(f'HTTP {r.status}')
        return json.loads(r.read().decode('utf-8', 'replace'))


def listing_pdfs(page=1, max_items=50):
    """Return [(abs_pdf_url, title, date)] for the PDF items of the listing.

    This source serves a single combined index.html (all records); ``page`` is
    accepted for the bounded-pages contract but only page 1 is meaningful here.
    Older entries are ``.htm`` and are skipped (PDF-only collector).
    """
    raw = _get(LIST_URL).decode('utf-8', 'replace')
    out, seen = [], set()
    for m in _ITEM_RE.finditer(raw):
        href, title, date = m.group('href'), m.group('title'), m.group('date')
        if not href.lower().endswith('.pdf'):
            continue  # .htm older entries: out of scope
        url = urllib.parse.urljoin(LIST_URL, href.strip())
        if url in seen:
            continue
        seen.add(url)
        out.append((url, _clean_title(title), (date or '').strip() or None))
        if len(out) >= max_items:
            break
    return out


def _clean_title(t):
    return re.sub(r'^[\u200b\u200c\u200d\ufeff]+', '', _html.unescape(t or '')).strip()


def _get_bytes(url, limit=None, referer=None):
    """GET raw bytes (PDFs can be up to the 10MB cap; markdown up to 2MB)."""
    if not _is_allowed(url):
        raise ValueError(f'unapproved host: {url}')
    if limit is None:
        limit = 20_000_000
    req = urllib.request.Request(url, headers=_headers(referer), method='GET')
    with urllib.request.urlopen(req, timeout=60) as r:
        if r.status != 200:
            raise ValueError(f'HTTP {r.status}')
        b = r.read(limit + 1)
        if len(b) > limit:
            raise ValueError('oversized resource')
        return b


def pdf_size_ok(url):
    """HEAD check: ensure the PDF exists (200) and is within MinerU's <=10MB cap."""
    req = urllib.request.Request(url, headers=_headers(LIST_URL), method='HEAD')
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            if r.status != 200:
                raise ValueError(f'pdf HTTP {r.status}')
            cl = r.headers.get('Content-Length')
            if cl and int(cl) > _PDF_LIMIT:
                raise ValueError(f'pdf too large ({int(cl)} > {_PDF_LIMIT})')
    except urllib.error.HTTPError as e:
        raise ValueError(f'pdf HEAD {e.code}')


def _pdf_page_count(url):
    """Download the PDF and count its pages via PyMuPDF (fitz)."""
    try:
        import pymupdf
    except Exception:
        import fitz as pymupdf
    raw = _get_bytes(url, limit=_PDF_LIMIT + 1, referer=LIST_URL)
    if not raw.startswith(b'%PDF'):
        raise ValueError('not a PDF (%PDF- header missing)')
    doc = pymupdf.open(stream=raw)
    n = doc.page_count
    doc.close()
    return n


def _mineru_parse(url, poll_delay, poll_timeout, page_range=None):
    """Submit one MinerU Agent task (optional page_range) and return its markdown.

    Submit is POST; polling is GET (the /agent/parse/<task_id> endpoint rejects
    POST with HTTP 405).  On failure the API reports ``err_code``/``err_msg``
    (not ``message``) — surfaced verbatim so the cause is not lost.
    """
    payload = {'url': url}
    if page_range:
        payload['page_range'] = page_range
    sub = _post_json(MINERU_SUBMIT, payload)
    if sub.get('code') != 0:
        raise ValueError(f'mineru submit: {sub.get("code")} {sub.get("msg")}')
    tid = sub['data']['task_id']
    deadline = time.time() + poll_timeout
    while True:
        time.sleep(poll_delay)
        d = _get_json(MINERU_POLL.format(tid=tid))
        data = d.get('data') or {}
        state = data.get('state')
        if state == 'done':
            murl = data.get('markdown_url')
            if not murl:
                raise ValueError('mineru done but no markdown_url')
            return _get(murl, limit=2_000_001).decode('utf-8', 'replace')
        if state in ('failed', 'error'):
            raise ValueError(
                f'mineru {state}: {data.get("err_code")} '
                f'{data.get("err_msg") or d.get("msg")}')
        if time.time() > deadline:
            raise ValueError('mineru poll timeout')


def mineru_ocr(url, poll_delay=3, poll_timeout=300, page_delay=3):
    """OCR a Fujian regulation PDF via the MinerU Agent channel; return markdown.

    The Agent API is capped at **20 pages / request** (else ``err_code -30003``).
    For PDFs within the cap a single whole-file request is used.  For larger
    PDFs the file is counted with PyMuPDF and chunked into <=20-page
    ``page_range`` requests (from-to only, per the Agent API), then the markdown
    chunks are concatenated in page order.  ``page_delay`` spaces submissions
    within a chunked PDF (MinerU IP rate limit, >=3s); spacing between distinct
    PDFs is enforced by the caller via ``delay``.
    """
    n = _pdf_page_count(url)
    if n <= _MINERU_PAGE_CAP:
        return _mineru_parse(url, poll_delay, poll_timeout, page_range=None)
    chunks, start, first = [], 1, True
    while start <= n:
        end = min(start + _MINERU_PAGE_CAP - 1, n)
        if not first:
            time.sleep(page_delay)  # MinerU IP rate limit between submissions
        chunks.append(_mineru_parse(url, poll_delay, poll_timeout, page_range=f'{start}-{end}'))
        start = end + 1
        first = False
    return '\n\n'.join(c for c in chunks if c and c.strip())


def extract_body(markdown):
    """Validate + normalize OCR'd markdown into the stored body text.

    Raises ValueError when the text is not a regulation/decree body (no 第X条
    clause, or too short) — the caller SKIPS such items honestly.
    """
    body = re.sub(r'[\u200b\u200c\u200d\ufeff]+', '', markdown or '').strip()
    if not _CLAUSE_RE.search(body):
        raise ValueError('no 第…条 clause in OCR body')
    if len(body) < _MIN_BODY:
        raise ValueError(f'OCR body too short ({len(body)})')
    return body


def _upsert(con, url, title, body, pubdate):
    """Insert-or-update one document; return (docid, status) where status in
    {'new','same','changed'}.  Dedup on source_url; SHA-256 of OCR body.
    Hash change resets validity to 待核验 (matches official_gov.py)."""
    sha = hashlib.sha256(body.encode()).hexdigest()
    old = con.execute(
        'SELECT id, sha256 FROM official_documents WHERE source_url=?', (url,)
    ).fetchone()
    if old:
        docid = old[0]
        con.execute('''UPDATE official_documents SET title=?,body=?,sha256=?,
            publication_date=?,last_seen_at=?,
            validity=CASE WHEN sha256<>? THEN '待核验' ELSE validity END,
            validity_evidence_url=CASE WHEN sha256<>? THEN NULL ELSE validity_evidence_url END,
            validity_checked_at=CASE WHEN sha256<>? THEN NULL ELSE validity_checked_at END
            WHERE id=?''',
            (title, body, sha, pubdate, stamp(), sha, sha, sha, docid))
        return docid, ('changed' if old[1] != sha else 'same')
    docid = con.execute('''INSERT INTO official_documents
        (source_url,source_domain,title,jurisdiction,category,publisher,
         publication_date,body,sha256,validity,first_seen_at,last_seen_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
        (url, DOMAIN, title, JURISDICTION, CATEGORY, PUBLISHER,
         pubdate, body, sha, '待核验', stamp(), stamp())).lastrowid
    return docid, 'new'


def collect(pages=1, max_items=50, delay=3.0, poll_delay=3.0,
             poll_timeout=300):
    con = sqlite3.connect(DB, timeout=30)
    con.execute('PRAGMA foreign_keys=ON')
    con.executescript(DDL)
    run = con.execute('INSERT INTO official_runs(source,started_at,pages) VALUES(?,?,?)',
                      ('福建省人大常委会(法规栏目)', stamp(), pages)).lastrowid
    con.commit()
    inserted = updated = skipped = errors = 0
    details = []
    try:
        # This source is a single combined listing page (page 1 holds all 64
        # records).  ``pages`` is bounded 1..10 for the shared contract; pages
        # >1 are not served by the site and are reported as no-op.
        for page in range(1, pages + 1):
            if page > 1:
                details.append(f'NOTE listing only serves 1 combined page; page {page} not served')
                continue
            try:
                items = listing_pdfs(page, max_items)
            except Exception as e:
                errors += 1
                details.append(f'LIST p{page}: {str(e)[:160]}')
                break
            for url, title, pubdate in items:
                try:
                    pdf_size_ok(url)
                    body = extract_body(mineru_ocr(url, poll_delay, poll_timeout, page_delay=3))
                except Exception as e:
                    # Honest skip: OCR failed / no 第X条 / too short / oversized.
                    skipped += 1
                    details.append(f'SKIP {url}: {str(e)[:150]}')
                    time.sleep(delay)
                    continue
                try:
                    docid, status = _upsert(con, url, title, body, pubdate)
                    if status == 'new':
                        inserted += 1
                    elif status == 'changed':
                        updated += 1
                    con.execute(
                        'INSERT OR IGNORE INTO official_revisions(document_id,sha256,body,captured_at) '
                        'VALUES(?,?,?,?)', (docid, hashlib.sha256(body.encode()).hexdigest(), body, stamp()))
                    con.commit()
                except Exception as e:
                    errors += 1
                    details.append(f'{url}: {str(e)[:150]}')
                time.sleep(delay)  # polite spacing between OCR submissions
    except Exception as e:
        errors += 1
        details.append(f'RUN: {type(e).__name__}: {str(e)[:200]}')
    finally:
        con.execute(
            'UPDATE official_runs SET finished_at=?,candidates=?,inserted=?,updated=?,errors=?,error_details=? WHERE id=?',
            (stamp(), inserted + updated + skipped, inserted, updated, errors,
             json.dumps(details, ensure_ascii=False), run))
        con.commit()
        con.close()
    result = {'source': PUBLISHER, 'pages': pages, 'max_items': max_items,
              'inserted': inserted, 'updated': updated, 'skipped': skipped,
              'errors': errors, 'details': details[:8]}
    print(json.dumps(result, ensure_ascii=False))
    if errors:
        raise SystemExit(1)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--pages', type=int, default=1)
    p.add_argument('--max-items', type=int, default=50)
    p.add_argument('--delay', type=float, default=3.0,
                 help='spacing between OCR submissions (>=3s: MinerU IP rate limit; also >=1s listing bound)')
    p.add_argument('--poll-delay', type=float, default=3.0, help='MinerU poll interval (>=3s)')
    p.add_argument('--poll-timeout', type=int, default=300, help='max seconds to wait per OCR task')
    a = p.parse_args()
    if not 1 <= a.pages <= 10:
        p.error('pages 1..10')
    if not 1 <= a.max_items <= 50:
        p.error('max-items 1..50')
    if a.delay < 3.0:
        p.error('delay >= 3s (MinerU IP rate limit; listing bound is >=1s)')
    if a.poll_delay < 3.0:
        p.error('poll-delay >= 3s (MinerU IP rate limit)')
    collect(a.pages, a.max_items, a.delay, a.poll_delay, a.poll_timeout)
