"""GCS JSON state + dashboard feed with generation-precondition locks."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("trader.state")

DEFAULT_STATE: dict[str, Any] = {
    "version": 2,
    "paused": False,
    "positions": {},
    "slots": {},
    "meta": {},
    "closed_trades": [],
    "expectation_log": [],
}


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
        self._generation: int | None = None

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
                    blob.reload()
                    self._generation = blob.generation
                    log.info(
                        "state_loaded gcs=%s/%s gen=%s",
                        self.bucket,
                        self.object_name,
                        self._generation,
                    )
                    data = json.loads(blob.download_as_text())
                    data.setdefault("paused", False)
                    data.setdefault("closed_trades", [])
                    return data
                self._generation = 0  # create
            except Exception as e:  # noqa: BLE001
                log.warning("gcs_load_failed fallback_local err=%s", e)
        if self.local_path.is_file():
            data = json.loads(self.local_path.read_text())
            data.setdefault("paused", False)
            return data
        return json.loads(json.dumps(DEFAULT_STATE))

    def save(self, state: dict[str, Any], *, if_generation_match: int | None = None) -> None:
        payload = json.dumps(state, indent=2, ensure_ascii=False) + "\n"
        self.local_path.parent.mkdir(parents=True, exist_ok=True)
        self.local_path.write_text(payload)
        if self.bucket:
            try:
                blob = self._gcs().bucket(self.bucket).blob(self.object_name)
                kwargs: dict[str, Any] = {"content_type": "application/json"}
                gen = if_generation_match if if_generation_match is not None else self._generation
                if gen is not None:
                    kwargs["if_generation_match"] = gen
                blob.upload_from_string(payload, **kwargs)
                blob.reload()
                self._generation = blob.generation
                log.info("state_saved gcs=%s/%s gen=%s", self.bucket, self.object_name, self._generation)
            except Exception as e:  # noqa: BLE001
                log.error("gcs_save_failed local_only err=%s", e)
                raise

    def mutate(self, fn: Callable[[dict], dict], retries: int = 5) -> dict:
        """Load → transform → save with generation precondition; retry on race."""
        last_err: Exception | None = None
        for i in range(retries):
            state = self.load()
            gen = self._generation
            new_state = fn(json.loads(json.dumps(state)))  # deep-ish copy via json
            try:
                self.save(new_state, if_generation_match=gen)
                return new_state
            except Exception as e:  # noqa: BLE001
                last_err = e
                log.warning("state_mutate_retry i=%s err=%s", i, e)
                time.sleep(0.2 * (i + 1))
        raise RuntimeError(f"state_mutate_failed: {last_err}")

    def save_feed(
        self, feed: dict[str, Any], object_name: str = "trader/dashboard_feed.json"
    ) -> str:
        payload = json.dumps(feed, indent=2, ensure_ascii=False) + "\n"
        local = self.local_path.parent / Path(object_name).name
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

    def load_feed(self, object_name: str = "trader/dashboard_feed.json") -> dict | None:
        if self.bucket:
            try:
                blob = self._gcs().bucket(self.bucket).blob(object_name)
                if blob.exists():
                    return json.loads(blob.download_as_text())
            except Exception as e:  # noqa: BLE001
                log.warning("feed_load_failed err=%s", e)
        return None
