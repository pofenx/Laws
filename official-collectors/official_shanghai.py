#!/usr/bin/env python3
"""Collect bounded Shanghai local regulations from an official bureau list."""
import hashlib
import io
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from lxml import html
import pymupdf
from official_gov import DDL

DB=Path(__file__).resolve().parent/'laws.sqlite3'
BASE='https://cgzf.sh.gov.cn'
LIST=BASE+'/channel_89/index.html'
HEADERS={'User-Agent':'Mozilla/5.0 (compatible; LegalCorpusResearch/1.0)'}

def stamp():return datetime.now(timezone.utc).isoformat(timespec='seconds')
def get(url,limit=8_000_000):
 if urlparse(url).hostname!='cgzf.sh.gov.cn' or not url.startswith('https://'):raise ValueError('unapproved host')
 with urlopen(Request(url,headers=HEADERS),timeout=25) as r:
  b=r.read(limit+1)
  if len(b)>limit:raise ValueError('oversized resource')
  return b

def collect(max_items=20,delay=1):
 con=sqlite3.connect(DB,timeout=30);con.execute('PRAGMA foreign_keys=ON');con.executescript(DDL)
 con.executescript('''CREATE TABLE IF NOT EXISTS official_assets (
 document_id INTEGER NOT NULL REFERENCES official_documents(id), asset_url TEXT NOT NULL,
 sha256 TEXT NOT NULL, PRIMARY KEY(document_id,asset_url));''')
 rid=con.execute('INSERT INTO official_runs(source,started_at,pages) VALUES(?,?,?)',('上海市城管执法局地方性法规栏目',stamp(),1)).lastrowid;con.commit()
 inserted=updated=errors=skipped=0;details=[];candidates=0
 try:
  tree=html.fromstring(get(LIST))
  anchors=tree.xpath('//a[contains(@href,"/channel_89/") and @title]')
  seen=set()
  for a in anchors:
   url=urljoin(BASE,a.get('href'))
   if url in seen:continue
   seen.add(url)
   if candidates>=max_items:break
   title=a.get('title','').strip()
   if not title or not re.search(r'(条例|规定|办法)(（\d{4}）)?$',title):continue
   candidates+=1
   try:
    page=html.fromstring(get(url))
    text_nodes=page.xpath('//*[@id="ivs_content"]')
    body='\n'.join(text_nodes[0].itertext()).strip() if text_nodes else ''
    assets=[]
    if len(body)<150:
     links=page.xpath('//a[contains(@href,".pdf")]/@href')
     if not links:raise ValueError('no article text or PDF')
     asset=urljoin(BASE,links[0]);raw=get(asset);pdf=pymupdf.open(stream=raw,filetype='pdf')
     pages=[p.get_text(sort=True).strip() for p in pdf]
     if not pages or sum(len(x)<40 for x in pages)>len(pages)//3:raise ValueError('PDF text extraction incomplete; requires OCR')
     body='\n\n'.join(pages)
     assets.append((asset,hashlib.sha256(raw).hexdigest()))
    body=re.sub(r'\n[ \t]*\n+', '\n\n',body).strip()
    if len(body)<300:raise ValueError('body too short')
    sha=hashlib.sha256(body.encode()).hexdigest()
    old=con.execute('SELECT id,sha256 FROM official_documents WHERE source_url=?',(url,)).fetchone()
    if old:
     did=old[0]
     con.execute('''UPDATE official_documents SET title=?,body=?,sha256=?,last_seen_at=?,
      validity=CASE WHEN sha256<>? THEN '待核验' ELSE validity END,
      validity_evidence_url=CASE WHEN sha256<>? THEN NULL ELSE validity_evidence_url END,
      validity_checked_at=CASE WHEN sha256<>? THEN NULL ELSE validity_checked_at END WHERE id=?''',(title,body,sha,stamp(),sha,sha,sha,did))
     if old[1]!=sha:updated+=1
    else:
     did=con.execute('''INSERT INTO official_documents(source_url,source_domain,title,jurisdiction,category,publisher,body,sha256,first_seen_at,last_seen_at)
      VALUES(?,?,?,?,?,?,?,?,?,?)''',(url,'cgzf.sh.gov.cn',title,'上海','地方性法规','上海市城市管理行政执法局',body,sha,stamp(),stamp())).lastrowid
     inserted+=1
    con.execute('INSERT OR IGNORE INTO official_revisions(document_id,sha256,body,captured_at) VALUES(?,?,?,?)',(did,sha,body,stamp()))
    for asset,asset_sha in assets:con.execute('INSERT OR REPLACE INTO official_assets(document_id,asset_url,sha256) VALUES(?,?,?)',(did,asset,asset_sha))
    con.commit()
   except ValueError as e:
    if str(e) in ('no article text or PDF','PDF text extraction incomplete; requires OCR','body too short'):
     skipped+=1;details.append(f'SKIP {url}: {e}')
    else:
     errors+=1;details.append(f'{url}: {str(e)[:150]}')
   except Exception as e:
    errors+=1;details.append(f'{url}: {str(e)[:150]}')
   time.sleep(delay)
 except Exception as e:
  errors+=1;details.append(f'LIST: {e}')
 finally:
  con.execute('UPDATE official_runs SET finished_at=?,candidates=?,inserted=?,updated=?,errors=?,error_details=? WHERE id=?',(stamp(),candidates,inserted,updated,errors,json.dumps(details,ensure_ascii=False),rid));con.commit();con.close()
 print(json.dumps({'source':LIST,'candidates':candidates,'inserted':inserted,'updated':updated,'skipped':skipped,'errors':errors,'details':details[:5]},ensure_ascii=False))
 if errors:raise SystemExit(1)

if __name__=='__main__':collect()
