import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Optional

import httpx

from config import API_KEYS, CREDITS_REFRESH_INTERVAL, MIN_CREDITS, UPSTREAM_BASE_URL

logger = logging.getLogger(__name__)

CACHE_FILE = "keys_cache.json"


def _load_cache() -> dict:
    """Load key credits/status cache from file."""
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(keys: list) -> None:
    """Persist key credits/status to file (exclude actual key values)."""
    try:
        cache = {}
        for e in keys:
            cache[e["key"]] = {
                "credits": e["credits"],
                "active": e["active"],
                "last_checked": e["last_checked"].isoformat() if e["last_checked"] else None,
            }
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception as exc:
        logger.warning("Failed to save cache: %s", exc)


def _fetch_credits_sync(key: str) -> int:
    """Synchronous credits fetch — runs in a thread pool to avoid blocking the event loop."""
    url = f"{UPSTREAM_BASE_URL}/client/openapi/getAPIKeyCredits"
    try:
        with httpx.Client(timeout=8) as client:
            resp = client.post(
                url,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json={"apiKey": key},
            )
        data = resp.json()
        if data.get("code") != 0 or not isinstance(data.get("data"), dict):
            logger.warning("Credits API error for key ...%s: %s", key[-6:], data.get("msg", "unknown"))
            return 0
        raw = data["data"]
        return int(raw.get("credits") or raw.get("residue") or raw.get("balance") or 0)
    except Exception as exc:
        logger.warning("Failed to fetch credits for key ...%s: %s", key[-6:], exc)
        return 0


class KeyManager:
    def __init__(self):
        cache = _load_cache()
        self.keys = []
        for k in API_KEYS:
            cached = cache.get(k, {})
            last_checked = None
            if cached.get("last_checked"):
                try:
                    last_checked = datetime.fromisoformat(cached["last_checked"])
                except Exception:
                    pass
            self.keys.append({
                "key": k,
                "credits": cached.get("credits", 0),
                "active": cached.get("active", True),
                "last_checked": last_checked,
            })
        self.current_index = 0
        self._lock = asyncio.Lock()
        logger.info("KeyManager loaded %d keys (%d from cache)", len(self.keys), len(cache))

    def get_next_key(self, cost: int = 0) -> Optional[str]:
        """Round-robin: return first active key with enough credits for the request."""
        if not self.keys:
            return None
        n = len(self.keys)
        required = max(MIN_CREDITS, cost)
        for i in range(n):
            idx = (self.current_index + i) % n
            entry = self.keys[idx]
            if entry["active"] and entry["credits"] >= required:
                self.current_index = (idx + 1) % n
                return entry["key"]
        return None

    def deduct_credits(self, key: str, cost: int) -> None:
        """Deduct credits locally after a successful request."""
        if cost <= 0:
            return
        for entry in self.keys:
            if entry["key"] == key:
                entry["credits"] = max(0, entry["credits"] - cost)
                entry["active"] = entry["credits"] > MIN_CREDITS
                logger.info(
                    "Key ...%s deducted %d credits, remaining=%d, active=%s",
                    key[-6:], cost, entry["credits"], entry["active"],
                )
                _save_cache(self.keys)
                break

    async def refresh_credits(self, key: str) -> None:
        """Fetch credits in a thread pool so the event loop stays free."""
        credits = await asyncio.to_thread(_fetch_credits_sync, key)
        async with self._lock:
            for entry in self.keys:
                if entry["key"] == key:
                    entry["credits"] = credits
                    entry["active"] = credits > MIN_CREDITS
                    entry["last_checked"] = datetime.utcnow()
                    logger.info(
                        "Key ...%s | credits=%d | active=%s",
                        key[-6:],
                        credits,
                        entry["active"],
                    )
                    break
            # 删除积分 <= MIN_CREDITS 的 Key
            before = len(self.keys)
            self.keys = [e for e in self.keys if e["credits"] > MIN_CREDITS or e["last_checked"] is None]
            removed = before - len(self.keys)
            if removed > 0:
                logger.info("Removed %d key(s) with credits <= %d", removed, MIN_CREDITS)
                self.current_index = self.current_index % max(len(self.keys), 1)
            _save_cache(self.keys)

    async def refresh_all_credits(self) -> None:
        """Concurrently refresh credits for all keys."""
        await asyncio.gather(*[self.refresh_credits(e["key"]) for e in self.keys], return_exceptions=True)

    def start_background_refresh(self) -> None:
        """Launch a background asyncio task that refreshes credits periodically."""
        asyncio.create_task(self._background_loop())

    async def _background_loop(self) -> None:
        logger.info("Background: initial credits refresh...")
        await self.refresh_all_credits()
        while True:
            await asyncio.sleep(CREDITS_REFRESH_INTERVAL)
            logger.info("Background: refreshing all key credits...")
            await self.refresh_all_credits()

    def list_keys(self) -> list:
        """Return a sanitised view of all keys (masked) for the admin endpoint."""
        return [
            {
                "key_hint": f"...{e['key'][-6:]}",
                "credits": e["credits"],
                "active": e["active"],
                "last_checked": e["last_checked"].isoformat() if e["last_checked"] else None,
            }
            for e in self.keys
        ]


key_manager = KeyManager()
