"""Independent in-memory rename/optional-trigger experiment, SQLite version recorded."""

import json
import sqlite3
from pathlib import Path

out = {"sqlite": sqlite3.sqlite_version, "storage": ":memory:"}
db = sqlite3.connect(":memory:")
db.execute(
    "create virtual table search_build using fts5(tokens,content='',"
    "contentless_delete=1,detail=none,tokenize='ascii')"
)
db.execute("insert into search_build(rowid,tokens) values(7,'s000g1abc')")
db.commit()
db.execute("begin immediate")
db.execute("alter table search_build rename to search_live")
db.commit()
out["rename_queries"] = db.execute(
    "select rowid from search_live where search_live match 's000g1abc'"
).fetchall()
out["objects_after_rename"] = [
    r[0]
    for r in db.execute("select name from sqlite_master where name like 'search_%' order by name")
]
db.execute("create table ev(id integer primary key,txt text)")
db.execute(
    "create trigger ad after delete on ev when exists("
    "select 1 from sqlite_master where name='search_live') "
    "begin delete from search_live where rowid=old.id; end"
)
db.execute("insert into ev values(7,'body')")
db.execute("drop table search_live")
try:
    db.execute("delete from ev where id=7")
except sqlite3.Error as exc:
    out["optional_trigger_table_missing_delete_error"] = str(exc)
Path(__file__).with_suffix(".json").write_text(json.dumps(out, indent=2), encoding="utf-8")
print(json.dumps(out, indent=2))
