"""Comprehensive unit tests for StateDB migrations, dHash buckets, G1/G2/G3/G10/G11 (M1)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from gphotos_synology.database import (
    _DHASH_BUCKET_CANDIDATES_SQL,
    StateDB,
    _dhash_buckets,
)


def _flip_blocks(dhash_hex: str, block_indices: list[int]) -> str:
    """Flip the lowest bit of each specified 16-bit block in a 32-hex-char dhash."""
    blocks = [int(dhash_hex[i * 4 : (i + 1) * 4], 16) for i in range(8)]
    for idx in block_indices:
        blocks[idx] ^= 0x0001
    return "".join(f"{b:04x}" for b in blocks)


def test_v110_to_v120_additive_migration_and_idempotency(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy_v110.db"

    # Build a genuine v1.1.0 database (no visual_status, no media_dhash_buckets,
    # no old_rel_path/old_taken_at/old_latitude/old_longitude)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE archives (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            source_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            batch_key TEXT NOT NULL DEFAULT '',
            size_bytes INTEGER NOT NULL DEFAULT 0,
            checksum TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            total_files INTEGER NOT NULL DEFAULT 0,
            imported_files INTEGER NOT NULL DEFAULT 0,
            duplicate_files INTEGER NOT NULL DEFAULT 0,
            failed_files INTEGER NOT NULL DEFAULT 0,
            error_message TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            UNIQUE(source, source_id)
        );
        CREATE TABLE media_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_sha256 TEXT NOT NULL UNIQUE,
            final_sha256 TEXT NOT NULL DEFAULT '',
            pixel_sha256 TEXT NOT NULL DEFAULT '',
            dhash TEXT NOT NULL DEFAULT '',
            width INTEGER NOT NULL DEFAULT 0,
            height INTEGER NOT NULL DEFAULT 0,
            sharpness REAL NOT NULL DEFAULT 0.0,
            quality_score REAL NOT NULL DEFAULT 0.0,
            origin TEXT NOT NULL DEFAULT 'takeout',
            original_filename TEXT NOT NULL,
            dest_rel_path TEXT NOT NULL,
            file_size INTEGER NOT NULL DEFAULT 0,
            file_mtime_ns INTEGER NOT NULL DEFAULT 0,
            taken_at TEXT,
            latitude REAL,
            longitude REAL,
            exif_status TEXT NOT NULL DEFAULT 'none',
            source_archive_id INTEGER,
            synology_item_id INTEGER,
            created_at TEXT NOT NULL
        );
        CREATE TABLE quality_upgrades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            media_id INTEGER,
            kept_rel_path TEXT NOT NULL,
            quarantine_filename TEXT NOT NULL,
            old_filename TEXT NOT NULL,
            new_filename TEXT NOT NULL,
            old_width INTEGER NOT NULL DEFAULT 0,
            old_height INTEGER NOT NULL DEFAULT 0,
            old_file_size INTEGER NOT NULL DEFAULT 0,
            old_sharpness REAL NOT NULL DEFAULT 0.0,
            new_width INTEGER NOT NULL DEFAULT 0,
            new_height INTEGER NOT NULL DEFAULT 0,
            new_file_size INTEGER NOT NULL DEFAULT 0,
            new_sharpness REAL NOT NULL DEFAULT 0.0,
            match_reason TEXT NOT NULL DEFAULT '',
            upgrade_reason TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'quarantined',
            created_at TEXT NOT NULL,
            resolved_at TEXT
        );
        """
    )
    now = "2026-01-01T00:00:00+00:00"
    dhash_lower = "0123456789abcdef0123456789abcdef"
    dhash_upper = "AABBCCDDEEFF00112233445566778899"
    dhash_legacy16 = "deadbeefcafebabe"
    conn.executemany(
        """
        INSERT INTO media_items (
            original_sha256, final_sha256, pixel_sha256, dhash,
            width, height, origin, original_filename, dest_rel_path,
            file_size, file_mtime_ns, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 'existing_nas', ?, ?, 1000, 100, ?)
        """,
        [
            ("sha_lower", "sha_lower", "px_lower", dhash_lower, 1000, 800, "a.jpg", "2026/a.jpg", now),
            ("sha_upper", "sha_upper", "px_upper", dhash_upper, 800, 600, "b.jpg", "2026/b.jpg", now),
            ("sha_legacy", "sha_legacy", "px_legacy", dhash_legacy16, 640, 480, "c.jpg", "2026/c.jpg", now),
            ("sha_heic", "sha_heic", "", "", 0, 0, "d.heic", "2026/d.heic", now),
        ],
    )
    conn.commit()
    conn.close()

    # Open twice to verify migration idempotency
    db1 = StateDB(db_path)
    db = StateDB(db_path)
    assert db1._has_legacy_dhash is True
    assert db._has_legacy_dhash is True

    with db.transaction() as c:
        media_cols = {r["name"] for r in c.execute("PRAGMA table_info(media_items)").fetchall()}
        upgrade_cols = {
            r["name"] for r in c.execute("PRAGMA table_info(quality_upgrades)").fetchall()
        }
        assert "visual_status" in media_cols
        assert {"old_rel_path", "old_taken_at", "old_latitude", "old_longitude"}.issubset(
            upgrade_cols
        )

        # Uppercase dhash normalized to lowercase
        upper_row = c.execute(
            "SELECT id, dhash, visual_status FROM media_items WHERE original_sha256 = 'sha_upper'"
        ).fetchone()
        assert upper_row["dhash"] == dhash_upper.lower()
        assert upper_row["visual_status"] == "ok"

        # 2 valid 32-hex rows => 16 bucket rows total (8 each), 0 for legacy16 & empty heic
        bucket_count = c.execute("SELECT COUNT(*) FROM media_dhash_buckets").fetchone()[0]
        assert bucket_count == 16

    indexed = db.get_indexed_files_by_rel_path()
    assert indexed["2026/a.jpg"]["visual_status"] == "ok"
    assert indexed["2026/a.jpg"]["visual_done"] is True
    assert indexed["2026/b.jpg"]["visual_status"] == "ok"
    assert indexed["2026/b.jpg"]["visual_done"] is True
    # Pre-v1.2.0 HEIC row stayed visual_status='' so v1.2.0 scanner re-indexes it once
    assert indexed["2026/d.heic"]["visual_status"] == ""
    assert indexed["2026/d.heic"]["has_visual"] is False
    assert indexed["2026/d.heic"]["visual_done"] is False


def test_app_state_crud_and_delete_state(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db")
    assert db.get_state("oauth_state") is None
    assert db.get_state("oauth_state", "fallback") == "fallback"

    db.set_state("oauth_state", "nonce-1")
    assert db.get_state("oauth_state") == "nonce-1"

    db.set_state("oauth_state", "nonce-2")
    assert db.get_state("oauth_state") == "nonce-2"

    db.delete_state("oauth_state")
    assert db.get_state("oauth_state") is None

    # Idempotent delete on missing key
    db.delete_state("oauth_state")
    assert db.get_state("oauth_state") is None


def test_deterministic_hash_lookup_prefers_original_sha256(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db")
    # Row A inserted first with final_sha256 == H_SHARED
    id_a = db.record_media_item(
        original_sha256="H_A",
        final_sha256="H_SHARED",
        original_filename="a.jpg",
        dest_rel_path="2026/a.jpg",
        file_size=100,
        taken_at="2026-01-01T00:00:00+00:00",
        latitude=None,
        longitude=None,
        exif_status="repaired",
        source_archive_id=None,
    )
    # Row B inserted second with original_sha256 == H_SHARED
    id_b = db.record_media_item(
        original_sha256="H_SHARED",
        final_sha256="H_B",
        original_filename="b.jpg",
        dest_rel_path="2026/b.jpg",
        file_size=120,
        taken_at="2026-01-02T00:00:00+00:00",
        latitude=None,
        longitude=None,
        exif_status="repaired",
        source_archive_id=None,
    )
    assert id_a < id_b

    found_direct = db.get_media_by_original_hash("H_SHARED")
    assert found_direct is not None
    assert found_direct["id"] == id_b

    found_dedup, reason = db.find_duplicate_media(sha256="H_SHARED")
    assert reason == "sha256"
    assert found_dedup is not None
    assert found_dedup["id"] == id_b

    # upsert_existing_nas_media Branch B also prefers exact original_sha256 match
    matched_id = db.upsert_existing_nas_media(
        sha256="H_SHARED",
        pixel_sha256="",
        dhash="",
        filename="copy.jpg",
        dest_rel_path="2026/other_path.jpg",
        file_size=120,
        file_mtime_ns=500,
        taken_at=None,
    )
    assert matched_id == id_b


def test_dhash_bucket_explain_query_plan_uses_primary_key(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db")
    base_dhash = "0123456789abcdef0123456789abcdef"
    db.record_media_item(
        original_sha256="sha1",
        final_sha256="sha1",
        original_filename="p1.jpg",
        dest_rel_path="2026/p1.jpg",
        file_size=1000,
        taken_at="2026-06-01T12:00:00+00:00",
        latitude=None,
        longitude=None,
        exif_status="repaired",
        source_archive_id=None,
        pixel_sha256="px1",
        dhash=base_dhash,
        width=800,
        height=600,
        visual_status="ok",
    )

    bucket_vals = [val for _, val in _dhash_buckets(base_dhash)]
    with db.transaction() as conn:
        plan_rows = conn.execute(
            f"EXPLAIN QUERY PLAN {_DHASH_BUCKET_CANDIDATES_SQL}",
            (*bucket_vals, base_dhash),
        ).fetchall()
    plan_details = [str(r["detail"]) for r in plan_rows]
    pk_searches = [
        d
        for d in plan_details
        if "SEARCH media_dhash_buckets USING PRIMARY KEY (bucket_idx=? AND bucket_val=?)" in d
    ]
    assert len(pk_searches) == 8, f"Expected 8 PK bucket searches, got: {plan_details}"
    assert not any("SCAN media_dhash_buckets" in d for d in plan_details)
    assert not any("SCAN m" in d for d in plan_details)


def test_dhash_buckets_recall_updates_cascades_and_legacy_fallback(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db")
    base_dhash = "102030405060708090a0b0c0d0e0f001"
    ts = "2026-05-10T10:00:00+00:00"

    media_id = db.record_media_item(
        original_sha256="sha_base",
        final_sha256="sha_base",
        original_filename="IMG_1000.JPG",
        dest_rel_path="2026/IMG_1000.JPG",
        file_size=2000,
        taken_at=ts,
        latitude=37.77,
        longitude=-122.42,
        exif_status="repaired",
        source_archive_id=None,
        pixel_sha256="px_base",
        dhash=base_dhash.upper(),  # uppercase normalized to lowercase
        width=1600,
        height=1200,
        visual_status="ok",
    )

    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT bucket_idx, bucket_val FROM media_dhash_buckets WHERE media_id = ? ORDER BY bucket_idx",
            (media_id,),
        ).fetchall()
    assert [(r["bucket_idx"], r["bucket_val"]) for r in rows] == _dhash_buckets(base_dhash)

    # Distance 0 (exact, uppercase query)
    match0, reason0 = db.find_duplicate_media(
        sha256="other0",
        pixel_sha256="other_px0",
        dhash=base_dhash.upper(),
        width=800,
        height=600,
        taken_at=ts,
        filename="IMG_1000.JPG",
        perceptual_max_distance=7,
    )
    assert reason0 == "perceptual_dhash" and match0 is not None and match0["id"] == media_id

    # Distance 1 (bucket path)
    d1 = _flip_blocks(base_dhash, [0])
    match1, reason1 = db.find_duplicate_media(
        sha256="other1",
        pixel_sha256="other_px1",
        dhash=d1,
        width=800,
        height=600,
        taken_at=ts,
        filename="IMG_1000.JPG",
        perceptual_max_distance=7,
    )
    assert reason1 == "perceptual_dhash" and match1 is not None and match1["id"] == media_id

    # Distance 7 (7 different blocks flipped, 1 block intact -> guaranteed bucket hit!)
    d7 = _flip_blocks(base_dhash, [0, 1, 2, 3, 4, 5, 6])
    match7, reason7 = db.find_duplicate_media(
        sha256="other7",
        pixel_sha256="other_px7",
        dhash=d7,
        width=800,
        height=600,
        taken_at=ts,
        filename="IMG_1000.JPG",
        perceptual_max_distance=7,
    )
    assert reason7 == "perceptual_dhash" and match7 is not None and match7["id"] == media_id

    # Distance 8 (all 8 blocks flipped): NOT matched at max_distance=7, MATCHED at 8 and 10 via fallback
    d8 = _flip_blocks(base_dhash, [0, 1, 2, 3, 4, 5, 6, 7])
    miss8, _ = db.find_duplicate_media(
        sha256="other8a",
        pixel_sha256="other_px8a",
        dhash=d8,
        width=800,
        height=600,
        taken_at=ts,
        filename="IMG_1000.JPG",
        perceptual_max_distance=7,
    )
    assert miss8 is None

    for max_dist in (8, 10):
        hit8, reason8 = db.find_duplicate_media(
            sha256=f"other8_{max_dist}",
            pixel_sha256=f"other_px8_{max_dist}",
            dhash=d8,
            width=800,
            height=600,
            taken_at=ts,
            filename="IMG_1000.JPG",
            perceptual_max_distance=max_dist,
        )
        assert reason8 == "perceptual_dhash" and hit8 is not None and hit8["id"] == media_id

    # upgrade_media_item replaces buckets and updates optional visual_status/GPS (including None)
    new_dhash = "fedcba9876543210fedcba9876543210"
    db.upgrade_media_item(
        media_id,
        original_sha256="sha_upgraded",
        final_sha256="sha_upgraded",
        pixel_sha256="px_upgraded",
        dhash=new_dhash,
        width=3200,
        height=2400,
        sharpness=200.0,
        quality_score=999.0,
        original_filename="IMG_1000_HQ.JPG",
        dest_rel_path="2026/IMG_1000_HQ.JPG",
        file_size=5000,
        file_mtime_ns=200,
        taken_at=ts,
        exif_status="repaired",
        visual_status="ok",
        latitude=None,
        longitude=None,
    )
    upgraded_row = db.get_media_by_id(media_id)
    assert upgraded_row is not None
    assert upgraded_row["dhash"] == new_dhash
    assert upgraded_row["latitude"] is None
    assert upgraded_row["longitude"] is None
    assert upgraded_row["visual_status"] == "ok"

    with db.transaction() as conn:
        old_bucket_hits = conn.execute(
            "SELECT COUNT(*) FROM media_dhash_buckets WHERE bucket_idx = 0 AND bucket_val = ?",
            (base_dhash[:4],),
        ).fetchone()[0]
        new_bucket_rows = conn.execute(
            "SELECT bucket_idx, bucket_val FROM media_dhash_buckets WHERE media_id = ? ORDER BY bucket_idx",
            (media_id,),
        ).fetchall()
    assert old_bucket_hits == 0
    assert [(r["bucket_idx"], r["bucket_val"]) for r in new_bucket_rows] == _dhash_buckets(
        new_dhash
    )

    # delete_media_item cascades cleanly to media_dhash_buckets
    assert db.delete_media_item(media_id) is True
    assert db.delete_media_item(media_id) is False
    assert db.get_media_by_id(media_id) is None
    with db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM media_dhash_buckets").fetchone()[0] == 0

    # upsert_existing_nas_media bucket sync + prune cascade + legacy 16-char hash recall
    nas_dhash1 = "aaaaaaaaaaaaaaaa1111111111111111"
    nas_dhash2 = "bbbbbbbbbbbbbbbb2222222222222222"
    nas_id = db.upsert_existing_nas_media(
        sha256="nas_sha1",
        pixel_sha256="nas_px1",
        dhash=nas_dhash1,
        width=1000,
        height=1000,
        visual_status="ok",
        filename="nas.jpg",
        dest_rel_path="2026/nas.jpg",
        file_size=500,
        file_mtime_ns=10,
        taken_at=ts,
    )
    # Updating same path with empty dhash & visual_status='skipped' preserves buckets & 'ok'
    db.upsert_existing_nas_media(
        sha256="nas_sha1",
        pixel_sha256="",
        dhash="",
        visual_status="skipped",
        filename="nas.jpg",
        dest_rel_path="2026/nas.jpg",
        file_size=500,
        file_mtime_ns=11,
        taken_at=ts,
    )
    assert db.get_media_by_id(nas_id)["visual_status"] == "ok"
    with db.transaction() as conn:
        preserved = conn.execute(
            "SELECT bucket_val FROM media_dhash_buckets WHERE media_id = ? AND bucket_idx = 0",
            (nas_id,),
        ).fetchone()
    assert preserved is not None and preserved["bucket_val"] == "aaaa"

    # Updating same path with new dhash replaces buckets
    db.upsert_existing_nas_media(
        sha256="nas_sha2",
        pixel_sha256="nas_px2",
        dhash=nas_dhash2,
        width=1000,
        height=1000,
        visual_status="ok",
        filename="nas.jpg",
        dest_rel_path="2026/nas.jpg",
        file_size=600,
        file_mtime_ns=12,
        taken_at=ts,
    )
    with db.transaction() as conn:
        replaced = conn.execute(
            "SELECT bucket_val FROM media_dhash_buckets WHERE media_id = ? AND bucket_idx = 0",
            (nas_id,),
        ).fetchone()
    assert replaced is not None and replaced["bucket_val"] == "bbbb"

    # Prune missing existing_nas media cascades bucket deletion
    pruned = db.prune_missing_existing_nas_media(set())
    assert pruned == 1
    with db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM media_dhash_buckets").fetchone()[0] == 0

    # Legacy 16-char hash still matches via Tier 3 even when perceptual_max_distance < 8
    legacy_id = db.record_media_item(
        original_sha256="sha_legacy16",
        final_sha256="sha_legacy16",
        original_filename="legacy.jpg",
        dest_rel_path="2026/legacy.jpg",
        file_size=400,
        taken_at=ts,
        latitude=None,
        longitude=None,
        exif_status="existing",
        source_archive_id=None,
        pixel_sha256="px_leg",
        dhash="1111222233334444",
        width=800,
        height=600,
    )
    assert db._has_legacy_dhash is True
    # 32-hex incoming hash whose first 16 hex chars differ by 1 bit from legacy 16-hex hash
    incoming_32 = "1111222233334445" + "9999888877776666"
    leg_match, leg_reason = db.find_duplicate_media(
        sha256="sha_new_leg",
        pixel_sha256="px_new_leg",
        dhash=incoming_32,
        width=800,
        height=600,
        taken_at=ts,
        filename="legacy.jpg",
        perceptual_max_distance=4,
    )
    assert leg_reason == "perceptual_dhash" and leg_match is not None and leg_match["id"] == legacy_id


def test_visual_status_transitions_and_indexed_files_map(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db")
    db.upsert_existing_nas_media(
        sha256="s_ok",
        pixel_sha256="p_ok",
        dhash="0" * 32,
        width=100,
        height=100,
        visual_status="ok",
        filename="ok.jpg",
        dest_rel_path="ok.jpg",
        file_size=10,
        file_mtime_ns=1,
        taken_at=None,
    )
    db.upsert_existing_nas_media(
        sha256="s_undec",
        pixel_sha256="",
        dhash="",
        visual_status="undecodable",
        filename="corrupt.jpg",
        dest_rel_path="corrupt.jpg",
        file_size=10,
        file_mtime_ns=2,
        taken_at=None,
    )
    db.upsert_existing_nas_media(
        sha256="s_skip",
        pixel_sha256="",
        dhash="",
        visual_status="skipped",
        filename="skip.jpg",
        dest_rel_path="skip.jpg",
        file_size=10,
        file_mtime_ns=3,
        taken_at=None,
    )

    indexed = db.get_indexed_files_by_rel_path()
    assert indexed["ok.jpg"]["has_visual"] is True
    assert indexed["ok.jpg"]["visual_status"] == "ok"
    assert indexed["ok.jpg"]["visual_done"] is True

    assert indexed["corrupt.jpg"]["has_visual"] is False
    assert indexed["corrupt.jpg"]["visual_status"] == "undecodable"
    assert indexed["corrupt.jpg"]["visual_done"] is True

    assert indexed["skip.jpg"]["has_visual"] is False
    assert indexed["skip.jpg"]["visual_status"] == "skipped"
    assert indexed["skip.jpg"]["visual_done"] is False

    # Transition undecodable -> ok cleanly
    db.upsert_existing_nas_media(
        sha256="s_undec2",
        pixel_sha256="p_fixed",
        dhash="1" * 32,
        width=200,
        height=200,
        visual_status="ok",
        filename="corrupt.jpg",
        dest_rel_path="corrupt.jpg",
        file_size=20,
        file_mtime_ns=4,
        taken_at=None,
    )
    updated = db.get_indexed_files_by_rel_path()["corrupt.jpg"]
    assert updated["visual_status"] == "ok"
    assert updated["has_visual"] is True
    assert updated["visual_done"] is True


def test_quality_upgrades_journal_lifecycle_and_batch_pagination(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db")
    media_id = db.record_media_item(
        original_sha256="m1",
        final_sha256="m1",
        original_filename="old.jpg",
        dest_rel_path="2025/old.jpg",
        file_size=500,
        taken_at="2025-01-01T00:00:00+00:00",
        latitude=48.8566,
        longitude=2.3522,
        exif_status="existing",
        source_archive_id=None,
    )

    pending_id = db.create_pending_quality_upgrade(
        media_id=media_id,
        quarantine_filename="q_old.jpg",
        old_rel_path="2025/old.jpg",
        old_filename="old.jpg",
        new_filename="new.jpg",
        old_width=640,
        old_height=480,
        old_file_size=500,
        old_sharpness=10.0,
        new_width=1920,
        new_height=1440,
        new_file_size=2000,
        new_sharpness=80.0,
        match_reason="perceptual_dhash",
        upgrade_reason="higher resolution",
        old_taken_at="2025-01-01T00:00:00+00:00",
        old_latitude=48.8566,
        old_longitude=2.3522,
    )

    # Transient in_progress row is hidden from UI list & stats, visible in recovery query
    assert db.list_quality_upgrades() == []
    assert len(db.list_quality_upgrades(include_in_progress=True)) == 1
    assert db.get_stats()["quality_upgrades_count"] == 0
    assert db.get_stats()["quarantined_count"] == 0

    in_prog = db.list_in_progress_quality_upgrades()
    assert len(in_prog) == 1
    assert in_prog[0]["id"] == pending_id
    assert in_prog[0]["old_rel_path"] == "2025/old.jpg"
    assert in_prog[0]["old_taken_at"] == "2025-01-01T00:00:00+00:00"
    assert in_prog[0]["old_latitude"] == 48.8566
    assert in_prog[0]["old_longitude"] == 2.3522

    # Finalize transitions in_progress -> quarantined
    db.finalize_quality_upgrade(pending_id, "2025/new.jpg", new_file_size=2450)
    assert db.list_in_progress_quality_upgrades() == []
    row = db.get_quality_upgrade(pending_id)
    assert row is not None
    assert row["status"] == "quarantined"
    assert row["kept_rel_path"] == "2025/new.jpg"
    assert row["new_file_size"] == 2450
    assert len(db.list_quality_upgrades()) == 1
    assert db.get_stats()["quality_upgrades_count"] == 1
    assert db.get_stats()["quarantined_count"] == 1

    # Rollback delete removes pending row
    rolled_back_id = db.create_pending_quality_upgrade(
        media_id=media_id,
        quarantine_filename="q_rb.jpg",
        old_rel_path="2025/old.jpg",
        old_filename="old.jpg",
        new_filename="bad.jpg",
        old_width=640,
        old_height=480,
        old_file_size=500,
        old_sharpness=10.0,
        new_width=1920,
        new_height=1440,
        new_file_size=2000,
        new_sharpness=80.0,
        match_reason="perceptual_dhash",
        upgrade_reason="test rollback",
    )
    db.delete_quality_upgrade(rolled_back_id)
    assert db.get_quality_upgrade(rolled_back_id) is None

    # Batch pagination past 500 rows (create 505 additional quarantined upgrades = 506 total)
    for i in range(505):
        db.record_quality_upgrade(
            media_id=media_id,
            kept_rel_path=f"2025/k_{i}.jpg",
            quarantine_filename=f"q_{i}.jpg",
            old_filename=f"o_{i}.jpg",
            new_filename=f"n_{i}.jpg",
            old_width=100,
            old_height=100,
            old_file_size=100,
            old_sharpness=1.0,
            new_width=200,
            new_height=200,
            new_file_size=200,
            new_sharpness=2.0,
            match_reason="pixel_sha256",
            upgrade_reason="batch test",
            old_rel_path=f"2025/o_{i}.jpg",
            old_taken_at="2025-01-01T00:00:00+00:00",
        )

    purged = 0
    while True:
        batch = db.list_quarantined_upgrades_batch(limit=200)
        if not batch:
            break
        for item in batch:
            db.mark_quality_upgrade_status(int(item["id"]), "purged")
            purged += 1
    assert purged == 506
    assert db.get_stats()["quarantined_count"] == 0
    assert db.get_stats()["quality_upgrades_count"] == 506


def test_upsert_archive_retry_reset_and_completed_with_errors_stats(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db")

    # Archive 1: completed_with_errors then retried
    aid1 = db.upsert_archive(
        source="inbox",
        source_id="arc-1",
        filename="takeout-001.zip",
        status="processing",
    )
    db.update_archive_progress(
        aid1,
        status="completed_with_errors",
        total_files=10,
        imported_files=7,
        duplicate_files=2,
        failed_files=1,
        error_message="1 file failed to import",
        completed=False,
    )
    stats = db.get_stats()
    assert stats["completed_with_errors_archives"] == 1
    assert stats["completed_archives"] == 0
    assert stats["failed_archives"] == 0

    # Retrying via upsert_archive resets all counters, error_message, and completed_at
    aid1_retry = db.upsert_archive(
        source="inbox",
        source_id="arc-1",
        filename="takeout-001.zip",
        status="processing",
    )
    assert aid1_retry == aid1
    arc1 = db.get_archive_by_source("inbox", "arc-1")
    assert arc1 is not None
    assert arc1["status"] == "processing"
    assert arc1["total_files"] == 0
    assert arc1["imported_files"] == 0
    assert arc1["duplicate_files"] == 0
    assert arc1["failed_files"] == 0
    assert arc1["error_message"] == ""
    assert arc1["completed_at"] is None

    # Archive 2: completed archive is NOT wiped if upsert_archive is called again
    aid2 = db.upsert_archive(
        source="gdrive",
        source_id="arc-2",
        filename="takeout-002.zip",
        status="processing",
    )
    db.update_archive_progress(
        aid2,
        status="completed",
        total_files=5,
        imported_files=5,
        duplicate_files=0,
        failed_files=0,
        completed=True,
    )
    db.upsert_archive(
        source="gdrive",
        source_id="arc-2",
        filename="takeout-002.zip",
        status="pending",
    )
    arc2 = db.get_archive_by_source("gdrive", "arc-2")
    assert arc2 is not None
    assert arc2["status"] == "completed"
    assert arc2["imported_files"] == 5
    assert arc2["completed_at"] is not None


def test_album_membership_retry_counting_and_error_revival(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db")
    mid = db.record_media_item(
        original_sha256="alb_sha",
        final_sha256="alb_sha",
        original_filename="beach.jpg",
        dest_rel_path="2026/beach.jpg",
        file_size=100,
        taken_at=None,
        latitude=None,
        longitude=None,
        exif_status="none",
        source_archive_id=None,
    )
    album_id = db.add_media_to_album("Summer Trip", mid)

    # Waiting for Synology Photos indexing (status='pending') 25 times must NOT burn retry_count
    for _ in range(25):
        db.mark_album_membership(
            album_id, mid, status="pending", error="Waiting for Synology indexing"
        )
    pending = db.get_pending_album_memberships()
    assert len(pending) == 1
    assert pending[0]["retry_count"] == 0

    # Explicit increment_retry=False on error keeps retry_count unchanged
    db.mark_album_membership(
        album_id, mid, status="error", error="transient", increment_retry=False
    )
    assert db.get_pending_album_memberships()[0]["retry_count"] == 0

    # Explicit increment_retry=True on pending increments retry_count by 1
    db.mark_album_membership(
        album_id, mid, status="pending", error="explicit", increment_retry=True
    )
    assert db.get_pending_album_memberships()[0]["retry_count"] == 1

    # 19 real errors bring retry_count to 20 -> item drops out of pending queue
    for _ in range(19):
        db.mark_album_membership(album_id, mid, status="error", error="API failure")
    assert db.get_pending_album_memberships() == []

    # Re-adding media to album revives 'error' row back to 'pending' with retry_count=0
    db.add_media_to_album("Summer Trip", mid)
    revived = db.get_pending_album_memberships()
    assert len(revived) == 1
    assert revived[0]["retry_count"] == 0

    # Marking synced clears last_error and removes from pending queue
    db.mark_album_membership(album_id, mid, status="synced")
    assert db.get_pending_album_memberships() == []
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT sync_status, last_error FROM album_memberships WHERE album_id = ? AND media_id = ?",
            (album_id, mid),
        ).fetchone()
    assert row["sync_status"] == "synced"
    assert row["last_error"] == ""


def test_log_event_periodic_pruning(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db")

    # 49 inserts should NOT trigger pruning yet
    for i in range(49):
        db.log_event("INFO", "test", f"msg-{i}", max_rows=10)
    assert len(db.get_recent_logs(limit=100)) == 49

    # 50th insert triggers pruning to exact max_rows=10 newest rows
    db.log_event("INFO", "test", "msg-49", max_rows=10)
    remaining = db.get_recent_logs(limit=100)
    assert len(remaining) == 10
    assert [r["message"] for r in remaining] == [f"msg-{i}" for i in range(49, 39, -1)]
