#!/usr/bin/env python3
"""Import LawRefBook/Laws snapshots into a provenance-aware SQLite corpus."""
import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

BASE = Path(__file__).resolve().parent
DB = BASE / 'laws.sqlite3'
OWNER = 'LawRefBook/Laws'
API = f'https://api.github.com/repos/{OWNER}/commits/master'
ZIP = 'https://ghfast.top/https://github.com/LawRefBook/Laws/archive/{commit}.zip'
UA = {'User-Agent': 'legal-corpus-sync/1.0', 'Accept': 'application/vnd.github+json'}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS documents (
 id INTEGER PRIMARY KEY, source_path TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
 jurisdiction TEXT NOT NULL, category TEXT NOT NULL, version_hint TEXT,
 revision_date_hint TEXT, body TEXT NOT NULL, sha256 TEXT NOT NULL,
 source_url TEXT NOT NULL, source_commit TEXT NOT NULL, official_url TEXT,
 validity TEXT NOT NULL DEFAULT '待核验' CHECK(validity IN ('待核验','有效','已废止','已失效')),
 validity_evidence_url TEXT, validity_checked_at TEXT,
 first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, present_upstream INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS document_revisions (
 id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES documents(id), sha256 TEXT NOT NULL,
 source_commit TEXT NOT NULL, captured_at TEXT NOT NULL, body TEXT NOT NULL,
 UNIQUE(document_id,sha256)
);
CREATE TABLE IF NOT EXISTS sync_runs (
 id INTEGER PRIMARY KEY, source_commit TEXT NOT NULL, started_at TEXT NOT NULL,
 completed_at TEXT, status TEXT NOT NULL, imported INTEGER NOT NULL DEFAULT 0,
 changed INTEGER NOT NULL DEFAULT 0, removed INTEGER NOT NULL DEFAULT 0, error TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(title, body, content='documents', content_rowid='id', tokenize='unicode61');
CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON documents BEGIN
 INSERT INTO documents_fts(rowid,title,body) VALUES(new.id,new.title,new.body); END;
CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON documents BEGIN
 INSERT INTO documents_fts(documents_fts,rowid,title,body) VALUES('delete',old.id,old.title,old.body); END;
CREATE TRIGGER IF NOT EXISTS docs_au AFTER UPDATE OF title,body ON documents BEGIN
 INSERT INTO documents_fts(documents_fts,rowid,title,body) VALUES('delete',old.id,old.title,old.body);
 INSERT INTO documents_fts(rowid,title,body) VALUES(new.id,new.title,new.body); END;
'''

def now():
 return datetime.now(timezone.utc).isoformat(timespec='seconds')

def metadata(path, text):
 parts = path.parts
 if parts[0] == 'DLC':
  jurisdiction = parts[1].removesuffix('地方法规') if len(parts)>1 else '待分类'
  category = '/'.join(parts[2:-1]) or '待分类'
 else:
  jurisdiction = '全国'
  category = '/'.join(parts[:-1]) or '待分类'
 title = next((m.group(1).strip() for line in text.splitlines()[:20] if (m := re.match(r'^#\s+(.+)',line)) and not line.startswith('##')),path.stem)
 date = re.search(r'\((\d{4}-\d{2}-\d{2})\)$',path.stem)
 return title, jurisdiction, category, date.group(1) if date else None

def import_tree(root, commit):
 root = Path(root)
 paths = sorted(p for p in root.rglob('*.md') if p.is_file() and p.name != '_index.md' and not any(x in p.parts for x in ('scripts','scrape','.github')) and p.name != 'README.md' and p.name != '法律法规模版.md')
 con = sqlite3.connect(DB)
 con.execute('PRAGMA foreign_keys=ON')
 con.executescript(SCHEMA)
 stamp = now(); added = changed = 0
 try:
  con.execute('BEGIN IMMEDIATE')
  run_id = con.execute('INSERT INTO sync_runs(source_commit,started_at,status) VALUES(?,?,?)',(commit,stamp,'running')).lastrowid
  con.execute('UPDATE documents SET present_upstream=0')
  for p in paths:
   relative = p.relative_to(root).as_posix()
   text = p.read_text(encoding='utf-8-sig')
   if not text.strip(): continue
   sha = hashlib.sha256(text.encode('utf-8')).hexdigest()
   title, region, category, hint = metadata(p.relative_to(root),text)
   url = f'https://github.com/{OWNER}/blob/{commit}/{quote(relative, safe="/")}'
   old = con.execute('SELECT id,sha256 FROM documents WHERE source_path=?',(relative,)).fetchone()
   if old:
    con.execute('''UPDATE documents SET title=?,jurisdiction=?,category=?,revision_date_hint=?,body=?,sha256=?,source_url=?,source_commit=?,last_seen_at=?,present_upstream=1,
     validity=CASE WHEN sha256<>? THEN '待核验' ELSE validity END,
     validity_evidence_url=CASE WHEN sha256<>? THEN NULL ELSE validity_evidence_url END,
     validity_checked_at=CASE WHEN sha256<>? THEN NULL ELSE validity_checked_at END
     WHERE id=?''',(title,region,category,hint,text,sha,url,commit,stamp,sha,sha,sha,old[0]))
    doc_id=old[0]
    if old[1]!=sha: changed+=1
   else:
    doc_id=con.execute('INSERT INTO documents(source_path,title,jurisdiction,category,version_hint,revision_date_hint,body,sha256,source_url,source_commit,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(relative,title,region,category,hint,hint,text,sha,url,commit,stamp,stamp)).lastrowid
    added+=1
   con.execute('INSERT OR IGNORE INTO document_revisions(document_id,sha256,source_commit,captured_at,body) VALUES(?,?,?,?,?)',(doc_id,sha,commit,stamp,text))
  removed=con.execute('SELECT count(*) FROM documents WHERE present_upstream=0').fetchone()[0]
  con.execute('UPDATE sync_runs SET completed_at=?,status=?,imported=?,changed=?,removed=? WHERE id=?',(now(),'ok',added,changed,removed,run_id))
  con.commit()
  return {'commit':commit,'files_read':len(paths),'new':added,'changed':changed,'not_in_latest':removed,'total':con.execute('SELECT count(*) FROM documents').fetchone()[0]}
 except Exception:
  con.rollback();raise
 finally: con.close()

def latest_commit():
 req=urllib.request.Request(API,headers=UA)
 with urllib.request.urlopen(req,timeout=30) as resp:
  return json.load(resp)['sha']

def current_commit():
 if not DB.exists():return None
 with sqlite3.connect(DB) as con:
  try:
   row=con.execute("SELECT source_commit FROM sync_runs WHERE status='ok' ORDER BY id DESC LIMIT 1").fetchone()
   return row[0] if row else None
  except sqlite3.OperationalError:return None

def download_and_import(commit):
 with tempfile.TemporaryDirectory(prefix='law-sync-') as tmp:
  archive=Path(tmp)/'source.zip'
  subprocess.run(['curl','-fLsS','--retry','5','--retry-delay','3','--connect-timeout','15','--max-time','1200','-o',str(archive),ZIP.format(commit=commit)],check=True)
  with zipfile.ZipFile(archive) as z:
   for entry in z.infolist():
    target=(Path(tmp)/entry.filename).resolve()
    if not target.is_relative_to(Path(tmp).resolve()):raise ValueError('unsafe ZIP member')
   z.extractall(tmp)
  roots=[p for p in Path(tmp).iterdir() if p.is_dir()]
  if len(roots)!=1:raise RuntimeError('unexpected archive layout')
  return import_tree(roots[0],commit)

if __name__=='__main__':
 parser=argparse.ArgumentParser()
 parser.add_argument('--local-root',type=Path,help='import existing extracted snapshot')
 parser.add_argument('--commit',help='exact commit for local snapshot')
 args=parser.parse_args()
 if args.local_root:
  if not args.commit:parser.error('--commit required with --local-root')
  print(json.dumps(import_tree(args.local_root,args.commit),ensure_ascii=False))
 else:
  commit=latest_commit()
  if current_commit()==commit:print(json.dumps({'status':'unchanged','commit':commit},ensure_ascii=False))
  else:print(json.dumps(download_and_import(commit),ensure_ascii=False))
