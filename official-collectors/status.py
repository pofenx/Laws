#!/usr/bin/env python3
"""Inspect official corpus and candidate-source coverage."""
import json
import sqlite3
from pathlib import Path
DB=Path(__file__).resolve().parent/'laws.sqlite3'
with sqlite3.connect(DB) as con:
 con.executescript('''CREATE TABLE IF NOT EXISTS source_candidates (
  source_url TEXT PRIMARY KEY, jurisdiction TEXT NOT NULL, publisher TEXT NOT NULL,
  discovered_at TEXT NOT NULL, verified_at TEXT, status TEXT NOT NULL DEFAULT '待接入',
  notes TEXT NOT NULL DEFAULT ''
 );''')
 print(json.dumps({'reference':con.execute('SELECT count(*) FROM documents').fetchone()[0],
  'official':con.execute('SELECT jurisdiction,count(*) FROM official_documents GROUP BY jurisdiction').fetchall(),
  'pending_sources':con.execute('SELECT count(*) FROM source_candidates WHERE status="待接入"').fetchone()[0],
  'last_runs':con.execute('SELECT source,finished_at,inserted,updated,errors FROM official_runs ORDER BY id DESC LIMIT 4').fetchall()},ensure_ascii=False))
