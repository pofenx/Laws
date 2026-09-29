#!/usr/bin/env python3
"""Bounded official Jiangsu provincial-regulations collector (省人大 省级法规).

Source: 江苏省人大常委会 官网 权威发布 / 省级法规 栏目 (www.jsrd.gov.cn/qwfb/sjfg/)
  - listing   GET https://www.jsrd.gov.cn/qwfb/sjfg/index.shtml      (page 0)
              GET https://www.jsrd.gov.cn/qwfb/sjfg/index_N.shtml   (N = 1..24)
      每页 15 条；每条带 标题 / 详情 href / 发布日期(.ptime)。
  - detail    GET 详情 shtml；正文取自官方详情页的 <div class="TRS_Editor"> 文本层。

只采集本栏目所列的“地方性法规”正文（含单行法规、单行“办法/规定”及修改/废止决定）。
正文一律取官网详情页原文，不用搜索摘要、不采“解读/目录/草案”。每条默认
validity='待核验'；重采时正文哈希变化才重置效力。限速且设上限
(pages<=20, 每页<=15, delay>=0.5s)，正文太短或无法识别正文的条目跳过并记录。
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
from pathlib import Path

DB = Path(__file__).resolve().parent / 'laws.sqlite3'
BASE = 'https://www.jsrd.gov.cn/qwfb/sjfg'
LIST = BASE + '/index.shtml'          # page 0
LIST_FMT = BASE + '/index_{}.shtml'   # page >=1
DOMAIN = 'www.jsrd.gov.cn'
JURISDICTION = '江苏'
CATEGORY = '地方性法规'
PUBLISHER = '江苏省人大常委会(江苏人大官网·省级法规)'
UA = 'Mozilla/5.0 (compatible; LegalCorpusResearch/1.0)'
MAX_ITEMS_PER_PAGE = 15
MIN_BODY = 300

# Reuse the official_documents / official_revisions / official_runs schema.
from official_gov import DDL, stamp  # noqa: E402

_ARTICLE = re.compile(r'第[0-9一二三四五六七八九十百千零两]+条')
_CHAPTER = re.compile(r'第[0-9一二三四五六七八九十百千零两]+[章节]')


def fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Referer': LIST})
    with urllib.request.urlopen(req, timeout=25) as r:
        if r.status != 200:
            raise ValueError(f'HTTP {r.status}')
        return r.read(3_000_001).decode('utf-8', 'replace')


def listing_url(page):
    return LIST if page == 0 else LIST_FMT.format(page)


def parse_listing(raw):
    """Return [(abs_detail_url, title, pub_date), ...] in DOM order."""
    body = raw[raw.find('<body'):]
    i = body.find('list_main')
    ul = body.find('<ul>', i)
    if ul < 0:
        return []
    ulend = body.find('</ul>', ul)
    items = []
    for m in re.finditer(r'<li>.*?</li>', body[ul:ulend], re.S):
        block = m.group(0)
        a = re.search(r'href="([^"]*t[0-9]+_[0-9]+\.shtml)"', block)
        if not a:
            continue
        href = a.group(1)
        t = re.search(r'class="title"><a[^>]*>(.*?)</a>', block, re.S)
        title = re.sub(r'<[^>]+>', '', t.group(1)).strip() if t else ''
        title = html.unescape(title)
        d = re.search(r'class="ptime">([^<]+)<', block)
        date = d.group(1).strip()[:10] if d else None
        items.append((urllib.parse.urljoin(LIST, href), title, date))
    return items


def detail_body(raw):
    """Return (title, body_text) from the official detail HTML (text layer only).

    Body comes from the <div class="TRS_Editor"> container; the 目录 (TOC) block
    is dropped so only the operative 第X条 text is kept. No snippets used.
    """
    m = re.search(r'<div\s+class="?TRS_Editor"?\s*>(.*?)</div>', raw, re.S)
    if not m:
        tm = re.search(r'<title[^>]*>(.*?)</title>', raw, re.S)
        title = html.unescape(tm.group(1)).strip() if tm else ''
        return title, ''
    t = m.group(1)
    t = re.sub(r'<(script|style).*?</\1>', '', t, flags=re.S | re.I)
    t = re.sub(r'</(p|div|li|tr|h\d|table)>', '\n', t, flags=re.I)
    t = re.sub(r'<br\s*/?>', '\n', t, flags=re.I)
    t = re.sub(r'<[^>]+>', '', t)
    t = html.unescape(t)
    lines = [re.sub(r'[ \t]+', ' ', ln).strip() for ln in t.splitlines()]
    lines = [ln for ln in lines if ln]
    # Drop the 目录 (TOC) chapter list: keep the preface, then the operative text
    # from the real first chapter heading (the nearest 第X章 before 第一条).
    ai = next((k for k, ln in enumerate(lines) if '第一条' in ln), None)
    if ai is not None:
        di = next((k for k, ln in enumerate(lines) if '目' in ln and '录' in ln and k < ai), None)
        ci = None
        for k in range(ai - 1, -1, -1):
            if _CHAPTER.search(lines[k]):
                ci = k
                break
        if di is not None and (ci is None or di < ci):
            lines = lines[:di] + (lines[ci:] if ci is not None else [])
    tm = re.search(r'<title[^>]*>(.*?)</title>', raw, re.S)
    title = html.unescape(tm.group(1)).strip() if tm else ''
    return title, '\n'.join(lines).strip()


def clean_title(title):
    # Drop the trailing site suffix "_江苏人大" that the <title> tag appends.
    title = re.sub(r'[_－-]?江苏人大$', '', title).strip()
    return title


def pub_date(raw):
    m = re.search(r'class="ptime"[^>]*>([^<]+)<', raw)
    return m.group(1).strip()[:10] if m else None


def collect(pages=2, delay=1.0, max_items=20):
    con = sqlite3.connect(DB, timeout=30)
    con.execute('PRAGMA foreign_keys=ON')
    con.executescript(DDL)
    ts = stamp()
    run = con.execute('INSERT INTO official_runs(source,started_at,pages) VALUES(?,?,?)',
                      ('江苏省人大省级法规', ts, pages)).lastrowid
    con.commit()
    candidates = inserted = updated = errors = 0
    details = []
    seen = set()
    try:
        for page in range(pages):
            if candidates >= max_items:
                break
            raw = fetch(listing_url(page))
            rows = parse_listing(raw)
            if not rows:
                raise ValueError(f'empty or unparseable listing: {listing_url(page)}')
            for url, title, date in rows:
                if candidates >= max_items:
                    break
                if url in seen or not title:
                    continue
                parsed = urllib.parse.urlparse(url)
                if parsed.scheme != 'https' or parsed.hostname != DOMAIN or not parsed.path.startswith('/qwfb/sjfg/') or not parsed.path.endswith('.shtml'):
                    errors += 1
                    details.append(f'unexpected detail URL: {url}')
                    continue
                seen.add(url)
                try:
                    d = fetch(url)
                    dt, body = detail_body(d)
                    if len(body) < MIN_BODY or not _ARTICLE.search(body):
                        raise ValueError('official article body missing/short')
                    sha = hashlib.sha256(body.encode()).hexdigest()
                    title = clean_title(html.unescape(re.sub(r'\s+', ' ', dt or title)))
                    pub = pub_date(d) or date
                    old = con.execute('SELECT id, sha256 FROM official_documents WHERE source_url=?',
                                       (url,)).fetchone()
                    if old:
                        con.execute('''UPDATE official_documents SET title=?,body=?,sha256=?,
                          publisher=?,publication_date=?,last_seen_at=?,
                          validity=CASE WHEN sha256<>? THEN '待核验' ELSE validity END,
                          validity_evidence_url=CASE WHEN sha256<>? THEN NULL ELSE validity_evidence_url END,
                          validity_checked_at=CASE WHEN sha256<>? THEN NULL ELSE validity_checked_at END
                          WHERE id=?''',
                          (title, body, sha, PUBLISHER, pub,
                           stamp(), sha, sha, sha, old[0]))
                        docid = old[0]
                        if sha != old[1]:
                            updated += 1
                    else:
                        docid = con.execute('''INSERT INTO official_documents
                          (source_url,source_domain,title,jurisdiction,category,publisher,
                           publication_date,document_number,body,sha256,validity,first_seen_at,last_seen_at)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                          (url, DOMAIN, title, JURISDICTION, CATEGORY, PUBLISHER,
                           pub, None, body, sha, '待核验', stamp(), stamp())).lastrowid
                        inserted += 1
                    con.execute('INSERT OR IGNORE INTO official_revisions(document_id,sha256,body,captured_at) VALUES(?,?,?,?)',
                                (docid, sha, body, stamp()))
                    con.commit()
                    candidates += 1
                except Exception as e:
                    errors += 1
                    details.append(f'{url}: {str(e)[:160]}')
                time.sleep(delay)
            if page < pages - 1:
                time.sleep(delay)
    except Exception as e:
        errors += 1
        details.append(f'LIST: {type(e).__name__}: {str(e)[:200]}')
    finally:
        con.execute('UPDATE official_runs SET finished_at=?,candidates=?,inserted=?,updated=?,errors=?,error_details=? WHERE id=?',
                    (stamp(), candidates, inserted, updated, errors, json.dumps(details, ensure_ascii=False), run))
        con.commit()
        con.close()
    result = {'source': '江苏省人大省级法规', 'pages': pages, 'candidates': candidates,
              'inserted': inserted, 'updated': updated, 'errors': errors, 'details': details[:5]}
    print(json.dumps(result, ensure_ascii=False))
    if errors:
        raise SystemExit(1)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--pages', type=int, default=2)
    p.add_argument('--max-items', type=int, default=20)
    p.add_argument('--delay', type=float, default=1.0)
    p.add_argument('--test', action='store_true', help='tiny bounded run (1 page, <=3 items)')
    a = p.parse_args()
    if a.test:
        pages, max_items, delay = 1, 3, 0.5
    else:
        pages, max_items, delay = a.pages, a.max_items, a.delay
    if not 1 <= pages <= 20 or not 1 <= max_items <= 20 or delay < 0.5:
        p.error('pages 1..20; max-items 1..20; delay >=0.5s')
    collect(pages, delay, max_items)
