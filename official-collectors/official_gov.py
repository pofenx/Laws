#!/usr/bin/env python3
"""Bounded official gov.cn policy-library collector (regulations only)."""
import argparse
import hashlib
import html
from html.parser import HTMLParser
import json
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DB = Path(__file__).resolve().parent / 'laws.sqlite3'
SEARCH = 'https://sousuo.www.gov.cn/search-gov/data'
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; LegalCorpusResearch/1.0)', 'Referer': 'https://sousuo.www.gov.cn/zcwjk/policyDocumentLibrary?t=zhengcelibrary_gw'}
DDL = '''
CREATE TABLE IF NOT EXISTS official_documents (
 id INTEGER PRIMARY KEY, source_url TEXT NOT NULL UNIQUE, source_domain TEXT NOT NULL,
 title TEXT NOT NULL, jurisdiction TEXT NOT NULL, category TEXT NOT NULL,
 publisher TEXT, publication_date TEXT, document_number TEXT,
 body TEXT NOT NULL, sha256 TEXT NOT NULL,
 validity TEXT NOT NULL DEFAULT '待核验' CHECK(validity IN ('待核验','有效','已废止','已失效')),
 validity_evidence_url TEXT, validity_checked_at TEXT,
 first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS official_revisions (
 id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES official_documents(id),
 sha256 TEXT NOT NULL, body TEXT NOT NULL, captured_at TEXT NOT NULL,
 UNIQUE(document_id,sha256)
);
CREATE TABLE IF NOT EXISTS official_runs (
 id INTEGER PRIMARY KEY, source TEXT NOT NULL, started_at TEXT NOT NULL,
 finished_at TEXT, pages INTEGER NOT NULL, candidates INTEGER NOT NULL DEFAULT 0,
 inserted INTEGER NOT NULL DEFAULT 0, updated INTEGER NOT NULL DEFAULT 0,
 errors INTEGER NOT NULL DEFAULT 0, error_details TEXT
);
'''

class Content(HTMLParser):
 def __init__(self):
  super().__init__(convert_charrefs=True)
  self.active=False; self.depth=0; self.skip=0; self.chunks=[];self.found=False
 def handle_starttag(self,tag,attrs):
  attrs=dict(attrs)
  if not self.active and attrs.get('id')=='UCAP-CONTENT':
   self.active=True;self.depth=1;self.found=True;return
  if not self.active:return
  if tag in ('script','style'):self.skip+=1
  if tag in ('div','section','article'):self.depth+=1
  if tag in ('br','p','tr','li','h1','h2','h3'):self.chunks.append('\n')
 def handle_endtag(self,tag):
  if not self.active:return
  if tag in ('script','style') and self.skip:self.skip-=1
  if tag in ('p','tr','li','h1','h2','h3'):self.chunks.append('\n')
  if tag in ('div','section','article'):
   self.depth-=1
   if not self.depth:self.active=False
 def handle_data(self,data):
  if self.active and not self.skip:self.chunks.append(data)
 def text(self):
  return '\n'.join(x.strip() for x in re.split(r'\n+',html.unescape(''.join(self.chunks))) if x.strip())

def stamp():return datetime.now(timezone.utc).isoformat(timespec='seconds')

def fetch(url):
 with urllib.request.urlopen(urllib.request.Request(url,headers=HEADERS),timeout=25) as r:
  if r.status!=200:raise ValueError(f'HTTP {r.status}')
  return r.read(3_000_001)

def listing(page,n=20):
 q={'t':'zhengcelibrary_gw','q':'','sort':'date','p':page,'n':n,'type':'gwyzcwjk'}
 d=json.loads(fetch(SEARCH+'?'+urllib.parse.urlencode(q)))
 if d.get('code')!=200:raise ValueError(f'list error: {d.get("code")} {d.get("msg")}')
 return d['searchVO']['catMap']['gongwen']['listVO']

def is_regulation(row):
 title=html.unescape(re.sub('<[^>]*>','',row.get('title','')))
 # Only direct promulgated regulations, not policy notices or interpretation pages.
 return bool(re.search(r'(条例|办法|规定|细则|规章)$',title) or
  (re.search(r'(修改|废止).*?(行政法规|规章|条例|办法|规定).*?决定$',title)))

def collect(pages=2,delay=1):
 con=sqlite3.connect(DB,timeout=30)
 con.execute('PRAGMA foreign_keys=ON');con.executescript(DDL)
 ts=stamp();run=con.execute('INSERT INTO official_runs(source,started_at,pages) VALUES(?,?,?)',('国务院政策文件库',ts,pages)).lastrowid;con.commit()
 candidates=inserted=updated=errors=0;details=[]
 try:
  for page in range(1,pages+1):
   for row in listing(page):
    if not is_regulation(row):continue
    candidates+=1;url=row.get('url','')
    parsed=urllib.parse.urlparse(url)
    if parsed.scheme!='https' or parsed.hostname!='www.gov.cn':continue
    try:
     parser=Content();parser.feed(fetch(url).decode('utf-8','replace'))
     body=parser.text()
     if not parser.found or len(body)<150:raise ValueError('missing/short official article body')
     sha=hashlib.sha256(body.encode()).hexdigest()
     old=con.execute('SELECT id,sha256 FROM official_documents WHERE source_url=?',(url,)).fetchone()
     title=html.unescape(re.sub('<[^>]*>','',row.get('title',''))).strip()
     if old:
      con.execute('''UPDATE official_documents SET title=?,body=?,sha256=?,publisher=?,publication_date=?,document_number=?,last_seen_at=?,
       validity=CASE WHEN sha256<>? THEN '待核验' ELSE validity END,
       validity_evidence_url=CASE WHEN sha256<>? THEN NULL ELSE validity_evidence_url END,
       validity_checked_at=CASE WHEN sha256<>? THEN NULL ELSE validity_checked_at END WHERE id=?''',
       (title,body,sha,row.get('puborg'),row.get('pubtimeStr'),row.get('pcode'),stamp(),sha,sha,sha,old[0]))
      docid=old[0]
      if sha!=old[1]:updated+=1
     else:
      docid=con.execute('''INSERT INTO official_documents(source_url,source_domain,title,jurisdiction,category,publisher,publication_date,document_number,body,sha256,first_seen_at,last_seen_at)
       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',(url,'www.gov.cn',title,'全国','国务院政策文件库',row.get('puborg'),row.get('pubtimeStr'),row.get('pcode'),body,sha,stamp(),stamp())).lastrowid
      inserted+=1
     con.execute('INSERT OR IGNORE INTO official_revisions(document_id,sha256,body,captured_at) VALUES(?,?,?,?)',(docid,sha,body,stamp()))
     con.commit()
    except Exception as e:
     errors+=1;details.append(f'{url}: {str(e)[:160]}')
    time.sleep(delay)
   if page<pages:time.sleep(delay)
 except Exception as e:
  errors+=1;details.append(f'LIST: {e}')
 finally:
  con.execute('UPDATE official_runs SET finished_at=?,candidates=?,inserted=?,updated=?,errors=?,error_details=? WHERE id=?',(stamp(),candidates,inserted,updated,errors,json.dumps(details,ensure_ascii=False),run));con.commit();con.close()
 result={'source':'国务院政策文件库','pages':pages,'candidates':candidates,'inserted':inserted,'updated':updated,'errors':errors,'details':details[:5]}
 print(json.dumps(result,ensure_ascii=False))
 if errors:raise SystemExit(1)

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--pages',type=int,default=2);p.add_argument('--delay',type=float,default=1)
 a=p.parse_args()
 if not 1<=a.pages<=10 or a.delay<0.5:p.error('pages 1..10; delay >=0.5s')
 collect(a.pages,a.delay)
