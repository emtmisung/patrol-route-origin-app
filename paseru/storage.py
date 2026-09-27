"""브라우저 작업보관 · 자동로그인 · 휴대폰 이어하기(일회용 QR) 저장 처리."""
import base64
import hashlib
import hmac
import io
import json
import re
import secrets
from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pandas as pd
import streamlit as st
from cryptography.fernet import Fernet
from cryptography.fernet import InvalidToken

from paseru.settings import (
    AUTH_REMEMBER_DAYS,
    BROWSER_DRAFT_KEY_PREFIX,
    MOBILE_TRANSFER_DIR,
    MOBILE_TRANSFER_MAX_BYTES,
    MOBILE_TRANSFER_TTL_SECONDS,
    app_password,
    mobile_transfer_secret,
)
from paseru.upload import find_address_column_index, find_name_column_index


def dataframe_to_draft(df):
    """DataFrame을 브라우저 저장용 JSON 문자열로 바꾼다."""
    if df is None:
        return None
    return df.to_json(orient="split", force_ascii=False, date_format="iso")


def dataframe_from_draft(value):
    """브라우저에 저장된 JSON을 안전한 크기의 DataFrame으로 복원한다."""
    if not isinstance(value, str) or not value or len(value) > 4_000_000:
        return None
    restored = pd.read_json(io.StringIO(value), orient="split")
    if len(restored) > 10_000 or len(restored.columns) > 100:
        return None
    return restored


def decode_browser_draft(raw_value):
    """7일 만료 여부와 기본 구조를 확인한 뒤 저장자료를 반환한다."""
    if not raw_value:
        return None, "empty"
    try:
        payload = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
        if not isinstance(payload, dict) or payload.get("version") != 1:
            return None, "invalid"
        expires_at = float(payload.get("expires_at", 0))
        if expires_at <= datetime.now().timestamp():
            return None, "expired"
        targets = dataframe_from_draft(payload.get("targets"))
        if targets is None or targets.empty:
            return None, "invalid"
        coords = dataframe_from_draft(payload.get("coords"))
        payload["targets_df"] = targets
        payload["coords_df"] = coords
        return payload, "ok"
    except (TypeError, ValueError, KeyError):
        return None, "invalid"


def browser_work_key(source_name, targets_df):
    """파일명과 대상목록으로 같은 작업을 계속 갱신할 저장 키를 만든다."""
    identity = f"{source_name or '업로드 자료'}\n{dataframe_to_draft(targets_df) or ''}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return f"{BROWSER_DRAFT_KEY_PREFIX}{digest}"


def browser_draft_content(source_name, patrol_title, station_query, station_result, targets_df,
                          coords_df, coord_api_calls):
    """변경 감지와 브라우저 저장에 사용할 최소 작업자료를 만든다."""
    return {
        "version": 1,
        "source_name": str(source_name or "업로드 자료"),
        "target_count": int(len(targets_df)) if targets_df is not None else 0,
        "patrol_title": str(patrol_title or ""),
        "station_query": str(station_query or ""),
        "station_result": station_result if isinstance(station_result, dict) else None,
        "targets": dataframe_to_draft(targets_df),
        "coords": dataframe_to_draft(coords_df),
        "coord_api_calls": int(coord_api_calls or 0),
    }


def route_execution_content(source_name, patrol_title, station_query, station_result,
                            targets_df, coords_df, coord_api_calls, station,
                            route_results, far_points, meta):
    """PC에서 생성한 최종 노선 결과를 휴대폰 현장 실행용으로 전달한다."""
    payload = browser_draft_content(
        source_name, patrol_title, station_query, station_result,
        targets_df, coords_df, coord_api_calls,
    )
    payload.update({
        "handoff_type": "route_execution",
        "station": station,
        "route_results": route_results,
        "far_points": far_points,
        "meta": meta,
    })
    return payload


def make_auth_remember_token():
    timestamp = str(int(datetime.now(timezone.utc).timestamp()))
    secret = f"{app_password()}|{mobile_transfer_secret()}"
    signature = hmac.new(
        secret.encode("utf-8"),
        f"paseru-auth:{timestamp}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"v1:{timestamp}:{signature}"


def valid_auth_remember_token(raw_token):
    if not app_password() or not raw_token:
        return False
    try:
        version, timestamp_text, signature = str(raw_token).split(":", 2)
        timestamp = int(timestamp_text)
    except (TypeError, ValueError):
        return False
    if version != "v1":
        return False
    now_ts = int(datetime.now(timezone.utc).timestamp())
    max_age = AUTH_REMEMBER_DAYS * 24 * 60 * 60
    if timestamp > now_ts + 300 or now_ts - timestamp > max_age:
        return False
    secret = f"{app_password()}|{mobile_transfer_secret()}"
    expected = hmac.new(
        secret.encode("utf-8"),
        f"paseru-auth:{timestamp}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


def browser_draft_label(payload):
    """저장된 작업 선택 목록에 표시할 한 줄 설명을 만든다."""
    source_name = payload.get("source_name") or payload.get("patrol_title") or "이전 저장자료"
    target_count = int(payload.get("target_count") or len(payload.get("targets_df", [])))
    try:
        saved_at = datetime.fromtimestamp(
            float(payload.get("saved_at", 0)), timezone.utc,
        ).astimezone(timezone(timedelta(hours=9)))
        saved_text = saved_at.strftime("%m월 %d일 %H:%M")
    except (TypeError, ValueError, OSError):
        saved_text = "저장시각 없음"
    return f"{source_name} · 대상 {target_count}건 · {saved_text}"


def minimum_transfer_targets(targets_df):
    """휴대폰 전달에는 대상명과 주소 열만 포함해 불필요한 원본 열을 제외한다."""
    columns = list(targets_df.columns)
    name_index = find_name_column_index(columns)
    address_index = find_address_column_index(columns, name_index, targets_df)
    return pd.DataFrame({
        "대상명": targets_df.iloc[:, name_index].copy(),
        "주소": targets_df.iloc[:, address_index].copy(),
    })


def mobile_transfer_cipher():
    """앱 비밀값으로 일회용 전달자료의 서버 임시파일을 암호화한다."""
    if not mobile_transfer_secret():
        return None
    key_material = hashlib.sha256(
        f"paseru-mobile-transfer-v1\n{mobile_transfer_secret()}".encode("utf-8"),
    ).digest()
    return Fernet(base64.urlsafe_b64encode(key_material))


def cleanup_mobile_transfers(now_timestamp=None):
    """10분이 지난 일회용 전달파일과 중단된 수신파일을 정리한다."""
    now_timestamp = float(now_timestamp or datetime.now().timestamp())
    MOBILE_TRANSFER_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    for transfer_path in list(MOBILE_TRANSFER_DIR.glob("*.transfer")) + list(
        MOBILE_TRANSFER_DIR.glob("*.claim")
    ):
        try:
            if transfer_path.stat().st_mtime < now_timestamp - MOBILE_TRANSFER_TTL_SECONDS - 60:
                transfer_path.unlink(missing_ok=True)
                continue
            if transfer_path.suffix == ".transfer":
                envelope = json.loads(transfer_path.read_text(encoding="utf-8"))
                if float(envelope.get("expires_at", 0)) <= now_timestamp:
                    transfer_path.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            transfer_path.unlink(missing_ok=True)


def create_mobile_transfer(draft_payload):
    """작업자료를 암호화해 10분짜리 일회용 전달 토큰으로 만든다."""
    cipher = mobile_transfer_cipher()
    if cipher is None:
        raise ValueError("앱 비밀번호가 설정되지 않아 전달자료를 암호화할 수 없습니다.")
    serialized = json.dumps(draft_payload, ensure_ascii=False, default=str).encode("utf-8")
    if len(serialized) > MOBILE_TRANSFER_MAX_BYTES:
        raise ValueError("작업자료가 휴대폰 일회용 전달 허용 크기를 초과했습니다.")

    cleanup_mobile_transfers()
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now().timestamp() + MOBILE_TRANSFER_TTL_SECONDS
    envelope = {
        "expires_at": expires_at,
        "ciphertext": cipher.encrypt(serialized).decode("ascii"),
    }
    final_path = MOBILE_TRANSFER_DIR / f"{token}.transfer"
    temporary_path = MOBILE_TRANSFER_DIR / f"{token}.{secrets.token_hex(6)}.tmp"
    temporary_path.write_text(json.dumps(envelope), encoding="utf-8")
    temporary_path.chmod(0o600)
    temporary_path.replace(final_path)
    return token, expires_at


def consume_mobile_transfer(token):
    """일회용 토큰의 작업을 한 번만 복원하고 임시파일을 즉시 삭제한다."""
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{40,60}", token):
        return None, "invalid"
    cipher = mobile_transfer_cipher()
    if cipher is None:
        return None, "unavailable"

    cleanup_mobile_transfers()
    transfer_path = MOBILE_TRANSFER_DIR / f"{token}.transfer"
    claim_path = MOBILE_TRANSFER_DIR / f"{token}.{secrets.token_hex(6)}.claim"
    try:
        transfer_path.replace(claim_path)
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "busy"

    try:
        envelope = json.loads(claim_path.read_text(encoding="utf-8"))
        if float(envelope.get("expires_at", 0)) <= datetime.now().timestamp():
            return None, "expired"
        decrypted = cipher.decrypt(envelope["ciphertext"].encode("ascii"))
        payload = json.loads(decrypted.decode("utf-8"))
        draft, draft_status = decode_browser_draft(payload)
        if draft_status != "ok":
            return None, "invalid"
        return draft, "ok"
    except (InvalidToken, KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None, "invalid"
    finally:
        claim_path.unlink(missing_ok=True)


def apply_browser_draft(payload, storage_key):
    """선택한 브라우저 저장 작업을 현재 세션으로 안전하게 전환한다."""
    restored_targets = payload["targets_df"].copy()
    st.session_state["browser_restored_df"] = restored_targets
    st.session_state["patrol_title"] = payload.get("patrol_title") or ""
    st.session_state["station_query"] = payload.get("station_query") or ""

    restored_station = payload.get("station_result")
    if isinstance(restored_station, dict):
        st.session_state["station_search_result"] = restored_station
    else:
        st.session_state.pop("station_search_result", None)

    restored_coords = payload.get("coords_df")
    if restored_coords is not None:
        st.session_state["coords_df"] = restored_coords.copy()
    else:
        st.session_state.pop("coords_df", None)
    st.session_state.pop("coord_future", None)
    st.session_state["coord_api_calls"] = int(payload.get("coord_api_calls", 0))

    restored_columns = list(restored_targets.columns)
    restored_name_idx = find_name_column_index(restored_columns)
    restored_addr_idx = find_address_column_index(restored_columns, restored_name_idx)
    st.session_state["coord_signature"] = tuple(
        (str(row[restored_columns[restored_name_idx]]),
         str(row[restored_columns[restored_addr_idx]]))
        for _, row in restored_targets.iterrows()
    )
    if payload.get("handoff_type") == "route_execution" and payload.get("route_results"):
        st.session_state["station"] = payload.get("station") or restored_station
        st.session_state["route_results"] = payload.get("route_results") or []
        st.session_state["far_points"] = payload.get("far_points") or []
        st.session_state["meta"] = payload.get("meta") or {}
        st.session_state["route_execution_mode"] = True
    else:
        for stale_key in ("station", "route_results", "far_points", "meta", "route_execution_mode"):
            st.session_state.pop(stale_key, None)

    st.session_state["active_browser_draft_key"] = storage_key
    st.session_state["browser_source_name"] = (
        payload.get("source_name") or payload.get("patrol_title") or "이전 저장자료"
    )
    st.session_state["browser_draft_saving_enabled"] = True
    st.session_state.pop("browser_draft_fingerprint", None)
    st.session_state.pop("browser_upload_signature", None)
    st.session_state["file_uploader_generation"] = (
        int(st.session_state.get("file_uploader_generation", 0)) + 1
    )
