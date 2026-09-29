#!/usr/bin/env python3
"""Bounded Guangdong NPC regulation/rules collector with MinerU OCR (广东省地方性法规数据库).

Source: 广东省人大常委会『广东省法规规章规范性文件数据库』 (www.gdpc.gov.cn/bascdata)
  - list   POST https://www.gdpc.gov.cn/bascdata/nfrr/law-rule!noSession_es_regulation_search.gx
           JSON body {pageNum,pageSize,orderByColumn:'passDate',lawRuleType:<1|2>}; no auth.
           lawRuleType 1 = 地方性法规 (含省级/经济特区/设区的市 较大市), 2 = 政府规章.
           res = {code:200, data:{rows:[{id,title,timeliness,passDate,officeVo.groupName,...}], total}}
  - detail GET  .../nfrr/law-rule!noSession_getById.gx?id=<id>
           d['list'] = [{fileClass:'3' 法规文本 / '1' 备案报告 / '6' 起草说明, fileExt, filePath, ...}]
  - asset  GET  .../bascdata/downloadFile?type=1&fileFolder=<filePath>  -> the regulation PDF
           (实测 4.9MB / 14 页, 扫描件无文字层; 少数带文字层)

Body text: the regulation is published as a (mostly scanned) PDF. The PDF text layer is
tried first with pymupdf — if it yields a real CJK text layer it is used directly (no OCR
cost); otherwise the PDF is sent to the MinerU Agent 轻量 OCR API (免登录):
  POST https://mineru.net/api/v1/agent/parse/url  {"url": <public download url>} -> task_id
  GET  https://mineru.net/api/v1/agent/parse/<task_id>  -> state=done -> data.markdown_url
If MinerU cannot fetch the remote URL it falls back to a local multipart upload
POST /api/v1/agent/parse/file. MinerU limits: <=10MB / <=20 pages / IP rate limit — PDFs
over the limit are skipped honestly (recorded in official_runs), never fabricated.

Every row defaults to validity='待核验' (site 时效 hint is NOT mapped to validity). A hash
change on re-capture resets validity to 待核验. Bounded & rate-limited (pages<=10,
page-size<=50, delay>=1s). Re-capture is idempotent: the PDF bytes SHA-256 is stored in
official_assets; when an item's PDF is unchanged the OCR step is skipped entirely.
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
import uuid
from pathlib import Path

from official_gov import DDL, stamp  # noqa: E402

import pymupdf

DB = Path(__file__).resolve().parent / 'laws.sqlite3'
UA = 'Mozilla/5.0 (compatible; LegalCorpusResearch/1.0)'
BASE = 'https://www.gdpc.gov.cn/bascdata'
SEARCH = BASE + '/nfrr/law-rule!noSession_es_regulation_search.gx'
BYID = BASE + '/nfrr/law-rule!noSession_getById.gx'
DOWNLOAD = BASE + '/downloadFile'
DOMAIN = 'www.gdpc.gov.cn'
MINERU = 'https://mineru.net/api/v1/agent'
MINERU_MAX_BYTES = 10 * 1024 * 1024
MINERU_MAX_PAGES = 20
PDF_CAP = 10 * 1024 * 1024 + 1        # read cap for the asset download
OCR_TIMEOUT = 200                     # max seconds to wait for a MinerU task
OCR_POLL = 6                          # polling interval
CJK_RE = re.compile(r'[\u4e00-\u9fff]')


def _http(url, data=None, headers=None, method=None, timeout=30, cap=None):
    h = {'User-Agent': UA, 'Referer': 'https://www.gdpc.gov.cn/bascdata/'}
    if headers:
        h.update(headers)
    if data is not None and 'Content-Type' not in h:
        h['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status != 200:
            raise ValueError(f'HTTP {r.status}')
        return r.read(cap + 1) if cap else r.read()


def search_page(page_num, page_size, law_rule_type):
    """Return list of regulation rows for one listing page of one category."""
    body = {'pageNum': page_num, 'pageSize': page_size,
            'orderByColumn': 'passDate', 'lawRuleType': law_rule_type}
    raw = _http(SEARCH, data=json.dumps(body).encode('utf-8'))
    d = json.loads(raw.decode('utf-8'))
    if d.get('code') != 200:
        raise ValueError(f'list code={d.get("code")} {str(d.get("msg"))[:80]}')
    return (d.get('data') or {}).get('rows') or []


# 法规文本 attachment fileClass codes. By category the regulation body is: 政府规章
# '3' (法规文本) or '5' (政府令号/第N号, e.g. 废止令); 地方性法规 '37' (the statute/决定 itself).
# Auxiliary codes (备案报告 1/10, 起草说明 4/6, 议案 07, 公告 36, 审议报告 99) are excluded.
REG_TEXT_CLASSES = {'3', '5', '37'}


def _rank_attachment(f, title, lr_name):
    name = f.get('fileName') or ''

    def stem(fn):
        fn = fn or ''
        for ext in ('.pdf', '.docx', '.doc'):
            if fn.lower().endswith(ext):
                return fn[:-len(ext)]
        return fn

    if '法规文本' in name or '文本' in name:
        return 0
    if title and (stem(name) == title or stem(name) == lr_name):
        return 1
    return 2


def regulation_text_attachment(doc_id, title=''):
    """Pick the regulation-text attachment from the detail API's file list.

    Returns (file_path, ext) preferring the PDF (for OCR), falling back to a .docx
    (text layer). Returns None if only auxiliary files exist. fileExt is normalized
    ('pdf'/'docx' — some items carry a leading dot).
    """
    raw = _http(f'{BYID}?id={doc_id}')
    d = json.loads(raw.decode('utf-8'))
    lr_name = ((d.get('lawRule') or {}).get('fileName') or '').strip()
    files = d.get('list') or []
    exts = ['pdf', 'docx']
    for ext in exts:
        cand = [f for f in files if (f.get('fileExt') or '').lstrip('.') == ext
               and str(f.get('fileClass')) in REG_TEXT_CLASSES]
        if cand:
            cand.sort(key=lambda f: _rank_attachment(f, title, lr_name))
            fp = cand[0].get('filePath')
            if fp:
                return fp, ext
    return None


def download_asset(file_path, cap=PDF_CAP):
    """Download the regulation attachment bytes + build the public download URL."""
    pub = f'{DOWNLOAD}?type=1&fileFolder=' + urllib.parse.quote(file_path, safe='')
    raw = _http(pub, cap=cap, timeout=40)
    return raw, pub


def extract_docx_text(docx_bytes):
    """Extract text from a .docx (zip) word/document.xml. Returns '' if not a zip."""
    import zipfile
    try:
        with zipfile.ZipFile(io.BytesIO(docx_bytes)) as z:
            xml = z.read('word/document.xml').decode('utf-8', 'replace')
    except Exception:
        return ''
    xml = xml.replace('</w:p>', '\n').replace('<w:br/>', '\n')
    text = re.sub(r'<[^>]+>', '', xml)
    return text


def extract_local_text(pdf_bytes):
    """Return (text, pages) from the PDF text layer, or ('', pages) for scans.

    Trusted only when the layer contains a real CJK text run (filters scans + CID garbage).
    """
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype='pdf')
    except Exception:
        return '', 0
    pages = len(doc)
    text = '\n'.join(doc[i].get_text(sort=True).strip() for i in range(pages))
    doc.close()
    if len(CJK_RE.findall(text)) < 100:
        return '', pages
    return text, pages


def _mineru_post(state, body=None, content_type=None):
    headers = {'User-Agent': UA, 'Content-Type': content_type or 'application/json'}
    req = urllib.request.Request(MINERU + state, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode('utf-8'))


def _mineru_poll(task_id):
    start = time.time()
    st = None
    while time.time() - start < OCR_TIMEOUT:
        d = _mineru_post(f'/parse/{task_id}')
        data = d.get('data') or {}
        st = data.get('state')
        if st in ('done', 'success'):
            return data.get('markdown_url')
        if st in ('error', 'failed'):
            raise ValueError(f'MinerU state={st} {str(d.get("msg"))[:120]}')
        time.sleep(OCR_POLL)
    raise ValueError(f'MinerU timeout after {OCR_TIMEOUT}s (last={st})')


def mineru_ocr(pdf_bytes, public_url):
    """OCR a regulation PDF via the MinerU Agent API. Returns (markdown, mode).

    Tries parse/url first; any failure (incl. remote-fetch failure) falls back to
    parse/file (local upload). Raises if both fail.
    """
    # 1) remote URL parse
    try:
        r = _mineru_post('/parse/url', body=json.dumps({'url': public_url}).encode('utf-8'))
        task_id = (r.get('data') or {}).get('task_id')
        if not task_id:
            raise ValueError(f'no task_id: {json.dumps(r, ensure_ascii=False)[:120]}')
        return _http(_mineru_poll(task_id), timeout=60).decode('utf-8', 'replace'), 'url'
    except Exception:
        pass  # fall through to local upload
    # 2) local multipart upload
    bnd = '----guangdong' + uuid.uuid4().hex
    body = io.BytesIO()
    body.write(f'--{bnd}\r\nContent-Disposition: form-data; name="files"; '
               f'filename="regulation.pdf"\r\nContent-Type: application/pdf\r\n\r\n'.encode())
    body.write(pdf_bytes)
    body.write(b'\r\n--' + bnd.encode() + b'--\r\n')
    r = _mineru_post('/parse/file', body=body.getvalue(),
                     content_type=f'multipart/form-data; boundary={bnd}')
    task_id = (r.get('data') or {}).get('task_id')
    if not task_id:
        raise ValueError(f'no task_id (file): {json.dumps(r, ensure_ascii=False)[:120]}')
    return _http(_mineru_poll(task_id), timeout=60).decode('utf-8', 'replace'), 'file'


def clean_body(md):
    md = re.sub(r'<!--.*?-->', '', md, flags=re.S).replace('\u00a0', ' ')
    lines = [re.sub(r'[ \t]+', ' ', ln).strip() for ln in md.splitlines()]
    out, blank = [], 0
    for ln in lines:
        if ln:
            out.append(ln); blank = 0
        else:
            blank += 1
            if blank <= 1:
                out.append('')
    while out and not out[0]:
        out.pop(0)
    while out and not out[-1]:
        out.pop()
    return '\n'.join(out)


def upsert(con, url, title, body, sha, publisher, pub_date, release_num, asset_url, pdf_sha, category):
    """Insert-or-update one document. Returns True if the row is new, else change-flag."""
    old = con.execute('SELECT id,sha256 FROM official_documents WHERE source_url=?',
                      (url,)).fetchone()
    if old:
        con.execute('''UPDATE official_documents SET title=?,body=?,sha256=?,publisher=?,
          category=?,
          publication_date=?,document_number=?,last_seen_at=?,
          validity=CASE WHEN sha256<>? THEN '待核验' ELSE validity END,
          validity_evidence_url=CASE WHEN sha256<>? THEN NULL ELSE validity_evidence_url END,
          validity_checked_at=CASE WHEN sha256<>? THEN NULL ELSE validity_checked_at END
          WHERE id=?''',
          (title, body, sha, publisher, category, pub_date, release_num, stamp(),
           sha, sha, sha, old[0]))
        docid = old[0]
        changed = sha != old[1]
    else:
        docid = con.execute('''INSERT INTO official_documents
          (source_url,source_domain,title,jurisdiction,category,publisher,publication_date,
           document_number,body,sha256,validity,first_seen_at,last_seen_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
          (url, DOMAIN, title, '广东', category, publisher, pub_date, release_num,
           body, sha, '待核验', stamp(), stamp())).lastrowid
        changed = True
    con.execute('INSERT OR IGNORE INTO official_revisions(document_id,sha256,body,captured_at) VALUES(?,?,?,?)',
               (docid, sha, body, stamp()))
    con.execute('INSERT OR REPLACE INTO official_assets(document_id,asset_url,sha256) VALUES(?,?,?)',
               (docid, asset_url, pdf_sha))
    con.commit()
    return changed


def _asset_unchanged(con, url, pdf_sha):
    row = con.execute('''SELECT 1 FROM official_assets a
      JOIN official_documents d ON d.id=a.document_id
      WHERE d.source_url=? AND a.sha256=?''', (url, pdf_sha)).fetchone()
    return row is not None


def collect(pages=1, page_size=10, delay=1.0, category='both'):
    con = sqlite3.connect(DB, timeout=30)
    con.execute('PRAGMA foreign_keys=ON')
    con.executescript(DDL)
    con.executescript('''CREATE TABLE IF NOT EXISTS official_assets (
      document_id INTEGER NOT NULL REFERENCES official_documents(id), asset_url TEXT NOT NULL,
      sha256 TEXT NOT NULL, PRIMARY KEY(document_id,asset_url));''')
    src = '广东省地方性法规数据库(省人大)'
    run = con.execute('INSERT INTO official_runs(source,started_at,pages) VALUES(?,?,?)',
                       (src, stamp(), pages)).lastrowid
    con.commit()
    cats = {'place': 1, 'gov': 2, 'both': [1, 2]}[category]
    if isinstance(cats, int):
        cats = [cats]
    candidates = inserted = updated = errors = 0
    ocr_total = ocr_ok = local_text = docx_text = skipped = unchanged = 0
    details = []
    try:
        for lrt in cats:
            for page in range(1, pages + 1):
                try:
                    rows = search_page(page, page_size, lrt)
                except Exception as e:
                    errors += 1
                    details.append(f'LIST lawRuleType={lrt} p{page}: {type(e).__name__}: {str(e)[:160]}')
                    break
                if not rows:
                    break
                for row in rows:
                    rid = (row.get('id') or '').strip()
                    title = html.unescape(row.get('title') or '').strip()
                    if not rid or not title:
                        continue
                    candidates += 1
                    url = f'{BYID}?id={rid}'          # stable official deep endpoint
                    office = (row.get('officeVo') or {}).get('groupName') or None
                    pub_date = row.get('passDate') or row.get('publishDate')
                    release_num = row.get('releaseNum') or None
                    try:
                        att = regulation_text_attachment(rid, title)
                        if not att:
                            raise ValueError('no 法规文本 attachment (pdf/docx)')
                        fp, ext = att
                        raw, pub = download_asset(fp)
                        sha_asset = hashlib.sha256(raw).hexdigest()
                        if _asset_unchanged(con, url, sha_asset):
                            unchanged += 1
                            con.execute('UPDATE official_documents SET last_seen_at=? WHERE source_url=?',
                                         (stamp(), url))
                            con.commit()
                            time.sleep(delay)
                            continue
                        body, ocr_mode = '', None
                        if ext == 'docx':
                            body, ocr_mode, docx_text = clean_body(extract_docx_text(raw)), 'docx', docx_text + 1
                        else:
                            if len(raw) > MINERU_MAX_BYTES:
                                raise ValueError(f'PDF exceeds MinerU size limit ({len(raw)} bytes)')
                            local, npages = extract_local_text(raw)
                            if local:
                                body, ocr_mode, local_text = clean_body(local), 'local', local_text + 1
                            else:
                                if npages > MINERU_MAX_PAGES:
                                    raise ValueError(f'over MinerU limit: {npages} pages')
                                ocr_total += 1
                                md, ocr_mode = mineru_ocr(raw, pub)
                                body = clean_body(md)
                                ocr_ok += 1
                        body = body.strip()
                        if len(body) < 150:
                            raise ValueError('extracted body too short')
                        sha = hashlib.sha256(body.encode()).hexdigest()
                        if upsert(con, url, title, body, sha, office, pub_date,
                                  release_num, pub, sha_asset,
                                  {1: '地方性法规', 2: '政府规章'}[lrt]):
                            inserted += 1
                        else:
                            updated += 1
                    except Exception as e:
                        msg = str(e)[:150]
                        if any(k in msg for k in ('too short', 'no 法规文本', 'over MinerU limit',
                                                  'OCR fail', 'MinerU')):
                            skipped += 1
                            details.append(f'SKIP {title[:24]}: {msg}')
                        else:
                            errors += 1
                            details.append(f'{title[:24]}: {msg}')
                    time.sleep(delay)
                if page < pages:
                    time.sleep(delay)
    except Exception as e:
        errors += 1
        details.append(f'RUN: {type(e).__name__}: {str(e)[:200]}')
    finally:
        con.execute('UPDATE official_runs SET finished_at=?,candidates=?,inserted=?,updated=?,errors=?,error_details=? WHERE id=?',
                    (stamp(), candidates, inserted, updated, errors,
                     json.dumps(details, ensure_ascii=False), run))
        con.commit()
        con.close()
    result = {'source': src, 'pages': pages, 'candidates': candidates,
              'inserted': inserted, 'updated': updated, 'unchanged': unchanged,
              'ocr_total': ocr_total, 'ocr_ok': ocr_ok,
              'ocr_success_rate': round(ocr_ok / ocr_total, 3) if ocr_total else None,
              'local_text': local_text, 'docx_text': docx_text,
              'skipped': skipped, 'errors': errors,
              'details': details[:6]}
    print(json.dumps(result, ensure_ascii=False))
    if errors:
        raise SystemExit(1)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--pages', type=int, default=1)
    p.add_argument('--page-size', type=int, default=10)
    p.add_argument('--delay', type=float, default=1.0)
    p.add_argument('--category', choices=['place', 'gov', 'both'], default='both')
    a = p.parse_args()
    if not 1 <= a.pages <= 10 or not 1 <= a.page_size <= 50 or a.delay < 1.0:
        p.error('pages 1..10; page-size 1..50; delay >=1.0s')
    collect(a.pages, a.page_size, a.delay, a.category)
