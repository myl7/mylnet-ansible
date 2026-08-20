#!/usr/bin/env python3
"""Merge the CTU uptime-kuma database into the repo-managed instance.

Source: /opt/uptime-kuma/data/kuma.db (CTU status page, manual deploy)
Target: /srv/uptime/data/kuma.db (repo-managed, uptime.myl.moe)

Copies monitors, groups, the status page and heartbeats in a single
transaction. Monitor ids get +6 so they do not clash with the target's
existing ids 1-2. Groups are renamed with a CityU prefix. Dry-run by
default; run with --apply to write.
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timezone

SRC_DB = "/opt/uptime-kuma/data/kuma.db"
DST_DB = "/srv/uptime/data/kuma.db"
SNAP_DIR = "/opt/stacks/uptime/backups"

MONITOR_OFFSET = 6
GROUP_RENAME = {
    "CS 服务器": "CityU · CS 服务器",
    "探针机与参照": "CityU · 参照",
}
# Tables copied verbatim (schema is identical across both DBs).
COPY_TABLES = ["monitor", "monitor_group", "status_page", "group", "heartbeat"]


def connect(path, ro=False):
    mode = "ro" if ro else "rw"
    conn = sqlite3.connect(f"file:{path}?mode={mode}", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def snapshot(path, label):
    """Consistent snapshot of a live sqlite db via the backup API."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = f"{SNAP_DIR}/{label}-{stamp}.sqlite"
    with connect(path, ro=True) as src, sqlite3.connect(dest) as dst:
        src.backup(dst)
    return dest


def table_columns(conn, table):
    return [
        row["name"]
        for row in conn.execute(f'PRAGMA table_info("{table}")')
    ]


def read_table(conn, table):
    cols = table_columns(conn, table)
    rows = [dict(r) for r in conn.execute(f'SELECT * FROM "{table}"')]
    return cols, rows


def table_sequence(conn, table):
    row = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name=?", (table,)
    ).fetchone()
    return row["seq"] if row else None


def load_source():
    data = {}
    with connect(SRC_DB, ro=True) as src:
        for table in COPY_TABLES:
            data[table] = read_table(src, table)
        data["sequence"] = {t: table_sequence(src, t) for t in COPY_TABLES}
    return data


def check_target_guards(conn):
    errors = []
    if table_sequence(conn, "monitor") and table_sequence(conn, "monitor") >= 7:
        errors.append("target monitor sequence already reaches the remapped ids")
    for table in ["group", "status_page"]:
        _, rows = read_table(conn, table)
        if rows:
            errors.append(f"target table {table} is not empty")
    row = conn.execute(
        "SELECT id FROM status_page WHERE slug='cs-cluster'"
    ).fetchone()
    if row:
        errors.append("target already has a status page with slug cs-cluster")
    return errors


def plan_summary(src, dst_seq):
    cols, monitors = src["monitor"]
    _, groups = src["group"]
    _, monitor_group = src["monitor_group"]
    _, status_pages = src["status_page"]
    _, heartbeats = src["heartbeat"]
    lines = [
        f"source monitors: {len(monitors)} -> target ids "
        f"{[m['id'] + MONITOR_OFFSET for m in monitors]}",
        f"source groups: {len(groups)} (target has none)",
        f"source monitor_group rows: {len(monitor_group)}",
        f"source status pages: {len(status_pages)} "
        f"slug={status_pages[0]['slug'] if status_pages else None}",
        f"source heartbeats: {len(heartbeats)} (target has {dst_seq.get('heartbeat', 0)})",
        f"group renames: {GROUP_RENAME}",
    ]
    return "\n".join(lines)


def apply(src, dst_conn):
    mcols, monitors = src["monitor"]
    gcols, groups = src["group"]
    _, monitor_group = src["monitor_group"]
    spcols, status_pages = src["status_page"]
    _, heartbeats = src["heartbeat"]

    # Remap groups: rename and keep ids 1-2 (target has none).
    group_id_map = {}
    for row in groups:
        new_name = GROUP_RENAME.get(row["name"], row["name"])
        group_id_map[row["id"]] = row["id"]
        dst_conn.execute(
            f"INSERT INTO `group` ({', '.join(gcols)}) VALUES ({', '.join('?' * len(gcols))})",
            [new_name if k == "name" else row[k] for k in gcols],
        )

    # Remap status page: keep id 1 (target has none).
    for row in status_pages:
        dst_conn.execute(
            f"INSERT INTO status_page ({', '.join(spcols)}) VALUES ({', '.join('?' * len(spcols))})",
            [row[k] for k in spcols],
        )

    # Remap monitors: +6 to avoid target ids 1-2.
    monitor_id_map = {}
    for row in monitors:
        new_id = row["id"] + MONITOR_OFFSET
        monitor_id_map[row["id"]] = new_id
        dst_conn.execute(
            f"INSERT INTO monitor ({', '.join(mcols)}) VALUES ({', '.join('?' * len(mcols))})",
            [new_id if k == "id" else row[k] for k in mcols],
        )

    for row in monitor_group:
        dst_conn.execute(
            "INSERT INTO monitor_group (monitor_id, group_id, weight, send_url, custom_url) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                monitor_id_map[row["monitor_id"]],
                group_id_map[row["group_id"]],
                row["weight"],
                row["send_url"],
                row["custom_url"],
            ),
        )

    hcols = table_columns(dst_conn, "heartbeat")
    write_cols = [k for k in hcols if k != "id"]
    placeholders = ", ".join("?" * len(write_cols))
    for row in heartbeats:
        dst_conn.execute(
            f"INSERT INTO heartbeat ({', '.join(write_cols)}) VALUES ({placeholders})",
            [monitor_id_map[row["monitor_id"]] if k == "monitor_id" else row[k]
             for k in write_cols],
        )

    # Fix autoincrement sequences so future inserts do not clash. Explicit
    # ids already bumped the sequence; take the actual max id so nothing
    # shrinks below it.
    for table in COPY_TABLES:
        row = dst_conn.execute(f'SELECT max(id) FROM "{table}"').fetchone()
        if row and row[0]:
            dst_conn.execute(
                "INSERT OR REPLACE INTO sqlite_sequence (name, seq) VALUES (?, ?)",
                (table, row[0]),
            )


def verify(dst_conn):
    counts = {
        "monitor": dst_conn.execute("SELECT count(*) FROM monitor").fetchone()[0],
        "group": dst_conn.execute("SELECT count(*) FROM `group`").fetchone()[0],
        "monitor_group": dst_conn.execute("SELECT count(*) FROM monitor_group").fetchone()[0],
        "status_page": dst_conn.execute("SELECT count(*) FROM status_page").fetchone()[0],
        "heartbeat": dst_conn.execute("SELECT count(*) FROM heartbeat").fetchone()[0],
    }
    groups = dst_conn.execute("SELECT name FROM `group`").fetchall()
    monitors = dst_conn.execute("SELECT id, name FROM monitor").fetchall()
    slug = dst_conn.execute("SELECT slug FROM status_page").fetchall()
    return counts, groups, monitors, slug


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="write into the target database (default is dry-run)",
    )
    args = parser.parse_args()

    src = load_source()
    with connect(DST_DB, ro=True) as dst:
        guards = check_target_guards(dst)
        dst_seq = {t: table_sequence(dst, t) or 0 for t in COPY_TABLES}
    if guards:
        print("refusing to run: " + "; ".join(guards), file=sys.stderr)
        sys.exit(1)

    print(plan_summary(src, dst_seq))
    if not args.apply:
        print("dry-run, not writing. Re-run with --apply to migrate.")
        return

    snap_src = snapshot(SRC_DB, "kuma-ctu")
    snap_dst = snapshot(DST_DB, "kuma-uptime")
    print(f"snapshots: {snap_src}, {snap_dst}")

    with connect(DST_DB) as dst:
        dst.execute("BEGIN IMMEDIATE")
        try:
            apply(src, dst)
        except Exception:
            dst.rollback()
            raise
        dst.commit()

    with connect(DST_DB, ro=True) as dst:
        counts, groups, monitors, slug = verify(dst)
    print(f"target counts: {counts}")
    print(f"target groups: {[g[0] for g in groups]}")
    print(f"target monitors: {[(m[0], m[1]) for m in monitors]}")
    print(f"target status page slugs: {[s[0] for s in slug]}")
    print("restart the container: docker restart uptime-uptime_kuma-1")


if __name__ == "__main__":
    main()
