"""Alexa lendo a conversa da casa.

Descobre os Echo do Home Assistant e publica em homes/{casa}/bridges/{ponte}/alexa
(o app mostra o botao de alto-falante na conversa e a escolha de Echo/modo nos
ajustes da casa). Quando uma mensagem e' marcada pra ser lida, a nuvem poe na
fila de comandos {action: "speak", params: {text, mode, targets}} e esta ponte
fala nos Echo escolhidos.

Integracoes suportadas:
- Alexa Devices (oficial do HA): uma entidade notify "speak" e outra "announce"
  por Echo -> notify.send_message.
- Alexa Media Player (HACS): media_player por Echo -> notify.alexa_media com
  data.type tts/announce.
So' fala em Echo que esta ponte mesma descobriu (o comando nao escolhe
entidade qualquer do HA).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

_LOGGER = logging.getLogger(__name__)

MAX_TEXT = 300
_BAD_KEY = re.compile(r"[.$#\[\]/]")


def _key(raw: str) -> str:
    """Chave valida no RTDB (sem . $ # [ ] /)."""
    return _BAD_KEY.sub("_", raw)[:120]


def _device_name(dev_reg: dr.DeviceRegistry, device_id: str | None, fallback: str) -> str:
    dev = dev_reg.async_get(device_id) if device_id else None
    if dev is None:
        return fallback
    return (dev.name_by_user or dev.name or fallback)[:60]


def discover_echos(hass: HomeAssistant) -> dict[str, dict[str, str]]:
    """{chave: {name, kind, speak?, announce?}} dos Echo disponiveis."""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    echos: dict[str, dict[str, str]] = {}

    for entry in ent_reg.entities.values():
        if entry.disabled_by is not None or entry.domain != "notify" or entry.platform != "alexa_devices":
            continue
        key = _key(entry.device_id or entry.entity_id)
        item = echos.setdefault(key, {"name": _device_name(dev_reg, entry.device_id, entry.entity_id), "kind": "alexa_devices"})
        role = (entry.translation_key or entry.unique_id or entry.entity_id).lower()
        if "announce" in role:
            item["announce"] = entry.entity_id
        elif "speak" in role:
            item["speak"] = entry.entity_id

    if hass.services.has_service("notify", "alexa_media"):
        for entry in ent_reg.entities.values():
            if entry.disabled_by is not None or entry.domain != "media_player" or entry.platform != "alexa_media":
                continue
            echos[_key(entry.entity_id)] = {
                "name": _device_name(dev_reg, entry.device_id, entry.original_name or entry.entity_id),
                "kind": "alexa_media",
                "speak": entry.entity_id,
                "announce": entry.entity_id,
            }

    return {k: v for k, v in echos.items() if v.get("speak") or v.get("announce")}


async def speak(hass: HomeAssistant, params: Any) -> None:
    """Executa um comando "speak" da fila (so' em Echo descobertos aqui)."""
    p = params if isinstance(params, dict) else {}
    text = str(p.get("text") or "").strip()[:MAX_TEXT]
    if not text:
        return
    announce = p.get("mode") == "announce"
    known = discover_echos(hass)
    wanted = p.get("targets")
    keys = [k for k in wanted if k in known] if isinstance(wanted, list) and wanted else list(known)
    for k in keys:
        echo = known[k]
        try:
            if echo["kind"] == "alexa_devices":
                entity = echo.get("announce" if announce else "speak") or echo.get("speak") or echo.get("announce")
                await hass.services.async_call("notify", "send_message", {"entity_id": entity, "message": text}, blocking=False)
            else:
                await hass.services.async_call(
                    "notify",
                    "alexa_media",
                    {"message": text, "target": [echo["speak"]], "data": {"type": "announce" if announce else "tts"}},
                    blocking=False,
                )
        except Exception:  # noqa: BLE001 - um Echo com problema nao impede os outros
            _LOGGER.exception("nao deu pra falar no Echo %s", echo.get("name"))
