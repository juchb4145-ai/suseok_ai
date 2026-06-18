from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


SENSITIVE_KEYWORDS = ("password", "passwd", "비밀번호", "user_id", "userid", "token", "secret")


@dataclass(frozen=True)
class ChejanCaptureConfig:
    enabled: bool = False
    simulation_only: bool = True
    capture_dir: str = "reports/kiwoom_chejan"
    max_rows: int = 10000
    include_unknown_fids: bool = True

    @classmethod
    def from_env(cls) -> "ChejanCaptureConfig":
        return cls(
            enabled=_env_bool("TRADING_KIWOOM_CHEJAN_RAW_CAPTURE_ENABLED", False),
            simulation_only=_env_bool("TRADING_KIWOOM_CHEJAN_CAPTURE_SIMULATION_ONLY", True),
            capture_dir=os.getenv("TRADING_KIWOOM_CHEJAN_CAPTURE_DIR", "reports/kiwoom_chejan"),
            max_rows=max(1, _env_int("TRADING_KIWOOM_CHEJAN_CAPTURE_MAX_ROWS", 10000)),
            include_unknown_fids=_env_bool("TRADING_KIWOOM_CHEJAN_CAPTURE_INCLUDE_UNKNOWN_FIDS", True),
        )


class KiwoomChejanCaptureWriter:
    def __init__(self, config: ChejanCaptureConfig | None = None) -> None:
        self.config = config or ChejanCaptureConfig.from_env()
        self.capture_dir = Path(self.config.capture_dir).expanduser()
        self._count = 0

    def write(
        self,
        *,
        broker_env: str,
        gubun: str,
        item_count: int,
        fid_list: str,
        raw_fids: dict[str, Any],
        parser_result: dict[str, Any],
        gateway_session_id: str = "",
    ) -> dict[str, Any]:
        if not self.config.enabled:
            return {"written": False, "reason": "CAPTURE_DISABLED"}
        if self.config.simulation_only and str(broker_env or "").upper() == "REAL":
            raise RuntimeError("Kiwoom Chejan capture is forbidden for REAL broker")
        if self._count >= self.config.max_rows:
            return {"written": False, "reason": "CAPTURE_MAX_ROWS_REACHED"}
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        redacted_raw = redact_chejan_payload(dict(raw_fids or {}))
        redacted_result = redact_chejan_payload(dict(parser_result or {}))
        checksum = _checksum({"gubun": gubun, "raw_fids": redacted_raw, "parser_result": redacted_result})
        row = {
            "capture_id": f"chejan_{uuid4().hex}",
            "captured_at": _now(),
            "gateway_session_id": str(gateway_session_id or ""),
            "broker_env": str(broker_env or ""),
            "gubun": str(gubun or ""),
            "item_count": int(item_count or 0),
            "fid_list": str(fid_list or ""),
            "raw_fids": redacted_raw,
            "parser_result": redacted_result,
            "parser_version": str(redacted_result.get("parser_version") or redacted_result.get("parserVersion") or ""),
            "event_classification": str(redacted_result.get("event_kind") or redacted_result.get("eventKind") or ""),
            "fixture_checksum": checksum,
        }
        path = self.capture_dir / f"{row['capture_id']}.json"
        path.write_text(json.dumps(row, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
        self._count += 1
        return {"written": True, "path": str(path), "fixture_checksum": checksum}


def redact_chejan_payload(payload: Any) -> Any:
    if isinstance(payload, dict):
        result: dict[str, Any] = {}
        for key, value in payload.items():
            text_key = str(key)
            lowered = text_key.lower()
            if any(token in lowered for token in SENSITIVE_KEYWORDS):
                continue
            if text_key in {"9201", "account", "account_no"}:
                result[text_key] = _account_token(str(value or ""))
            else:
                result[text_key] = redact_chejan_payload(value)
        return result
    if isinstance(payload, list):
        return [redact_chejan_payload(item) for item in payload]
    return payload


def validate_redaction(payload: Any) -> dict[str, Any]:
    leaks = _sensitive_keys(payload)
    return {"ok": not leaks, "leaks": leaks}


def _sensitive_keys(payload: Any) -> list[str]:
    leaks: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            text_key = str(key)
            lowered = text_key.lower()
            if any(token in lowered for token in SENSITIVE_KEYWORDS):
                leaks.append(text_key)
            if text_key in {"9201", "account", "account_no"} and _looks_unredacted_account(value):
                leaks.append(text_key)
            leaks.extend(_sensitive_keys(value))
    elif isinstance(payload, list):
        for item in payload:
            leaks.extend(_sensitive_keys(item))
    return leaks


def _looks_unredacted_account(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return not text.startswith("ACC_TOKEN_")


def _account_token(account: str) -> str:
    text = str(account or "")
    if not text:
        return ""
    return "ACC_TOKEN_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _checksum(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default))).strip())
    except (TypeError, ValueError):
        return int(default)


__all__ = [
    "ChejanCaptureConfig",
    "KiwoomChejanCaptureWriter",
    "redact_chejan_payload",
    "validate_redaction",
]
