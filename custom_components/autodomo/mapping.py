"""Traducao entre entidades do Home Assistant e o modelo do AutoDomo.

Contrato: AutoDomo/autodomo-cloud docs/modelo-dados.md. `kind` = dominio do
HA (input_boolean vira `switch`); `caps` descreve o que o dispositivo faz;
`state` leva so' o que o app precisa mostrar.
"""

from __future__ import annotations

from typing import Any

from homeassistant.const import ATTR_SUPPORTED_FEATURES, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import State

# Estados de cover/lock sairam de homeassistant.const (HA 2025+; viraram
# CoverState/LockState nas plataformas) - literais evitam depender da versao.
COVER_STATES = ("open", "closed", "opening", "closing")
STATE_LOCKED = "locked"

# Chaves do RTDB nao aceitam . # $ [ ] /
def device_id_for(entity_id: str) -> str:
    return entity_id.replace(".", "_")


KINDS = (
    "light", "switch", "cover", "sensor", "binary_sensor", "climate", "lock", "fan", "scene", "button",
    "media_player", "vacuum", "alarm_control_panel", "valve", "water_heater", "humidifier",
)
# Dominios com '_' no nome primeiro: "binary_sensor_x" nao pode virar "binary.sensor_x".
_DOMAINS = sorted({*KINDS, "input_boolean"}, key=len, reverse=True)


def entity_id_for(device_id: str) -> str | None:
    """Inverso de device_id_for: o prefixo e' um dominio conhecido."""
    for domain in _DOMAINS:
        if device_id.startswith(domain + "_") and len(device_id) > len(domain) + 1:
            return f"{domain}.{device_id[len(domain) + 1:]}"
    return None


def kind_for(domain: str) -> str | None:
    if domain == "input_boolean":
        return "switch"
    if domain in KINDS:
        return domain
    return None


def _num(v: Any) -> float | int | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else f


# Sem estado proprio: o "estado" e' a hora do ultimo acionamento, e "unknown"
# so' quer dizer que nunca foi acionado - nao que esta' offline.
STATELESS_DOMAINS = ("button", "scene")


def _available(state: State) -> bool:
    if state.domain in STATELESS_DOMAINS:
        return state.state != STATE_UNAVAILABLE
    return state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)


def device_payload(state: State, room: str | None) -> dict[str, Any] | None:
    """Documento `devices/{id}` para a entidade (None se o dominio nao e' suportado)."""
    domain = state.domain
    kind = kind_for(domain)
    if kind is None:
        return None
    attrs = state.attributes
    caps: dict[str, Any] = {}
    features = int(attrs.get(ATTR_SUPPORTED_FEATURES, 0) or 0)

    if kind == "light":
        modes = set(attrs.get("supported_color_modes") or [])
        caps["onoff"] = True
        if modes & {"brightness", "hs", "rgb", "rgbw", "rgbww", "xy", "color_temp", "white"}:
            caps["brightness"] = True
        if modes & {"hs", "rgb", "rgbw", "rgbww", "xy"}:
            caps["color_rgb"] = True
        if "color_temp" in modes:
            caps["color_temp"] = True
            if attrs.get("min_color_temp_kelvin"):
                caps["color_temp_min_k"] = attrs["min_color_temp_kelvin"]
            if attrs.get("max_color_temp_kelvin"):
                caps["color_temp_max_k"] = attrs["max_color_temp_kelvin"]
    elif kind == "switch":
        caps["onoff"] = True
    elif kind == "cover":
        caps["open_close"] = bool(features & 3)  # OPEN | CLOSE
        caps["stop"] = bool(features & 8)
        caps["position"] = bool(features & 4)
        caps["tilt"] = bool(features & 128)
        if attrs.get("device_class"):
            caps["device_class"] = attrs["device_class"]
    elif kind == "sensor":
        if attrs.get("unit_of_measurement"):
            caps["unit"] = attrs["unit_of_measurement"]
        if attrs.get("device_class"):
            caps["device_class"] = attrs["device_class"]
        if attrs.get("state_class"):
            caps["state_class"] = attrs["state_class"]
    elif kind == "binary_sensor":
        if attrs.get("device_class"):
            caps["device_class"] = attrs["device_class"]
    elif kind == "climate":
        caps["modes"] = list(attrs.get("hvac_modes") or [])
        caps["target_temp"] = True
        if attrs.get("min_temp") is not None:
            caps["min_temp"] = attrs["min_temp"]
        if attrs.get("max_temp") is not None:
            caps["max_temp"] = attrs["max_temp"]
        if attrs.get("target_temp_step"):
            caps["temp_step"] = attrs["target_temp_step"]
        if attrs.get("fan_modes"):
            caps["fan_modes"] = list(attrs["fan_modes"])
    elif kind == "lock":
        caps["lock_unlock"] = True
    elif kind == "fan":
        caps["onoff"] = True
        caps["percentage"] = bool(features & 1)
    elif kind == "scene":
        caps["activate"] = True
    elif kind == "button":
        caps["press"] = True
    elif kind == "media_player":
        caps["onoff"] = bool(features & 384)  # TURN_ON | TURN_OFF
        caps["play_pause"] = bool(features & 16385)  # PAUSE | PLAY
        caps["next"] = bool(features & 32)
        caps["previous"] = bool(features & 16)
        caps["volume"] = bool(features & 4)
        caps["mute"] = bool(features & 8)
        if features & 2048 and attrs.get("source_list"):
            caps["sources"] = [str(s) for s in attrs["source_list"]][:30]
        if attrs.get("device_class"):
            caps["device_class"] = attrs["device_class"]  # tv / speaker / receiver
    elif kind == "vacuum":
        caps["start"] = bool(features & 8192)
        caps["pause"] = bool(features & 4)
        caps["stop"] = bool(features & 8)
        caps["return_home"] = bool(features & 16)
        caps["locate"] = bool(features & 512)
        if features & 32 and attrs.get("fan_speed_list"):
            caps["fan_speeds"] = [str(s) for s in attrs["fan_speed_list"]][:12]
    elif kind == "alarm_control_panel":
        modes = [m for bit, m in ((1, "home"), (2, "away"), (4, "night"), (32, "vacation")) if features & bit]
        caps["arm_modes"] = modes or ["away"]
        # code_format: None (sem codigo), "number" ou "text"
        if attrs.get("code_format"):
            caps["code"] = attrs["code_format"]
            caps["code_arm_required"] = bool(attrs.get("code_arm_required", True))
    elif kind == "valve":
        caps["open_close"] = bool(features & 3)
        caps["stop"] = bool(features & 8)
        caps["position"] = bool(features & 4)
        if attrs.get("device_class"):
            caps["device_class"] = attrs["device_class"]  # water / gas
    elif kind == "water_heater":
        caps["onoff"] = bool(features & 8)
        caps["target_temp"] = bool(features & 1)
        if features & 2 and attrs.get("operation_list"):
            caps["modes"] = [str(m) for m in attrs["operation_list"]]
        if attrs.get("min_temp") is not None:
            caps["min_temp"] = attrs["min_temp"]
        if attrs.get("max_temp") is not None:
            caps["max_temp"] = attrs["max_temp"]
        if attrs.get("target_temp_step"):
            caps["temp_step"] = attrs["target_temp_step"]
    elif kind == "humidifier":
        caps["onoff"] = True
        caps["target_humidity"] = True
        if attrs.get("min_humidity") is not None:
            caps["min_humidity"] = attrs["min_humidity"]
        if attrs.get("max_humidity") is not None:
            caps["max_humidity"] = attrs["max_humidity"]
        if features & 1 and attrs.get("available_modes"):
            caps["modes"] = [str(m) for m in attrs["available_modes"]]
        if attrs.get("device_class"):
            caps["device_class"] = attrs["device_class"]  # humidifier / dehumidifier

    payload: dict[str, Any] = {
        "kind": kind,
        "name": state.name,
        "caps": caps or {"onoff": True},
        "source": {"entity_id": state.entity_id, "domain": domain},
    }
    if room:
        payload["room"] = room
    return payload


def state_payload(state: State, ts_ms: int) -> dict[str, Any]:
    """Documento `state/{id}`: pequeno, so' o que o app mostra."""
    kind = kind_for(state.domain) or state.domain
    attrs = state.attributes
    out: dict[str, Any] = {"ts": ts_ms, "available": _available(state)}

    if kind in ("light", "switch", "fan", "binary_sensor"):
        out["on"] = state.state == STATE_ON
    if kind == "light":
        if attrs.get("brightness") is not None:
            out["brightness"] = int(attrs["brightness"])
        if attrs.get("rgb_color"):
            out["rgb"] = [int(c) for c in attrs["rgb_color"]]
        if attrs.get("color_temp_kelvin"):
            out["color_temp"] = int(attrs["color_temp_kelvin"])
    elif kind == "cover":
        out["state"] = state.state if state.state in COVER_STATES else "unknown"
        if attrs.get("current_position") is not None:
            out["position"] = int(attrs["current_position"])
        if attrs.get("current_tilt_position") is not None:
            out["tilt"] = int(attrs["current_tilt_position"])
    elif kind == "sensor":
        value: Any = state.state
        try:
            value = float(state.state)
            if value.is_integer():
                value = int(value)
        except (TypeError, ValueError):
            pass
        out["value"] = value
        if attrs.get("unit_of_measurement"):
            out["unit"] = attrs["unit_of_measurement"]
    elif kind == "climate":
        out["mode"] = state.state
        if attrs.get("temperature") is not None:
            out["target_temp"] = attrs["temperature"]
        if attrs.get("current_temperature") is not None:
            out["current_temp"] = attrs["current_temperature"]
        if attrs.get("fan_mode"):
            out["fan"] = attrs["fan_mode"]
        if attrs.get("hvac_action"):
            out["action"] = attrs["hvac_action"]
    elif kind == "lock":
        out["locked"] = state.state == STATE_LOCKED
        out["state"] = state.state
    elif kind == "fan":
        if attrs.get("percentage") is not None:
            out["percentage"] = int(attrs["percentage"])
    elif kind == "media_player":
        out["state"] = state.state  # off/on/idle/playing/paused/standby/buffering
        out["on"] = state.state not in ("off", "standby", STATE_UNAVAILABLE, STATE_UNKNOWN)
        if attrs.get("volume_level") is not None:
            out["volume"] = round(float(attrs["volume_level"]) * 100)
        if attrs.get("is_volume_muted") is not None:
            out["muted"] = bool(attrs["is_volume_muted"])
        for key, src in (("title", "media_title"), ("artist", "media_artist"), ("source", "source"), ("app", "app_name")):
            if attrs.get(src):
                out[key] = str(attrs[src])[:120]
    elif kind == "vacuum":
        out["state"] = state.state  # cleaning/docked/paused/idle/returning/error
        if attrs.get("battery_level") is not None:
            out["battery"] = int(attrs["battery_level"])
        if attrs.get("fan_speed"):
            out["fan_speed"] = str(attrs["fan_speed"])
    elif kind == "alarm_control_panel":
        out["state"] = state.state  # disarmed/armed_home/armed_away/armed_night/armed_vacation/arming/pending/triggered
    elif kind == "valve":
        out["state"] = state.state if state.state in COVER_STATES else "unknown"
        if attrs.get("current_position") is not None:
            out["position"] = int(attrs["current_position"])
    elif kind == "water_heater":
        out["mode"] = state.state
        if _num(attrs.get("temperature")) is not None:
            out["target_temp"] = _num(attrs["temperature"])
        if _num(attrs.get("current_temperature")) is not None:
            out["current_temp"] = _num(attrs["current_temperature"])
    elif kind == "humidifier":
        out["on"] = state.state == STATE_ON
        if _num(attrs.get("humidity")) is not None:
            out["target_humidity"] = _num(attrs["humidity"])
        if _num(attrs.get("current_humidity")) is not None:
            out["current_humidity"] = _num(attrs["current_humidity"])
        if attrs.get("mode"):
            out["mode"] = str(attrs["mode"])
        if attrs.get("action"):
            out["action"] = str(attrs["action"])
    return out


def service_for_command(domain: str, action: str, params: Any) -> tuple[str, str, dict[str, Any]] | None:
    """(dominio, servico, dados extras) para um comando do app, ou None se invalido."""
    p = params if isinstance(params, dict) else {}
    kind = kind_for(domain)
    if kind is None:
        return None

    if action in ("turn_on", "turn_off", "toggle") and kind in ("light", "switch", "fan"):
        return domain, action, {}
    if kind == "light":
        if action == "set_brightness" and "brightness" in p:
            return domain, "turn_on", {"brightness": max(0, min(255, int(p["brightness"])))}
        if action == "set_color" and isinstance(p.get("rgb"), list) and len(p["rgb"]) == 3:
            return domain, "turn_on", {"rgb_color": [max(0, min(255, int(c))) for c in p["rgb"]]}
        if action == "set_color_temp" and "kelvin" in p:
            return domain, "turn_on", {"color_temp_kelvin": int(p["kelvin"])}
    elif kind == "cover":
        if action == "open":
            return domain, "open_cover", {}
        if action == "close":
            return domain, "close_cover", {}
        if action == "stop":
            return domain, "stop_cover", {}
        if action == "set_position" and "position" in p:
            return domain, "set_cover_position", {"position": max(0, min(100, int(p["position"])))}
        if action == "set_tilt" and "tilt" in p:
            return domain, "set_cover_tilt_position", {"tilt_position": max(0, min(100, int(p["tilt"])))}
    elif kind == "climate":
        if action == "set_temperature" and "temperature" in p:
            return domain, "set_temperature", {"temperature": float(p["temperature"])}
        if action == "set_mode" and isinstance(p.get("mode"), str):
            return domain, "set_hvac_mode", {"hvac_mode": p["mode"]}
        if action == "set_fan_mode" and isinstance(p.get("fan"), str):
            return domain, "set_fan_mode", {"fan_mode": p["fan"]}
        if action in ("turn_on", "turn_off"):
            return domain, action, {}
    elif kind == "lock":
        if action in ("lock", "unlock"):
            return domain, action, {}
    elif kind == "fan":
        if action == "set_percentage" and "percentage" in p:
            return domain, "set_percentage", {"percentage": max(0, min(100, int(p["percentage"])))}
    elif kind == "scene" and action == "activate":
        return domain, "turn_on", {}
    elif kind == "button" and action == "press":
        return domain, "press", {}
    elif kind == "media_player":
        simple = {"turn_on": "turn_on", "turn_off": "turn_off", "play_pause": "media_play_pause", "next": "media_next_track", "previous": "media_previous_track"}
        if action in simple:
            return domain, simple[action], {}
        if action == "set_volume" and "volume" in p:
            return domain, "volume_set", {"volume_level": max(0, min(100, int(p["volume"]))) / 100}
        if action == "mute" and isinstance(p.get("muted"), bool):
            return domain, "volume_mute", {"is_volume_muted": p["muted"]}
        if action == "select_source" and isinstance(p.get("source"), str):
            return domain, "select_source", {"source": p["source"]}
    elif kind == "vacuum":
        simple = {"start": "start", "pause": "pause", "stop": "stop", "return_home": "return_to_base", "locate": "locate"}
        if action in simple:
            return domain, simple[action], {}
        if action == "set_fan_speed" and isinstance(p.get("fan_speed"), str):
            return domain, "set_fan_speed", {"fan_speed": p["fan_speed"]}
    elif kind == "alarm_control_panel":
        code = {"code": str(p["code"])[:32]} if p.get("code") not in (None, "") else {}
        if action == "arm" and p.get("mode") in ("home", "away", "night", "vacation"):
            return domain, f"alarm_arm_{p['mode']}", code
        if action == "disarm":
            return domain, "alarm_disarm", code
    elif kind == "valve":
        simple = {"open": "open_valve", "close": "close_valve", "stop": "stop_valve"}
        if action in simple:
            return domain, simple[action], {}
        if action == "set_position" and "position" in p:
            return domain, "set_valve_position", {"position": max(0, min(100, int(p["position"])))}
    elif kind == "water_heater":
        if action in ("turn_on", "turn_off"):
            return domain, action, {}
        if action == "set_temperature" and "temperature" in p:
            return domain, "set_temperature", {"temperature": float(p["temperature"])}
        if action == "set_mode" and isinstance(p.get("mode"), str):
            return domain, "set_operation_mode", {"operation_mode": p["mode"]}
    elif kind == "humidifier":
        if action in ("turn_on", "turn_off", "toggle"):
            return domain, action, {}
        if action == "set_humidity" and "humidity" in p:
            return domain, "set_humidity", {"humidity": max(0, min(100, int(p["humidity"])))}
        if action == "set_mode" and isinstance(p.get("mode"), str):
            return domain, "set_mode", {"mode": p["mode"]}
    return None
