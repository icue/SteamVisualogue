"""SQLite persistence for Steam Visualogue's deterministic data layer.

The database deliberately stores Steam IDs only in user-scoped tables.  Public
collector results are assembled elsewhere and never expose those identifiers.
"""

from __future__ import annotations

import json
import hashlib
import math
import re
import sqlite3
import time
import zlib
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


SNAPSHOT_PAYLOAD_VERSION = 1
ACQUISITION_SNAPSHOT_TTL = 24 * 60 * 60
LOCALIZED_APP_LABEL_TTL = 30 * 24 * 60 * 60
LOCALIZED_ACHIEVEMENT_LABEL_TTL = 90 * 24 * 60 * 60
LOCALIZED_LABEL_FAILURE_TTL = 7 * 24 * 60 * 60
MAX_SNAPSHOT_PAYLOAD_BYTES = 50 * 1024 * 1024
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PALETTE_ALGORITHM_RE = re.compile(r"[^\s]{1,128}")
def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _loads(value: str | None, fallback: Any) -> Any:
    if value is None:
        return fallback

    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _snapshot_json(payload: Mapping[str, Any]) -> bytes:
    """Encode a public snapshot payload with deterministic JSON semantics."""

    if not isinstance(payload, Mapping):
        raise ValueError("snapshot payload must be a JSON object")
    forbidden = {
        "steamid", "steam_id", "userid", "user_id", "run_id",
        "generated_at", "evidence_fingerprint", "api_key", "identity",
        "identity_key", "path", "local_path", "source_path",
        "temporary_source_path", "prompt", "session", "session_id",
    }

    def check_keys(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if str(key).casefold() in forbidden:
                    raise ValueError("snapshot payload contains a private or run-local field")
                check_keys(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                check_keys(child)

    check_keys(payload)
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("snapshot payload is not JSON serializable") from error
    if not encoded or len(encoded) > MAX_SNAPSHOT_PAYLOAD_BYTES:
        raise ValueError("snapshot payload is empty or exceeds the size limit")
    return encoded


def _snapshot_blob(payload: Mapping[str, Any]) -> tuple[bytes, str]:
    encoded = _snapshot_json(payload)
    return zlib.compress(encoded, level=9), hashlib.sha256(encoded).hexdigest()


def _decode_snapshot_blob(
    blob: bytes | bytearray | memoryview,
    expected_sha256: str,
    *,
    max_bytes: int = MAX_SNAPSHOT_PAYLOAD_BYTES,
) -> dict[str, Any]:
    if not _SHA256_RE.fullmatch(str(expected_sha256)):
        raise ValueError("snapshot SHA-256 is invalid")
    compressed = bytes(blob)
    if not compressed or len(compressed) > max_bytes:
        raise ValueError("snapshot payload is empty or exceeds the size limit")
    decoder = zlib.decompressobj()
    raw = decoder.decompress(compressed, int(max_bytes) + 1)
    if len(raw) > int(max_bytes) or decoder.unconsumed_tail or not decoder.eof:
        raise ValueError("snapshot payload is damaged or exceeds the size limit")
    raw += decoder.flush()
    if len(raw) > int(max_bytes) or decoder.unused_data:
        raise ValueError("snapshot payload is damaged or exceeds the size limit")
    if hashlib.sha256(raw).hexdigest() != str(expected_sha256):
        raise ValueError("snapshot payload checksum does not match")
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("snapshot payload is not valid JSON") from error
    if not isinstance(decoded, dict):
        raise ValueError("snapshot payload must be a JSON object")
    return decoded


class CacheDB:
    """Small explicit repository over the global and per-user cache tables."""

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = str(path)
        self.clock = clock
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "CacheDB":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            DROP TABLE IF EXISTS app_palette;

            CREATE TABLE IF NOT EXISTS app_metadata (
                appid INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT,
                genres_json TEXT NOT NULL DEFAULT '[]',
                release_date TEXT,
                developers_json TEXT NOT NULL DEFAULT '[]',
                publishers_json TEXT NOT NULL DEFAULT '[]',
                platforms_json TEXT NOT NULL DEFAULT '{}',
                categories_json TEXT NOT NULL DEFAULT '[]',
                achievement_total INTEGER,
                header_image_url TEXT,
                metadata_status TEXT NOT NULL,
                fetched_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS image_palette (
                content_sha256 TEXT NOT NULL,
                algorithm TEXT NOT NULL,
                color_count INTEGER NOT NULL,
                palette_json TEXT NOT NULL,
                computed_at REAL NOT NULL,
                PRIMARY KEY (content_sha256, algorithm, color_count)
            );

            CREATE TABLE IF NOT EXISTS achievement_schema (
                appid INTEGER NOT NULL,
                apiname TEXT NOT NULL,
                display_name TEXT,
                description TEXT,
                hidden INTEGER NOT NULL DEFAULT 0,
                icon_url TEXT,
                icon_gray_url TEXT,
                fetched_at REAL NOT NULL,
                PRIMARY KEY (appid, apiname)
            );

            CREATE TABLE IF NOT EXISTS achievement_schema_state (
                appid INTEGER PRIMARY KEY,
                status TEXT NOT NULL,
                fetched_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS achievement_global (
                appid INTEGER NOT NULL,
                apiname TEXT NOT NULL,
                global_percent REAL,
                fetched_at REAL NOT NULL,
                PRIMARY KEY (appid, apiname)
            );

            CREATE TABLE IF NOT EXISTS achievement_global_state (
                appid INTEGER PRIMARY KEY,
                status TEXT NOT NULL,
                fetched_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_games (
                steamid TEXT NOT NULL,
                appid INTEGER NOT NULL,
                playtime_forever INTEGER NOT NULL,
                name TEXT,
                snapshot_at REAL NOT NULL,
                PRIMARY KEY (steamid, appid)
            );

            CREATE TABLE IF NOT EXISTS user_achievements (
                steamid TEXT NOT NULL,
                appid INTEGER NOT NULL,
                apiname TEXT NOT NULL,
                achieved INTEGER NOT NULL,
                unlocktime INTEGER NOT NULL,
                fetched_at REAL NOT NULL,
                PRIMARY KEY (steamid, appid, apiname)
            );

            CREATE TABLE IF NOT EXISTS user_achievement_state (
                steamid TEXT NOT NULL,
                appid INTEGER NOT NULL,
                status TEXT NOT NULL,
                playtime_forever INTEGER NOT NULL,
                fetched_at REAL NOT NULL,
                PRIMARY KEY (steamid, appid)
            );

            CREATE TABLE IF NOT EXISTS run_history (
                run_id TEXT PRIMARY KEY,
                steamid TEXT NOT NULL,
                player_alias TEXT NOT NULL,
                generated_at REAL NOT NULL,
                game_count INTEGER NOT NULL,
                status TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS identity_resolution_cache (
                identity_key_hash TEXT PRIMARY KEY,
                steamid TEXT NOT NULL,
                player_alias TEXT NOT NULL,
                resolved_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_identity_resolution_steamid
                ON identity_resolution_cache (steamid);

            CREATE TABLE IF NOT EXISTS user_acquisition_snapshot (
                steamid TEXT PRIMARY KEY,
                snapshot_id TEXT NOT NULL UNIQUE,
                player_alias TEXT NOT NULL,
                collected_at REAL NOT NULL,
                collection_payload BLOB NOT NULL,
                collection_sha256 TEXT NOT NULL,
                enriched_at REAL,
                enrichment_payload BLOB,
                enrichment_sha256 TEXT,
                payload_version INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS artwork_content (
                content_sha256 TEXT PRIMARY KEY,
                payload BLOB NOT NULL,
                byte_size INTEGER NOT NULL,
                stored_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS artwork_source (
                source_url_hash TEXT PRIMARY KEY,
                content_sha256 TEXT NOT NULL,
                content_type TEXT NOT NULL,
                fetched_at REAL NOT NULL,
                FOREIGN KEY (content_sha256) REFERENCES artwork_content(content_sha256)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS localized_app_label (
                appid INTEGER NOT NULL,
                report_locale TEXT NOT NULL,
                display_name TEXT,
                status TEXT NOT NULL,
                fetched_at REAL NOT NULL,
                PRIMARY KEY (appid, report_locale)
            );

            CREATE TABLE IF NOT EXISTS localized_achievement_label (
                appid INTEGER NOT NULL,
                apiname TEXT NOT NULL,
                report_locale TEXT NOT NULL,
                display_name TEXT,
                description TEXT,
                fetched_at REAL NOT NULL,
                PRIMARY KEY (appid, apiname, report_locale)
            );

            CREATE TABLE IF NOT EXISTS localized_achievement_label_state (
                appid INTEGER NOT NULL,
                report_locale TEXT NOT NULL,
                status TEXT NOT NULL,
                fetched_at REAL NOT NULL,
                PRIMARY KEY (appid, report_locale)
            );

            CREATE TABLE IF NOT EXISTS achievement_semantic_cache (
                identity_scope TEXT NOT NULL,
                game_input_fingerprint TEXT NOT NULL,
                analysis_contract_fingerprint TEXT NOT NULL,
                report_locale TEXT NOT NULL,
                game_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                stored_at REAL NOT NULL,
                PRIMARY KEY (
                    identity_scope,
                    game_input_fingerprint,
                    analysis_contract_fingerprint,
                    report_locale
                )
            );

            CREATE TABLE IF NOT EXISTS editorial_reuse_current (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                steamid TEXT NOT NULL,
                evidence_fingerprint TEXT NOT NULL,
                visual_fingerprint TEXT NOT NULL,
                deck_schema_fingerprint TEXT NOT NULL,
                report_locale TEXT NOT NULL,
                source_run_id TEXT NOT NULL,
                bundle_json TEXT NOT NULL,
                bundle_sha256 TEXT NOT NULL,
                completed_at REAL NOT NULL,
                UNIQUE (steamid, evidence_fingerprint, visual_fingerprint, deck_schema_fingerprint, report_locale)
            );

            CREATE TABLE IF NOT EXISTS editorial_reuse_generated_current (
                entry_id INTEGER NOT NULL,
                asset_id TEXT NOT NULL,
                png_blob BLOB NOT NULL,
                byte_size INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                record_json TEXT NOT NULL,
                PRIMARY KEY (entry_id, asset_id),
                FOREIGN KEY (entry_id) REFERENCES editorial_reuse_current(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_user_games_steamid
                ON user_games (steamid);
            CREATE INDEX IF NOT EXISTS idx_user_achievements_identity
                ON user_achievements (steamid, appid);
            CREATE INDEX IF NOT EXISTS idx_run_history_identity
                ON run_history (steamid, generated_at);
            CREATE INDEX IF NOT EXISTS idx_editorial_reuse_current_identity
                ON editorial_reuse_current (steamid, completed_at);
            CREATE INDEX IF NOT EXISTS idx_achievement_semantic_identity
                ON achievement_semantic_cache (identity_scope, report_locale);
            """
        )
        self._connection.commit()

    @staticmethod
    def is_fresh(row: Mapping[str, Any] | None, ttl_seconds: float, now: float) -> bool:
        if not row or row.get("fetched_at") is None:
            return False
        return now - float(row["fetched_at"]) < ttl_seconds

    @staticmethod
    def is_snapshot_fresh(
        row: Mapping[str, Any] | None,
        now: float,
        ttl_seconds: float = ACQUISITION_SNAPSHOT_TTL,
    ) -> bool:
        """Return true only for a non-future snapshot inside the rolling TTL."""

        if not row or row.get("collected_at") is None:
            return False
        try:
            collected_at = float(row["collected_at"])
            current = float(now)
            ttl = float(ttl_seconds)
        except (TypeError, ValueError):
            return False
        if not all(math.isfinite(value) for value in (collected_at, current, ttl)):
            return False
        age = current - collected_at
        return 0 <= age < ttl

    @staticmethod
    def _validate_snapshot_payload(payload: Mapping[str, Any], *, enrichment: bool) -> None:
        if not isinstance(payload, Mapping) or not isinstance(payload.get("games"), list):
            raise ValueError("snapshot payload must contain a games array")
        if not isinstance(payload.get("data_status"), Mapping):
            raise ValueError("snapshot payload must contain a data_status object")
        for game in payload["games"]:
            if not isinstance(game, Mapping):
                raise ValueError("snapshot games must contain objects")
            try:
                appid = int(game.get("appid"))
                playtime = float(game.get("playtime_minutes", 0))
            except (TypeError, ValueError):
                raise ValueError("snapshot game fields have invalid types") from None
            if appid < 1 or not math.isfinite(playtime) or playtime < 0:
                raise ValueError("snapshot game fields have invalid values")
            if not isinstance(game.get("name"), str):
                raise ValueError("snapshot game name has an invalid type")
        if enrichment:
            if not isinstance(payload.get("enriched_at"), str) or not payload["enriched_at"]:
                raise ValueError("enrichment payload must contain enriched_at")
        elif not isinstance(payload.get("player_alias"), str) or not payload["player_alias"]:
            raise ValueError("collection payload must contain player_alias")

    def _read_snapshot(
        self,
        steamid: str,
        *,
        max_payload_bytes: int = MAX_SNAPSHOT_PAYLOAD_BYTES,
    ) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM user_acquisition_snapshot WHERE steamid = ?",
            (str(steamid),),
        ).fetchone()
        if row is None:
            return None
        try:
            if int(row["payload_version"]) != SNAPSHOT_PAYLOAD_VERSION:
                raise ValueError("snapshot payload version is unsupported")
            if not str(row["snapshot_id"]) or not str(row["player_alias"]):
                raise ValueError("snapshot identity fields are invalid")
            collected_at = float(row["collected_at"])
            if not math.isfinite(collected_at):
                raise ValueError("snapshot collected_at is invalid")
            collection_payload = _decode_snapshot_blob(
                row["collection_payload"],
                str(row["collection_sha256"]),
                max_bytes=max_payload_bytes,
            )
            self._validate_snapshot_payload(collection_payload, enrichment=False)
            if collection_payload["player_alias"] != str(row["player_alias"]):
                raise ValueError("snapshot player alias does not match its payload")
            enrichment_payload = None
            if row["enrichment_payload"] is not None or row["enrichment_sha256"] is not None:
                if row["enrichment_payload"] is None or row["enrichment_sha256"] is None:
                    raise ValueError("snapshot enrichment payload is incomplete")
                if row["enriched_at"] is None or not math.isfinite(float(row["enriched_at"])):
                    raise ValueError("snapshot enriched_at is invalid")
                enrichment_payload = _decode_snapshot_blob(
                    row["enrichment_payload"],
                    str(row["enrichment_sha256"]),
                    max_bytes=max_payload_bytes,
                )
                self._validate_snapshot_payload(enrichment_payload, enrichment=True)
            elif row["enriched_at"] is not None:
                raise ValueError("snapshot enriched_at has no payload")
        except (TypeError, ValueError, zlib.error):
            with self._connection:
                self._connection.execute(
                    "DELETE FROM user_acquisition_snapshot WHERE steamid = ?",
                    (str(steamid),),
                )
            return None
        return {
            "steamid": str(row["steamid"]),
            "snapshot_id": str(row["snapshot_id"]),
            "player_alias": str(row["player_alias"]),
            "collected_at": collected_at,
            "collection_payload": collection_payload,
            "collection_sha256": str(row["collection_sha256"]),
            "enriched_at": (
                float(row["enriched_at"]) if row["enriched_at"] is not None else None
            ),
            "enrichment_payload": enrichment_payload,
            "enrichment_sha256": (
                str(row["enrichment_sha256"]) if row["enrichment_sha256"] is not None else None
            ),
            "payload_version": int(row["payload_version"]),
        }

    def replace_collection_snapshot(
        self,
        steamid: str,
        snapshot_id: str,
        player_alias: str,
        collected_at: float,
        payload: Mapping[str, Any],
        *,
        payload_version: int = SNAPSHOT_PAYLOAD_VERSION,
    ) -> None:
        """Atomically replace one user's collection and discard old enrichment."""

        if int(payload_version) != SNAPSHOT_PAYLOAD_VERSION:
            raise ValueError("snapshot payload version is unsupported")
        if not str(steamid) or not str(snapshot_id) or not str(player_alias):
            raise ValueError("snapshot identity fields are required")
        if not math.isfinite(float(collected_at)):
            raise ValueError("snapshot collected_at is invalid")
        self._validate_snapshot_payload(payload, enrichment=False)
        compressed, digest = _snapshot_blob(payload)
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO user_acquisition_snapshot (
                    steamid, snapshot_id, player_alias, collected_at,
                    collection_payload, collection_sha256, enriched_at,
                    enrichment_payload, enrichment_sha256, payload_version
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?)
                ON CONFLICT(steamid) DO UPDATE SET
                    snapshot_id=excluded.snapshot_id,
                    player_alias=excluded.player_alias,
                    collected_at=excluded.collected_at,
                    collection_payload=excluded.collection_payload,
                    collection_sha256=excluded.collection_sha256,
                    enriched_at=NULL,
                    enrichment_payload=NULL,
                    enrichment_sha256=NULL,
                    payload_version=excluded.payload_version
                """,
                (
                    str(steamid), str(snapshot_id), str(player_alias), float(collected_at),
                    sqlite3.Binary(compressed), digest, int(payload_version),
                ),
            )

    def replace_enrichment_snapshot(
        self,
        steamid: str,
        snapshot_id: str,
        enriched_at: float,
        payload: Mapping[str, Any],
        *,
        payload_version: int = SNAPSHOT_PAYLOAD_VERSION,
    ) -> bool:
        """Atomically save enrichment only if the collection snapshot is still current."""

        if int(payload_version) != SNAPSHOT_PAYLOAD_VERSION:
            raise ValueError("snapshot payload version is unsupported")
        if not math.isfinite(float(enriched_at)):
            raise ValueError("snapshot enriched_at is invalid")
        self._validate_snapshot_payload(payload, enrichment=True)
        compressed, digest = _snapshot_blob(payload)
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE user_acquisition_snapshot
                SET enriched_at = ?, enrichment_payload = ?, enrichment_sha256 = ?
                WHERE steamid = ? AND snapshot_id = ? AND payload_version = ?
                """,
                (
                    float(enriched_at), sqlite3.Binary(compressed), digest,
                    str(steamid), str(snapshot_id), int(payload_version),
                ),
            )
        return cursor.rowcount == 1

    def get_acquisition_snapshot(
        self,
        steamid: str,
        *,
        max_payload_bytes: int = MAX_SNAPSHOT_PAYLOAD_BYTES,
    ) -> dict[str, Any] | None:
        """Return an integrity-checked snapshot, deleting corrupt rows as a cache miss."""

        return self._read_snapshot(str(steamid), max_payload_bytes=max_payload_bytes)

    def get_collection_snapshot(
        self,
        steamid: str,
        *,
        now: float | None = None,
        ttl_seconds: float = ACQUISITION_SNAPSHOT_TTL,
        max_payload_bytes: int = MAX_SNAPSHOT_PAYLOAD_BYTES,
    ) -> dict[str, Any] | None:
        snapshot = self._read_snapshot(str(steamid), max_payload_bytes=max_payload_bytes)
        current = self.clock() if now is None else now
        if snapshot is None or not self.is_snapshot_fresh(snapshot, current, ttl_seconds):
            return None
        return snapshot

    def get_enrichment_snapshot(
        self,
        steamid: str,
        *,
        snapshot_id: str | None = None,
        now: float | None = None,
        ttl_seconds: float = ACQUISITION_SNAPSHOT_TTL,
        max_payload_bytes: int = MAX_SNAPSHOT_PAYLOAD_BYTES,
    ) -> dict[str, Any] | None:
        snapshot = self.get_collection_snapshot(
            str(steamid),
            now=now,
            ttl_seconds=ttl_seconds,
            max_payload_bytes=max_payload_bytes,
        )
        if snapshot is None or snapshot.get("enrichment_payload") is None:
            return None
        if snapshot_id is not None and snapshot["snapshot_id"] != str(snapshot_id):
            return None
        return snapshot

    def upsert_identity_resolution(
        self,
        identity_key_hash: str,
        steamid: str,
        player_alias: str,
        *,
        resolved_at: float | None = None,
    ) -> None:
        if not _SHA256_RE.fullmatch(str(identity_key_hash)):
            raise ValueError("identity key hash must be a lowercase SHA-256 digest")
        if not str(steamid) or not str(player_alias):
            raise ValueError("identity resolution fields are required")
        timestamp = self.clock() if resolved_at is None else resolved_at
        if not math.isfinite(float(timestamp)):
            raise ValueError("identity resolution timestamp is invalid")
        self._connection.execute(
            """
            INSERT INTO identity_resolution_cache
                (identity_key_hash, steamid, player_alias, resolved_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(identity_key_hash) DO UPDATE SET
                steamid=excluded.steamid,
                player_alias=excluded.player_alias,
                resolved_at=excluded.resolved_at
            """,
            (str(identity_key_hash), str(steamid), str(player_alias), float(timestamp)),
        )
        self._connection.commit()

    def get_identity_resolution(self, identity_key_hash: str) -> dict[str, Any] | None:
        if not _SHA256_RE.fullmatch(str(identity_key_hash)):
            return None
        row = self._connection.execute(
            "SELECT * FROM identity_resolution_cache WHERE identity_key_hash = ?",
            (str(identity_key_hash),),
        ).fetchone()
        return dict(row) if row is not None else None

    def upsert_app_metadata(
        self,
        appid: int,
        metadata: Mapping[str, Any],
        *,
        status: str = "ok",
        fetched_at: float | None = None,
    ) -> None:
        fetched_at = self.clock() if fetched_at is None else fetched_at
        self._connection.execute(
            """
            INSERT INTO app_metadata (
                appid, name, type, genres_json, release_date, developers_json,
                publishers_json, platforms_json, categories_json, achievement_total,
                header_image_url, metadata_status, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(appid) DO UPDATE SET
                name=excluded.name, type=excluded.type,
                genres_json=excluded.genres_json, release_date=excluded.release_date,
                developers_json=excluded.developers_json,
                publishers_json=excluded.publishers_json,
                platforms_json=excluded.platforms_json,
                categories_json=excluded.categories_json,
                achievement_total=excluded.achievement_total,
                header_image_url=excluded.header_image_url,
                metadata_status=excluded.metadata_status, fetched_at=excluded.fetched_at
            """,
            (
                int(appid), str(metadata.get("name") or ""), metadata.get("type"),
                _json(metadata.get("genres") or []), metadata.get("release_date"),
                _json(metadata.get("developers") or []),
                _json(metadata.get("publishers") or []),
                _json(metadata.get("platforms") or {}),
                _json(metadata.get("categories") or []), metadata.get("achievement_total"),
                metadata.get("header_image_url"), status, float(fetched_at),
            ),
        )
        self._connection.commit()

    def get_app_metadata(self, appid: int) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM app_metadata WHERE appid = ?", (int(appid),)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        for column, fallback in (
            ("genres_json", []), ("developers_json", []), ("publishers_json", []),
            ("platforms_json", {}), ("categories_json", []),
        ):
            result[column.removesuffix("_json")] = _loads(result.pop(column), fallback)
        return result

    @staticmethod
    def _image_palette_key_parts(key: Any) -> tuple[str, str, int]:
        if isinstance(key, Mapping):
            content_sha256 = key.get("content_sha256")
            algorithm = key.get("algorithm")
            color_count = key.get("color_count")
        elif isinstance(key, (tuple, list)) and len(key) == 3:
            content_sha256, algorithm, color_count = key
        else:
            content_sha256 = getattr(key, "content_sha256", None)
            algorithm = getattr(key, "algorithm", None)
            color_count = getattr(key, "color_count", None)
        if not isinstance(content_sha256, str) or not _SHA256_RE.fullmatch(content_sha256):
            raise ValueError("image palette content_sha256 must be a lowercase SHA-256 digest")
        if not isinstance(algorithm, str) or not _PALETTE_ALGORITHM_RE.fullmatch(algorithm):
            raise ValueError("image palette algorithm is invalid")
        if isinstance(color_count, bool) or not isinstance(color_count, int) or not 1 <= color_count <= 32:
            raise ValueError("image palette color_count must be an integer between 1 and 32")
        return str(content_sha256), str(algorithm), int(color_count)

    @staticmethod
    def _valid_image_palette(
        palette: Any,
        content_sha256: str,
        algorithm: str,
        color_count: int,
    ) -> bool:
        def number(value: Any, *, minimum: float | None = None, maximum: float | None = None) -> bool:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False
            try:
                value = float(value)
            except (OverflowError, ValueError):
                return False
            if not math.isfinite(value):
                return False
            return (
                (minimum is None or value >= minimum)
                and (maximum is None or value <= maximum)
            )

        if not isinstance(palette, dict):
            return False
        if palette.get("algorithm") != algorithm:
            return False
        if palette.get("source_image_hash") != content_sha256:
            return False
        if palette.get("palette_space") != "oklch":
            return False
        if not number(palette.get("mean_luminance"), minimum=0.0, maximum=1.0):
            return False
        if not number(palette.get("mean_saturation"), minimum=0.0, maximum=1.0):
            return False
        if not number(palette.get("mean_chroma"), minimum=0.0):
            return False
        if not number(palette.get("mean_oklab_lightness"), minimum=0.0, maximum=1.0):
            return False
        valid_pixel_count = palette.get("valid_pixel_count")
        if (
            isinstance(valid_pixel_count, bool)
            or not isinstance(valid_pixel_count, int)
            or valid_pixel_count < 1
        ):
            return False
        dominant = palette.get("dominant_colors")
        if not isinstance(dominant, list) or not dominant or len(dominant) > color_count:
            return False
        for item in dominant:
            if not isinstance(item, dict):
                return False
            if not isinstance(item.get("hex"), str) or not re.fullmatch(r"#[0-9A-F]{6}", item["hex"]):
                return False
            rgb = item.get("rgb")
            if (
                not isinstance(rgb, list)
                or len(rgb) != 3
                or any(
                    isinstance(channel, bool)
                    or not isinstance(channel, int)
                    or not 0 <= channel <= 255
                    for channel in rgb
                )
            ):
                return False
            weight = item.get("weight")
            if not number(weight, minimum=0.0, maximum=1.0):
                return False
            if not number(item.get("luminance"), minimum=0.0, maximum=1.0):
                return False
            oklch = item.get("oklch")
            if (
                not isinstance(oklch, list)
                or len(oklch) != 3
                or not number(oklch[0], minimum=0.0, maximum=1.0)
                or not number(oklch[1], minimum=0.0)
                or not number(oklch[2], minimum=0.0, maximum=360.0)
            ):
                return False
            if not number(item.get("chroma"), minimum=0.0):
                return False
        return True

    def get_image_palettes(self, keys: Iterable[Any]) -> dict[Any, dict[str, Any]]:
        """Batch-load integrity-checked image palettes by content cache key."""

        requested: list[tuple[Any, tuple[str, str, int]]] = []
        seen: set[tuple[str, str, int]] = set()
        for key in keys:
            parts = self._image_palette_key_parts(key)
            if parts not in seen:
                requested.append((key, parts))
                seen.add(parts)
        if not requested:
            return {}

        rows: list[sqlite3.Row] = []
        # Three bound parameters per key leave room below SQLite's usual
        # variable limit and keep this a small number of batch queries.
        for offset in range(0, len(requested), 300):
            batch = requested[offset:offset + 300]
            clauses = ", ".join("(?, ?, ?)" for _ in batch)
            parameters = [value for _, parts in batch for value in parts]
            rows.extend(
                self._connection.execute(
                    f"SELECT content_sha256, algorithm, color_count, palette_json, computed_at "
                    f"FROM image_palette WHERE (content_sha256, algorithm, color_count) IN ({clauses})",
                    parameters,
                ).fetchall()
            )

        requested_keys: dict[tuple[str, str, int], Any] = {}
        for original, parts in requested:
            try:
                hash(original)
            except TypeError:
                requested_keys[parts] = parts
            else:
                requested_keys[parts] = original
        result: dict[Any, dict[str, Any]] = {}
        damaged: list[tuple[str, str, int]] = []
        for row in rows:
            raw_parts = (row["content_sha256"], row["algorithm"], row["color_count"])
            try:
                parts = self._image_palette_key_parts(raw_parts)
            except (TypeError, ValueError):
                damaged.append(raw_parts)  # type: ignore[arg-type]
                continue
            palette = _loads(row["palette_json"], None)
            if not self._valid_image_palette(palette, *parts):
                damaged.append(parts)
                continue
            result[requested_keys[parts]] = palette
        if damaged:
            with self._connection:
                self._connection.executemany(
                    "DELETE FROM image_palette WHERE content_sha256 = ? AND algorithm = ? AND color_count = ?",
                    damaged,
                )
        return result

    def upsert_image_palettes(self, rows: Any) -> None:
        """Batch-write complete image palettes in one parent-process transaction."""

        if isinstance(rows, Mapping):
            iterable = [(key, value) for key, value in rows.items()]
        else:
            iterable = list(rows)
        normalized: list[tuple[str, str, int, str, float]] = []
        for row in iterable:
            computed_at: float | None = None
            if isinstance(row, Mapping):
                key = row.get("key") or row.get("cache_key")
                palette = row.get("palette")
                computed_at = row.get("computed_at")
            elif isinstance(row, (tuple, list)) and len(row) in {2, 3}:
                key, palette = row[0], row[1]
                if len(row) == 3:
                    computed_at = row[2]
            else:
                raise ValueError("image palette rows must contain a cache key and palette")
            parts = self._image_palette_key_parts(key)
            if not self._valid_image_palette(palette, *parts):
                raise ValueError("image palette does not match its cache key")
            normalized.append(
                (*parts, _json(palette), float(self.clock() if computed_at is None else computed_at))
            )
        if not normalized:
            return
        with self._connection:
            self._connection.executemany(
                """
                INSERT INTO image_palette
                    (content_sha256, algorithm, color_count, palette_json, computed_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(content_sha256, algorithm, color_count) DO UPDATE SET
                    palette_json=excluded.palette_json,
                    computed_at=excluded.computed_at
                """,
                normalized,
            )

    def upsert_steam_artwork(
        self,
        source_url_hash: str,
        payload: bytes,
        content_type: str,
        *,
        fetched_at: float | None = None,
        max_bytes: int = 20 * 1024 * 1024,
    ) -> dict[str, Any]:
        """Store a verified Steam image as URL-hash to content-hash data."""

        if not re.fullmatch(r"[0-9a-f]{64}", str(source_url_hash)):
            raise ValueError("source_url_hash must be a lowercase SHA-256 digest")
        raw = bytes(payload)
        if not raw or len(raw) > int(max_bytes):
            raise ValueError("Steam artwork payload is empty or exceeds the size limit")
        mime = str(content_type or "").split(";", 1)[0].strip().lower()
        if mime not in {"image/jpeg", "image/png", "image/webp"}:
            raise ValueError("Steam artwork cache accepts JPEG, PNG, or WebP only")
        digest = hashlib.sha256(raw).hexdigest()
        timestamp = self.clock() if fetched_at is None else fetched_at
        with self._connection:
            previous = self._connection.execute(
                "SELECT content_sha256 FROM artwork_source WHERE source_url_hash = ?",
                (source_url_hash,),
            ).fetchone()
            self._connection.execute(
                """
                INSERT INTO artwork_content VALUES (?, ?, ?, ?)
                ON CONFLICT(content_sha256) DO UPDATE SET
                    payload=excluded.payload,
                    byte_size=excluded.byte_size,
                    stored_at=excluded.stored_at
                """,
                (digest, sqlite3.Binary(raw), len(raw), float(timestamp)),
            )
            self._connection.execute(
                """
                INSERT INTO artwork_source VALUES (?, ?, ?, ?)
                ON CONFLICT(source_url_hash) DO UPDATE SET
                    content_sha256=excluded.content_sha256,
                    content_type=excluded.content_type,
                    fetched_at=excluded.fetched_at
                """,
                (source_url_hash, digest, mime, float(timestamp)),
            )
            if previous is not None and previous["content_sha256"] != digest:
                self._connection.execute(
                    """
                    DELETE FROM artwork_content
                    WHERE content_sha256 = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM artwork_source
                          WHERE artwork_source.content_sha256 = artwork_content.content_sha256
                      )
                    """,
                    (previous["content_sha256"],),
                )
        return {
            "source_url_hash": source_url_hash,
            "content_sha256": digest,
            "content_type": mime,
            "byte_size": len(raw),
            "fetched_at": float(timestamp),
        }

    def get_steam_artwork(
        self,
        source_url_hash: str,
        *,
        max_age_seconds: float,
        allow_stale: bool = False,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        """Return integrity-checked Steam artwork, optionally including stale data."""

        row = self._connection.execute(
            """
            SELECT s.source_url_hash, s.content_sha256, s.content_type, s.fetched_at,
                   c.payload, c.byte_size
            FROM artwork_source AS s
            JOIN artwork_content AS c ON c.content_sha256 = s.content_sha256
            WHERE s.source_url_hash = ?
            """,
            (str(source_url_hash),),
        ).fetchone()
        if row is None:
            return None
        payload = bytes(row["payload"])
        valid_mime = row["content_type"] in {"image/jpeg", "image/png", "image/webp"}
        if not valid_mime:
            with self._connection:
                self._connection.execute(
                    "DELETE FROM artwork_source WHERE source_url_hash = ?",
                    (row["source_url_hash"],),
                )
            return None
        valid = (
            int(row["byte_size"]) > 0
            and int(row["byte_size"]) <= 20 * 1024 * 1024
            and len(payload) == int(row["byte_size"])
            and hashlib.sha256(payload).hexdigest() == row["content_sha256"]
        )
        if not valid:
            with self._connection:
                self._connection.execute(
                    "DELETE FROM artwork_content WHERE content_sha256 = ?",
                    (row["content_sha256"],),
                )
            return None
        current = self.clock() if now is None else now
        fresh = float(current) - float(row["fetched_at"]) < float(max_age_seconds)
        if not fresh and not allow_stale:
            return None
        return {
            "source_url_hash": row["source_url_hash"],
            "content_sha256": row["content_sha256"],
            "content_type": row["content_type"],
            "fetched_at": float(row["fetched_at"]),
            "byte_size": int(row["byte_size"]),
            "payload": payload,
            "cache_status": "cached" if fresh else "cached_stale",
        }

    def replace_achievement_schema(
        self,
        appid: int,
        achievements: Iterable[Mapping[str, Any]],
        *,
        status: str = "ok",
        fetched_at: float | None = None,
    ) -> None:
        fetched_at = self.clock() if fetched_at is None else fetched_at
        rows = list(achievements)
        with self._connection:
            self._connection.execute("DELETE FROM achievement_schema WHERE appid = ?", (int(appid),))
            self._connection.executemany(
                """
                INSERT INTO achievement_schema
                    (appid, apiname, display_name, description, hidden,
                     icon_url, icon_gray_url, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (int(appid), str(item["apiname"]), item.get("display_name"),
                     item.get("description"), int(bool(item.get("hidden"))),
                     item.get("icon_url"), item.get("icon_gray_url"), float(fetched_at))
                    for item in rows if item.get("apiname")
                ],
            )
            self._connection.execute(
                """INSERT INTO achievement_schema_state
                   (appid, status, fetched_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(appid) DO UPDATE SET
                       status=excluded.status,
                       fetched_at=excluded.fetched_at""",
                (int(appid), status, float(fetched_at)),
            )

    def get_achievement_schema(self, appid: int) -> dict[str, Any] | None:
        state = self._connection.execute(
            "SELECT * FROM achievement_schema_state WHERE appid = ?", (int(appid),)
        ).fetchone()
        if state is None:
            return None
        rows = self._connection.execute(
            "SELECT * FROM achievement_schema WHERE appid = ? ORDER BY apiname", (int(appid),)
        ).fetchall()
        return {**dict(state), "achievements": [dict(row) for row in rows]}

    def replace_achievement_global(
        self,
        appid: int,
        achievements: Iterable[Mapping[str, Any]],
        *,
        status: str = "ok",
        fetched_at: float | None = None,
    ) -> None:
        fetched_at = self.clock() if fetched_at is None else fetched_at
        rows = list(achievements)
        with self._connection:
            self._connection.execute("DELETE FROM achievement_global WHERE appid = ?", (int(appid),))
            self._connection.executemany(
                "INSERT INTO achievement_global VALUES (?, ?, ?, ?)",
                [
                    (int(appid), str(item["apiname"]), item.get("global_percent"), float(fetched_at))
                    for item in rows if item.get("apiname")
                ],
            )
            self._connection.execute(
                """INSERT INTO achievement_global_state VALUES (?, ?, ?)
                   ON CONFLICT(appid) DO UPDATE SET
                       status=excluded.status, fetched_at=excluded.fetched_at""",
                (int(appid), status, float(fetched_at)),
            )

    def get_achievement_global(self, appid: int) -> dict[str, Any] | None:
        state = self._connection.execute(
            "SELECT * FROM achievement_global_state WHERE appid = ?", (int(appid),)
        ).fetchone()
        if state is None:
            return None
        rows = self._connection.execute(
            "SELECT * FROM achievement_global WHERE appid = ? ORDER BY apiname", (int(appid),)
        ).fetchall()
        return {**dict(state), "achievements": [dict(row) for row in rows]}

    @staticmethod
    def _localized_locale(report_locale: str) -> str:
        from .locales import normalize_report_locale

        return normalize_report_locale(report_locale)

    def upsert_localized_app_label(
        self,
        appid: int,
        report_locale: str,
        display_name: str | None,
        *,
        status: str = "ok",
        fetched_at: float | None = None,
    ) -> None:
        locale = self._localized_locale(report_locale)
        timestamp = self.clock() if fetched_at is None else fetched_at
        if status == "ok" and not str(display_name or "").strip():
            status = "unavailable"
        self._connection.execute(
            """
            INSERT INTO localized_app_label
                (appid, report_locale, display_name, status, fetched_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(appid, report_locale) DO UPDATE SET
                display_name=excluded.display_name,
                status=excluded.status,
                fetched_at=excluded.fetched_at
            """,
            (int(appid), locale, str(display_name).strip() if display_name else None, status, float(timestamp)),
        )
        self._connection.commit()

    def get_localized_app_label(
        self,
        appid: int,
        report_locale: str,
        *,
        force: bool = False,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        locale = self._localized_locale(report_locale)
        row = self._connection.execute(
            "SELECT * FROM localized_app_label WHERE appid = ? AND report_locale = ?",
            (int(appid), locale),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        current = self.clock() if now is None else now
        ttl = LOCALIZED_APP_LABEL_TTL if result.get("status") == "ok" else LOCALIZED_LABEL_FAILURE_TTL
        if not force and not self.is_fresh(result, ttl, float(current)):
            return None
        if result.get("status") == "ok" and not str(result.get("display_name") or "").strip():
            return None
        result["cache_status"] = "cached" if result.get("status") == "ok" else "cached_failure"
        return result

    def replace_localized_achievement_labels(
        self,
        appid: int,
        report_locale: str,
        achievements: Iterable[Mapping[str, Any]],
        *,
        status: str = "ok",
        fetched_at: float | None = None,
    ) -> None:
        locale = self._localized_locale(report_locale)
        timestamp = self.clock() if fetched_at is None else fetched_at
        rows = [
            (
                int(appid),
                str(item.get("apiname") or item.get("name") or ""),
                locale,
                str(item.get("display_name") or item.get("displayName") or "").strip() or None,
                str(item.get("description") or "").strip() or None,
                float(timestamp),
            )
            for item in achievements
            if str(item.get("apiname") or item.get("name") or "").strip()
        ]
        with self._connection:
            self._connection.execute(
                "DELETE FROM localized_achievement_label WHERE appid = ? AND report_locale = ?",
                (int(appid), locale),
            )
            self._connection.executemany(
                """
                INSERT INTO localized_achievement_label
                    (appid, apiname, report_locale, display_name, description, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            self._connection.execute(
                """
                INSERT INTO localized_achievement_label_state
                    (appid, report_locale, status, fetched_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(appid, report_locale) DO UPDATE SET
                    status=excluded.status, fetched_at=excluded.fetched_at
                """,
                (int(appid), locale, status, float(timestamp)),
            )

    def get_localized_achievement_label_state(
        self,
        appid: int,
        report_locale: str,
        *,
        force: bool = False,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        locale = self._localized_locale(report_locale)
        row = self._connection.execute(
            "SELECT * FROM localized_achievement_label_state WHERE appid = ? AND report_locale = ?",
            (int(appid), locale),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        current = self.clock() if now is None else now
        ttl = LOCALIZED_ACHIEVEMENT_LABEL_TTL if result.get("status") == "ok" else LOCALIZED_LABEL_FAILURE_TTL
        if not force and not self.is_fresh(result, ttl, float(current)):
            return None
        return result

    def get_localized_achievement_label(
        self,
        appid: int,
        apiname: str,
        report_locale: str,
        *,
        force: bool = False,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        locale = self._localized_locale(report_locale)
        state = self.get_localized_achievement_label_state(
            appid, locale, force=force, now=now
        )
        if state is None:
            return None
        row = self._connection.execute(
            """
            SELECT * FROM localized_achievement_label
            WHERE appid = ? AND apiname = ? AND report_locale = ?
            """,
            (int(appid), str(apiname), locale),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        if state.get("status") == "ok" and not str(result.get("display_name") or "").strip():
            return None
        result["status"] = state["status"]
        result["cache_status"] = "cached" if state.get("status") == "ok" else "cached_failure"
        return result

    def get_user_games(self, steamid: str) -> dict[int, dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM user_games WHERE steamid = ?", (steamid,)
        ).fetchall()
        return {int(row["appid"]): dict(row) for row in rows}

    def replace_user_games(
        self,
        steamid: str,
        games: Iterable[Mapping[str, Any]],
        *,
        snapshot_at: float | None = None,
    ) -> None:
        snapshot_at = self.clock() if snapshot_at is None else snapshot_at
        rows = list(games)
        with self._connection:
            self._connection.execute("DELETE FROM user_games WHERE steamid = ?", (steamid,))
            self._connection.executemany(
                "INSERT INTO user_games VALUES (?, ?, ?, ?, ?)",
                [
                    (steamid, int(game["appid"]), int(game.get("playtime_forever") or 0),
                     game.get("name"), float(snapshot_at))
                    for game in rows
                ],
            )

    def replace_user_achievements(
        self,
        steamid: str,
        appid: int,
        achievements: Iterable[Mapping[str, Any]],
        *,
        playtime_forever: int,
        status: str = "ok",
        fetched_at: float | None = None,
    ) -> None:
        fetched_at = self.clock() if fetched_at is None else fetched_at
        rows = list(achievements)
        with self._connection:
            self._connection.execute(
                "DELETE FROM user_achievements WHERE steamid = ? AND appid = ?",
                (steamid, int(appid)),
            )
            self._connection.executemany(
                "INSERT INTO user_achievements VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (steamid, int(appid), str(item["apiname"]),
                     int(bool(item.get("achieved"))), int(item.get("unlocktime") or 0),
                     float(fetched_at))
                    for item in rows if item.get("apiname")
                ],
            )
            self._connection.execute(
                """INSERT INTO user_achievement_state VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(steamid, appid) DO UPDATE SET
                       status=excluded.status,
                       playtime_forever=excluded.playtime_forever,
                       fetched_at=excluded.fetched_at""",
                (steamid, int(appid), status, int(playtime_forever), float(fetched_at)),
            )

    def get_user_achievements(self, steamid: str, appid: int) -> dict[str, Any] | None:
        state = self._connection.execute(
            "SELECT * FROM user_achievement_state WHERE steamid = ? AND appid = ?",
            (steamid, int(appid)),
        ).fetchone()
        if state is None:
            return None
        rows = self._connection.execute(
            """SELECT * FROM user_achievements
               WHERE steamid = ? AND appid = ? ORDER BY apiname""",
            (steamid, int(appid)),
        ).fetchall()
        return {**dict(state), "achievements": [dict(row) for row in rows]}

    @staticmethod
    def _semantic_cache_key(
        identity_scope: str,
        game_input_fingerprint: str,
        analysis_contract_fingerprint: str,
        report_locale: str,
    ) -> tuple[str, str, str, str]:
        values = (
            str(identity_scope).strip(),
            str(game_input_fingerprint).strip(),
            str(analysis_contract_fingerprint).strip(),
            str(report_locale).strip(),
        )
        if not all(values):
            raise ValueError("achievement semantic cache key fields are required")
        for label, value in (
            ("game input fingerprint", values[1]),
            ("analysis contract fingerprint", values[2]),
        ):
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
                raise ValueError(f"{label} is not a SHA-256 fingerprint")
        if len(values[0]) > 256 or len(values[3]) > 32:
            raise ValueError("achievement semantic cache key field is too long")
        return values

    @staticmethod
    def _semantic_cache_payload(payload: Any) -> tuple[str, str, dict[str, Any]]:
        if not isinstance(payload, Mapping):
            raise ValueError("achievement semantic cache payload must be an object")
        game_id = str(payload.get("game_id") or "")
        if not re.fullmatch(r"game:[1-9][0-9]*", game_id):
            raise ValueError("achievement semantic cache payload has an invalid game ID")
        try:
            encoded = _json(dict(payload))
        except (TypeError, ValueError) as exc:
            raise ValueError("achievement semantic cache payload is not JSON serializable") from exc
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        return encoded, digest, dict(payload)

    def upsert_achievement_semantic_cache(
        self,
        identity_scope: str,
        game_input_fingerprint: str,
        analysis_contract_fingerprint: str,
        report_locale: str,
        payload: Mapping[str, Any],
        *,
        stored_at: float | None = None,
    ) -> None:
        """Store one validated per-game semantic result with no expiration."""

        key = self._semantic_cache_key(
            identity_scope,
            game_input_fingerprint,
            analysis_contract_fingerprint,
            report_locale,
        )
        encoded, digest, normalized = self._semantic_cache_payload(payload)
        timestamp = self.clock() if stored_at is None else stored_at
        if not math.isfinite(float(timestamp)):
            raise ValueError("achievement semantic cache timestamp is invalid")
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO achievement_semantic_cache (
                    identity_scope, game_input_fingerprint,
                    analysis_contract_fingerprint, report_locale, game_id,
                    payload_json, payload_sha256, stored_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    identity_scope, game_input_fingerprint,
                    analysis_contract_fingerprint, report_locale
                ) DO UPDATE SET
                    game_id=excluded.game_id,
                    payload_json=excluded.payload_json,
                    payload_sha256=excluded.payload_sha256,
                    stored_at=excluded.stored_at
                """,
                (*key, str(normalized["game_id"]), encoded, digest, float(timestamp)),
            )

    def get_achievement_semantic_cache(
        self,
        identity_scope: str,
        game_input_fingerprint: str,
        analysis_contract_fingerprint: str,
        report_locale: str,
    ) -> dict[str, Any] | None:
        """Return an integrity-checked semantic result, or delete a bad row."""

        key = self._semantic_cache_key(
            identity_scope,
            game_input_fingerprint,
            analysis_contract_fingerprint,
            report_locale,
        )
        row = self._connection.execute(
            """
            SELECT * FROM achievement_semantic_cache
            WHERE identity_scope = ? AND game_input_fingerprint = ?
              AND analysis_contract_fingerprint = ? AND report_locale = ?
            """,
            key,
        ).fetchone()
        if row is None:
            return None
        encoded = str(row["payload_json"])
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        try:
            payload = json.loads(encoded)
            if (
                digest != str(row["payload_sha256"])
                or not isinstance(payload, dict)
                or str(payload.get("game_id") or "") != str(row["game_id"])
            ):
                raise ValueError("semantic cache integrity mismatch")
            self._semantic_cache_payload(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            with self._connection:
                self._connection.execute(
                    """
                    DELETE FROM achievement_semantic_cache
                    WHERE identity_scope = ? AND game_input_fingerprint = ?
                      AND analysis_contract_fingerprint = ? AND report_locale = ?
                    """,
                    key,
                )
            return None
        return payload

    def delete_achievement_semantic_cache(
        self,
        identity_scope: str,
        game_input_fingerprint: str,
        analysis_contract_fingerprint: str,
        report_locale: str,
    ) -> int:
        key = self._semantic_cache_key(
            identity_scope,
            game_input_fingerprint,
            analysis_contract_fingerprint,
            report_locale,
        )
        with self._connection:
            cursor = self._connection.execute(
                """
                DELETE FROM achievement_semantic_cache
                WHERE identity_scope = ? AND game_input_fingerprint = ?
                  AND analysis_contract_fingerprint = ? AND report_locale = ?
                """,
                key,
            )
        return max(cursor.rowcount, 0)

    def record_run(
        self,
        run_id: str,
        steamid: str,
        player_alias: str,
        game_count: int,
        *,
        status: str = "ok",
        generated_at: float | None = None,
    ) -> None:
        generated_at = self.clock() if generated_at is None else generated_at
        self._connection.execute(
            "INSERT OR REPLACE INTO run_history VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, steamid, player_alias, float(generated_at), int(game_count), status),
        )
        self._connection.commit()

    def get_run_identity(self, run_id: str) -> dict[str, Any] | None:
        """Resolve a public run ID to its local private identity context."""
        row = self._connection.execute(
            "SELECT run_id, steamid, player_alias, generated_at FROM run_history WHERE run_id = ?",
            (str(run_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def put_editorial_bundle_for_run(
        self,
        run_id: str,
        evidence_fingerprint: str,
        visual_fingerprint: str,
        report_locale: str,
        bundle: Mapping[str, Any],
        generated_assets: Iterable[Mapping[str, Any]],
        *,
        deck_schema_fingerprint: str,
        completed_at: float | None = None,
    ) -> dict[str, Any]:
        """Atomically commit one final-QA editorial bundle for a private identity."""

        context = self.get_run_identity(run_id)
        if context is None:
            raise ValueError("Run identity context is unavailable from the local cache")
        report_locale = self._localized_locale(report_locale)
        if bundle.get("report_locale") != report_locale:
            raise ValueError("Editorial bundle locale does not match the cache key")
        for label, value in (
            ("evidence_fingerprint", evidence_fingerprint),
            ("visual_fingerprint", visual_fingerprint),
            ("deck_schema_fingerprint", deck_schema_fingerprint),
        ):
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(value)):
                raise ValueError(f"{label} is not a valid versioned SHA-256 fingerprint")
        encoded_bundle = _json(bundle)
        bundle_sha256 = hashlib.sha256(encoded_bundle.encode("utf-8")).hexdigest()
        assets: list[tuple[str, bytes, str, str]] = []
        for item in generated_assets:
            asset_id = str(item.get("asset_id") or "")
            payload = bytes(item.get("payload") or b"")
            record = item.get("record")
            if not re.fullmatch(r"generated:sha256:[0-9a-f]{64}", asset_id):
                raise ValueError("Editorial cache asset ID is invalid")
            if not payload or len(payload) > 20 * 1024 * 1024:
                raise ValueError("Editorial cache asset is empty or exceeds the size limit")
            digest = hashlib.sha256(payload).hexdigest()
            if not isinstance(record, Mapping) or record.get("sha256") != digest:
                raise ValueError("Editorial cache asset record does not match its PNG bytes")
            forbidden = {
                "path", "prompt", "source_path", "temporary_source_path",
                "session", "session_id", "steamid", "user_id",
            }
            if forbidden.intersection(str(key) for key in record):
                raise ValueError("Editorial cache asset record contains private or run-local fields")
            assets.append((asset_id, payload, digest, _json(record)))
        timestamp = self.clock() if completed_at is None else completed_at
        with self._connection:
            existing = self._connection.execute(
                """
                SELECT id FROM editorial_reuse_current
                WHERE steamid = ? AND evidence_fingerprint = ? AND visual_fingerprint = ?
                  AND deck_schema_fingerprint = ? AND report_locale = ?
                """,
                (
                    context["steamid"],
                    evidence_fingerprint,
                    visual_fingerprint,
                    deck_schema_fingerprint,
                    report_locale,
                ),
            ).fetchone()
            if existing is None:
                cursor = self._connection.execute(
                    """
                    INSERT INTO editorial_reuse_current (
                        steamid, evidence_fingerprint, visual_fingerprint,
                        deck_schema_fingerprint, report_locale, source_run_id, bundle_json, bundle_sha256, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        context["steamid"], evidence_fingerprint, visual_fingerprint,
                        deck_schema_fingerprint, report_locale, str(run_id), encoded_bundle, bundle_sha256, float(timestamp),
                    ),
                )
                entry_id = int(cursor.lastrowid)
            else:
                entry_id = int(existing["id"])
                self._connection.execute(
                    """
                    UPDATE editorial_reuse_current
                    SET source_run_id = ?, bundle_json = ?, bundle_sha256 = ?, completed_at = ?
                    WHERE id = ?
                    """,
                    (str(run_id), encoded_bundle, bundle_sha256, float(timestamp), entry_id),
                )
                self._connection.execute(
                    "DELETE FROM editorial_reuse_generated_current WHERE entry_id = ?",
                    (entry_id,),
                )
            self._connection.executemany(
                """
                INSERT INTO editorial_reuse_generated_current
                    (entry_id, asset_id, png_blob, byte_size, sha256, record_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (entry_id, asset_id, sqlite3.Binary(payload), len(payload), digest, record_json)
                    for asset_id, payload, digest, record_json in assets
                ],
            )
        return {
            "entry_id": entry_id,
            "source_run_id": str(run_id),
            "evidence_fingerprint": evidence_fingerprint,
            "visual_fingerprint": visual_fingerprint,
            "deck_schema_fingerprint": deck_schema_fingerprint,
            "report_locale": report_locale,
            "generated_assets": len(assets),
        }

    def get_editorial_bundle_for_run(
        self,
        run_id: str,
        evidence_fingerprint: str,
        visual_fingerprint: str,
        report_locale: str,
        *,
        deck_schema_fingerprint: str,
    ) -> dict[str, Any] | None:
        """Load one integrity-checked bundle scoped to the current run identity."""

        context = self.get_run_identity(run_id)
        if context is None:
            raise ValueError("Run identity context is unavailable from the local cache")
        report_locale = self._localized_locale(report_locale)
        row = self._connection.execute(
            """
            SELECT * FROM editorial_reuse_current
            WHERE steamid = ? AND evidence_fingerprint = ? AND visual_fingerprint = ?
              AND deck_schema_fingerprint = ? AND report_locale = ?
            """,
            (context["steamid"], evidence_fingerprint, visual_fingerprint, deck_schema_fingerprint, report_locale),
        ).fetchone()
        if row is None:
            return None
        bundle_json = str(row["bundle_json"])
        if hashlib.sha256(bundle_json.encode("utf-8")).hexdigest() != row["bundle_sha256"]:
            with self._connection:
                self._connection.execute("DELETE FROM editorial_reuse_current WHERE id = ?", (row["id"],))
            return None
        try:
            bundle = json.loads(bundle_json)
        except json.JSONDecodeError:
            with self._connection:
                self._connection.execute("DELETE FROM editorial_reuse_current WHERE id = ?", (row["id"],))
            return None
        asset_rows = self._connection.execute(
            "SELECT * FROM editorial_reuse_generated_current WHERE entry_id = ? ORDER BY asset_id",
            (row["id"],),
        ).fetchall()
        assets = []
        for asset_row in asset_rows:
            payload = bytes(asset_row["png_blob"])
            digest = hashlib.sha256(payload).hexdigest()
            if len(payload) != int(asset_row["byte_size"]) or digest != asset_row["sha256"]:
                with self._connection:
                    self._connection.execute("DELETE FROM editorial_reuse_current WHERE id = ?", (row["id"],))
                return None
            record = _loads(asset_row["record_json"], None)
            if not isinstance(record, dict) or record.get("sha256") != digest:
                with self._connection:
                    self._connection.execute("DELETE FROM editorial_reuse_current WHERE id = ?", (row["id"],))
                return None
            assets.append(
                {
                    "asset_id": asset_row["asset_id"],
                    "payload": payload,
                    "record": record,
                }
            )
        return {
            "source_run_id": row["source_run_id"],
            "evidence_fingerprint": row["evidence_fingerprint"],
            "visual_fingerprint": row["visual_fingerprint"],
            "deck_schema_fingerprint": row["deck_schema_fingerprint"],
            "report_locale": row["report_locale"],
            "bundle": bundle,
            "generated_assets": assets,
        }

    def purge_user(self, steamid: str) -> int:
        """Delete every row associated with one Steam identity."""
        deleted = 0
        with self._connection:
            for table in (
                "editorial_reuse_current", "user_achievements", "user_achievement_state",
                "user_games", "run_history", "user_acquisition_snapshot",
            ):
                cursor = self._connection.execute(f"DELETE FROM {table} WHERE steamid = ?", (steamid,))
                deleted += max(cursor.rowcount, 0)
            cursor = self._connection.execute(
                "DELETE FROM identity_resolution_cache WHERE steamid = ?", (steamid,)
            )
            deleted += max(cursor.rowcount, 0)
            cursor = self._connection.execute(
                "DELETE FROM achievement_semantic_cache WHERE identity_scope = ?",
                (str(steamid),),
            )
            deleted += max(cursor.rowcount, 0)
        return deleted

    def purge_global(self) -> int:
        """Delete all shared enrichment data while retaining user snapshots."""
        deleted = 0
        with self._connection:
            for table in (
                "app_metadata", "image_palette", "achievement_schema",
                "achievement_schema_state", "achievement_global", "achievement_global_state",
                "artwork_source", "artwork_content", "localized_app_label",
                "localized_achievement_label", "localized_achievement_label_state",
            ):
                cursor = self._connection.execute(f"DELETE FROM {table}")
                deleted += max(cursor.rowcount, 0)
        return deleted
