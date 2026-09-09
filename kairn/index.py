"""ワークスペース索引（SQLite FTS5）。ローカルの写しから再生成できる派生物。

- sections: worklog*.md / *.md を `## ` 見出し単位に分割して全文検索
- cases:    case.json の title / tickets / related / elements を検索用に平坦化

索引しないもの（同期と同じ規則）: シンボリックリンク、rules.exclude のディレクトリ・ファイル（既定は config.DEFAULT_RULES。
`.git/**` を含む。Index(exclude=…) に conf.rules["exclude"] を渡す）、作業領域（直下に .git ファイル / .kairn-nosync がある
ディレクトリ。sync.is_workarea）の配下。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

from .config import DEFAULT_RULES
from .store import CaseStore
from .sync import excluded_dir, excluded_file, is_workarea

SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS sections USING fts5(case_id, file, heading, body, tokenize='trigram');
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, mtime REAL);
CREATE VIRTUAL TABLE IF NOT EXISTS cases USING fts5(case_id, title, status, tickets, related, elements, tokenize='trigram');
"""


def split_sections(text: str) -> list[tuple[str, str]]:
    parts = re.split(r"(?m)^(?=## )", text)
    out = []
    for p in parts:
        if not p.strip():
            continue
        m = re.match(r"^## (.+)$", p, re.M)
        out.append((m.group(1).strip() if m else "(先頭)", p))
    return out


class Index:
    def __init__(self, index_dir: Path, cases_dir: Path, exclude: list[str] | None = None):
        index_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(index_dir / "kairn.sqlite")
        self.db.executescript(SCHEMA)
        self.cases_dir = cases_dir
        self.store = CaseStore(cases_dir)
        self.exclude = [str(x) for x in (DEFAULT_RULES["exclude"] if exclude is None else exclude)]

    def _md_files(self):
        """索引対象の md。シンボリックリンク（同期対象外・壊れていることがある）、rules.exclude のディレクトリ（target/** 等）・
        ファイル、作業領域（直下に .git ファイル / .kairn-nosync）の配下は除く（同期のフィルタと同じ規則）。"""
        if not self.cases_dir.exists():
            return
        for root, dirs, files in os.walk(self.cases_dir):
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d)) and not excluded_dir(d, self.exclude)
                       and not is_workarea(Path(root) / d)]
            for fn in files:
                if fn.endswith(".md") and not os.path.islink(os.path.join(root, fn)) and not excluded_file(fn, self.exclude):
                    yield Path(root) / fn

    def rebuild(self, full: bool = False) -> dict:
        """変更のあった md だけ再索引（full=True で全部）。"""
        n_files = 0
        known = dict(self.db.execute("SELECT path, mtime FROM files"))
        seen = set()
        for md in self._md_files():
            rel = md.relative_to(self.cases_dir).as_posix(); seen.add(rel)
            try:
                mtime = md.stat().st_mtime
            except OSError:  # 消えた・読めない
                continue
            if not full and known.get(rel) == mtime:
                continue
            case_id = rel.split("/")[0]
            self.db.execute("DELETE FROM sections WHERE file=?", (rel,))
            try:
                text = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for heading, body in split_sections(text):
                self.db.execute("INSERT INTO sections(case_id, file, heading, body) VALUES (?,?,?,?)", (case_id, rel, heading, body))
            self.db.execute("INSERT OR REPLACE INTO files(path, mtime) VALUES (?,?)", (rel, mtime))
            n_files += 1
        for rel in set(known) - seen:  # 消えたファイル
            self.db.execute("DELETE FROM sections WHERE file=?", (rel,))
            self.db.execute("DELETE FROM files WHERE path=?", (rel,))
        self.db.execute("DELETE FROM cases")
        for cid in self.store.list_case_ids():
            c = self.store.load_case(cid)
            el = c.get("elements") or {}
            self.db.execute("INSERT INTO cases VALUES (?,?,?,?,?,?)", (cid, c.get("title", ""), c.get("status", ""),
                            " ".join(map(str, c.get("tickets", []))), " ".join(c.get("related", [])),
                            " ".join(v for vs in el.values() for v in vs)))
        self.db.commit()
        return {"files_indexed": n_files, "cases": len(self.store.list_case_ids())}

    @staticmethod
    def _terms(q: str) -> list[str]:
        # 記号で分割（句読点をクエリ構文に渡さない）。日本語は分かち書きされないので trigram で部分一致させる
        return [w for w in re.split(r"[\s　,、。・/()（）\[\]{}\"'`:;!?]+", q) if w]

    @classmethod
    def _fts_query(cls, q: str, op: str = "AND") -> tuple[str, list[str]]:
        """(FTS MATCH 式, 3 文字未満の語) — trigram は 3 文字以上しか索引しないので短語は後で絞る。
        各語は引用符で囲む（記号や AND/OR/NOT/NEAR をクエリ構文として解釈させない）。"""
        words = cls._terms(q)
        long_ = [w for w in words if len(w) >= 3]
        short = [w for w in words if len(w) < 3]
        return (f" {op} ".join('"' + w.replace('"', '""') + '"' for w in long_) if long_ else ""), short

    def search_sections(self, query: str, cases: list[str] | None = None, limit: int = 10) -> list[dict]:
        match, short = self._fts_query(query)
        if not match and not short:
            return []
        if match:
            sql = "SELECT case_id, file, heading, body, snippet(sections, 3, '[', ']', '…', 24), bm25(sections) FROM sections WHERE sections MATCH ?"
            args: list = [match]
        else:  # 短語だけ（trigram は 3 文字未満を索引しない）: 本文と案件 ID を LIKE で走査
            sql = "SELECT case_id, file, heading, body, substr(body, 1, 160), 0 FROM sections WHERE 1=1"
            args = []
            for w in short:
                sql += " AND (body LIKE ? OR case_id LIKE ?)"; args += [f"%{w}%", f"%{w}%"]
        if cases:
            sql += " AND case_id IN (%s)" % ",".join("?" * len(cases)); args += cases
        sql += " ORDER BY 6 LIMIT ?"; args.append(limit * 4 if short else limit)
        rows = []
        for r in self.db.execute(sql, args):
            if match and short and not all(w.lower() in r[3].lower() for w in short):
                continue
            rows.append({"case": r[0], "file": r[1], "heading": r[2], "snippet": r[4], "score": round(-r[5], 3)})
            if len(rows) >= limit:
                break
        return rows

    def section_text(self, file: str, heading: str) -> str | None:
        r = self.db.execute("SELECT body FROM sections WHERE file=? AND heading=? LIMIT 1", (file, heading)).fetchone()
        return r[0] if r else None

    def find_cases(self, query: str, k: int = 5) -> list[dict]:
        """case.json（title/tickets/related/elements）と本文の両方から案件を採点。理由付き。
        search_sections と違い語は OR で結ぶ（問いの一部にでも当たる案件を拾い、bm25 で順位付け）。"""
        match, short = self._fts_query(query, op="OR")
        scores: dict[str, float] = {}; reasons: dict[str, list[str]] = {}
        if not match:
            if not short:
                return []
            # 短語だけ（trigram は 3 文字未満を索引しない）: 案件 ID / title の LIKE で補う
            sql = "SELECT case_id FROM cases WHERE " + " OR ".join("case_id LIKE ? OR title LIKE ?" for _ in short)
            args = [x for w in short for x in (f"%{w}%", f"%{w}%")]
            for (cid,) in self.db.execute(sql + " LIMIT 50", args):
                scores[cid] = scores.get(cid, 0) + 1.0; reasons.setdefault(cid, []).append("案件 ID / title に部分一致")
            ranked = sorted(scores.items(), key=lambda x: (-x[1], x[0]))[:k]
            return [{"case": cid, "score": round(s, 3), "reasons": reasons.get(cid, [])} for cid, s in ranked]
        for cid, title, score in self.db.execute("SELECT case_id, title, bm25(cases) FROM cases WHERE cases MATCH ? LIMIT 50", (match,)):
            scores[cid] = scores.get(cid, 0) + 3.0 * -score; reasons.setdefault(cid, []).append("案件カードに一致")
        for cid, heading, score in self.db.execute("SELECT case_id, heading, bm25(sections) FROM sections WHERE sections MATCH ? LIMIT 200", (match,)):
            scores[cid] = scores.get(cid, 0) + -score
            if len(reasons.setdefault(cid, [])) < 4:
                reasons[cid].append(f"節「{heading[:40]}」")
        ranked = sorted(scores.items(), key=lambda x: -x[1])[:k]
        return [{"case": cid, "score": round(s, 3), "reasons": reasons.get(cid, [])} for cid, s in ranked]
