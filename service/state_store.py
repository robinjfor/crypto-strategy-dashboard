"""GCS JSON state + dashboard feed; local fallback for dry-run."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("trader.state")

DEFAULT_STATE: dict[str, Any] = {"version": 1, "positions": {}, "slots": {}, "meta": {}}


class StateStore:
    def __init__(
        self,
        bucket: str | None = None,
        object_name: str = "trader/state.json",
        local_path: str | Path | None = None,
    ):
        self.bucket = bucket or os.environ.get("GCS_BUCKET") or ""
        self.object_name = object_name
        self.local_path = Path(
            local_path or os.environ.get("STATE_LOCAL_PATH") or "/tmp/trader_state.json"
        )
        self._client = None

    def _gcs(self):
        if self._client is None:
            from google.cloud import storage

            self._client = storage.Client()
        return self._client

    def load(self) -> dict[str, Any]:
        if self.bucket:
            try:
                blob = self._gcs().bucket(self.bucket).blob(self.object_name)
                if blob.exists():
                    log.info("state_loaded gcs=%s/%s", self.bucket, self.object_name)
                    return json.loads(blob.download_as_text())
            except Exception as e:  # noqa: BLE001
                log.warning("gcs_load_failed fallback_local err=%s", e)
        if self.local_path.is_file():
            return json.loads(self.local_path.read_text())
        return json.loads(json.dumps(DEFAULT_STATE))

    def save(self, state: dict[str, Any]) -> None:
        payload = json.dumps(state, indent=2, ensure_ascii=False) + "\n"
        self.local_path.parent.mkdir(parents=True, exist_ok=True)
        self.local_path.write_text(payload)
        if self.bucket:
            try:
                blob = self._gcs().bucket(self.bucket).blob(self.object_name)
                blob.upload_from_string(payload, content_type="application/json")
                log.info("state_saved gcs=%s/%s", self.bucket, self.object_name)
            except Exception as e:  # noqa: BLE001
                log.error("gcs_save_failed local_only err=%s", e)

    def save_feed(
        self, feed: dict[str, Any], object_name: str = "trader/dashboard_feed.json"
    ) -> str:
        payload = json.dumps(feed, indent=2, ensure_ascii=False) + "\n"
        local = self.local_path.parent / "dashboard_feed.json"
        local.write_text(payload)
        if not self.bucket:
            return str(local)
        try:
            blob = self._gcs().bucket(self.bucket).blob(object_name)
            blob.upload_from_string(payload, content_type="application/json")
            try:
                blob.make_public()
                return blob.public_url
            except Exception:  # noqa: BLE001
                return f"gs://{self.bucket}/{object_name}"
        except Exception as e:  # noqa: BLE001
            log.error("feed_save_failed err=%s", e)
            return str(local)
