"""Persist reviewed element membership using stable slide IDs and text identity."""

import json
import sqlite3
from pathlib import Path

from slideguard.adapters.storage.sqlite import _document_key, _slide_ids
from slideguard.domain.models import ElementGroup
from slideguard.planning.element_groups import eligible_shapes, identity


class SqliteElementGroupRepository:
    def __init__(self, database_path):
        self.database_path = Path(database_path)

    def _slides(self, deck):
        if len(deck.slide_fingerprints) == deck.slide_count:
            return tuple(str(item[0]) for item in deck.slide_fingerprints)
        return _slide_ids(Path(deck.source_path))

    def save(self, deck, groups):
        slides = self._slides(deck)
        shapes = eligible_shapes(deck)
        records = []
        for group in groups:
            if not group.manual:
                continue

            def addresses(keys):
                return [
                    [slides[shapes[k].slide_number - 1], shapes[k].shape_id, identity(shapes[k])]
                    for k in keys
                    if k in shapes
                ]

            records.append(
                {
                    "id": group.id,
                    "title": group.title,
                    "members": addresses(group.members),
                    "excluded": addresses(group.excluded),
                }
            )
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS element_groups (document TEXT PRIMARY KEY, groups_json TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO element_groups VALUES (?, ?) ON CONFLICT(document) DO UPDATE SET groups_json=excluded.groups_json",
                (_document_key(Path(deck.source_path)), json.dumps(records, ensure_ascii=False)),
            )

    def load(self, deck):
        if not self.database_path.exists():
            return []
        with sqlite3.connect(self.database_path.as_uri() + "?mode=ro", uri=True) as connection:
            if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='element_groups' AND type='table'"
            ).fetchone():
                return []
            row = connection.execute(
                "SELECT groups_json FROM element_groups WHERE document=?",
                (_document_key(Path(deck.source_path)),),
            ).fetchone()
        if row is None:
            return []
        slides = self._slides(deck)
        positions = {str(slide_id): n for n, slide_id in enumerate(slides, 1)}
        shapes = eligible_shapes(deck)

        def resolve(addresses):
            keys = []
            for slide_id, shape_id, signature in addresses:
                key = f"{positions.get(str(slide_id), 0)}:{shape_id}"
                if key in shapes and identity(shapes[key]) == signature:
                    keys.append(key)
            return tuple(keys)

        return [
            ElementGroup(item["id"], item["title"], resolve(item["members"]), resolve(item["excluded"]), True)
            for item in json.loads(row[0])
        ]
