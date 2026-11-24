"""Mapa da rede mesh AutoDomo: quem ouve quem, e com que sinal (RSSI).

Le' pelo MQTT do proprio HA, com os comandos AT do gateway AutoDomo:
  AT+MESHNODES?            nos cadastrados (o gateway e' quem cadastra - so' ele responde)
  AT+DEVICES?              id/endereco/online/saltos/tipo/firmware de cada no
  AT+NEIGHBORS?            vizinhos que o proprio gateway ouve direto
  AT+MESHNEIGHBORS=<addr>  vizinhos que o no <addr> ouve direto (ate' 8, os mais fortes)

So' leitura - nenhum rele' muda. Um no por vez, com intervalo, pra nao encher
a rede (casa grande: ~1,5 s por no).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import logging
import re
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er

_LOGGER = logging.getLogger(__name__)

ONLINE_TOPICS = ("+/+/device/+/online", "+/+/+/device/+/online")  # base com e sem "/" na frente
DISCOVER_WAIT_S = 3.0
AT_TIMEOUT_S = 15.0
NODE_GAP_S = 1.5
MAX_NODES = 120

_RE_DEVICE = re.compile(r"ID:(\d+) ADDR:0x([0-9a-fA-F]{1,4}) ONLINE:(\w+)")
_RE_FIELD = re.compile(r"(\w+):(\S+)")
_RE_NODE = re.compile(r"NODE addr=0x([0-9a-fA-F]{1,4})")
_RE_LOCAL_NB = re.compile(r"NB:0x([0-9a-fA-F]{1,4}) RSSI:(-?\d+)")
_RE_REMOTE_NB = re.compile(r"NEIGHBORS:NB1 R\d+ G\d+ ([0-9A-Fa-f]*)")


class MeshMapError(Exception):
    """Sem MQTT / sem gateway - vira a mensagem de erro no app."""


class _AtSession:
    """Conversa AT com um gateway: publica em .../set e junta .../at_reply ate' OK/ERROR/NOACK."""

    def __init__(self, hass: HomeAssistant, base: str) -> None:
        self.hass = hass
        self.base = base
        self._lines: list[str] = []
        self._done = asyncio.Event()
        self._unsub: Callable[[], None] | None = None

    async def __aenter__(self) -> _AtSession:
        from homeassistant.components import mqtt  # noqa: PLC0415 - mqtt e' opcional

        self._unsub = await mqtt.async_subscribe(self.hass, f"{self.base}/at_reply", self._on_reply, qos=1)
        await asyncio.sleep(0.5)  # resposta velha retida chega agora, antes do primeiro comando
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._unsub:
            self._unsub()

    @callback
    def _on_reply(self, msg: Any) -> None:
        payload = msg.payload.decode(errors="replace") if isinstance(msg.payload, bytes) else str(msg.payload)
        self._lines.append(payload)
        if payload in ("OK", "NOACK") or payload.startswith("ERROR"):
            self._done.set()

    async def cmd(self, command: str) -> list[str]:
        from homeassistant.components import mqtt  # noqa: PLC0415

        self._lines = []
        self._done.clear()
        await mqtt.async_publish(self.hass, f"{self.base}/set", command, qos=1)
        try:
            await asyncio.wait_for(self._done.wait(), AT_TIMEOUT_S)
        except TimeoutError:
            return []
        await asyncio.sleep(0.2)  # linhas que chegaram junto do OK
        return list(self._lines)


async def _discover_gateways(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Gateways (e nos com Wi-Fi) que anunciam {"online":true} retido no broker."""
    from homeassistant.components import mqtt  # noqa: PLC0415

    found: dict[str, dict[str, Any]] = {}

    @callback
    def on_online(msg: Any) -> None:
        try:
            data = json.loads(msg.payload)
        except (ValueError, TypeError):
            return
        if not isinstance(data, dict) or data.get("online") is not True:
            return
        base = msg.topic.rsplit("/", 1)[0]  # .../device/<id>
        found[base] = {"base": base, "net": int(data.get("network_id") or 0), "board": str(data.get("board", ""))}

    unsubs = [await mqtt.async_subscribe(hass, t, on_online, qos=1) for t in ONLINE_TOPICS]
    try:
        await asyncio.sleep(DISCOVER_WAIT_S)
    finally:
        for u in unsubs:
            u()
    return list(found.values())


def _parse_devices(lines: list[str]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for line in lines:
        m = _RE_DEVICE.search(line)
        if not m:
            continue
        fields = dict(_RE_FIELD.findall(line))
        addr = int(m.group(2), 16)
        hops = fields.get("HOPS", "")
        out[addr] = {
            "id": int(m.group(1)),
            "online": m.group(3).upper() == "YES",
            "hops": int(hops) if hops.isdigit() else None,
            "type": fields.get("TYPE", "")[:24],
            "fw": fields.get("FW", "")[:16],
            "board": fields.get("BOARD", "")[:24],
            "relays": int(fields["RELAYS"]) if fields.get("RELAYS", "").isdigit() else 0,
            "netcfg": fields.get("NET", ""),
        }
    return out


def _parse_remote_neighbors(lines: list[str]) -> dict[int, int] | None:
    """10 hex por vizinho: endereco(4) rssi(2, complemento de 2) contagem(2) idade/10s(2)."""
    for line in lines:
        m = _RE_REMOTE_NB.search(line)
        if not m:
            continue
        hexs = m.group(1)
        nb: dict[int, int] = {}
        for i in range(0, len(hexs) - 9, 10):
            addr = int(hexs[i : i + 4], 16)
            rssi = int(hexs[i + 4 : i + 6], 16)
            nb[addr] = rssi - 256 if rssi > 127 else rssi
        return nb
    return None


def _ha_device_info(hass: HomeAssistant, net: int, dev_id: int, published: Callable[[str], str | None]) -> dict[str, Any]:
    """Nome/comodo do aparelho no HA (descoberta MQTT: identificador autodomo_<rede>_<id>)."""
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_device(identifiers={("mqtt", f"autodomo_{net}_{dev_id}")})
    if device is None:
        return {}
    info: dict[str, Any] = {"name": (device.name_by_user or device.name or "")[:80]}
    if device.area_id and (area := ar.async_get(hass).async_get_area(device.area_id)):
        info["room"] = area.name[:40]
    devs = {}
    for entry in er.async_entries_for_device(er.async_get(hass), device.id):
        if (did := published(entry.entity_id)) is not None:
            devs[did] = True
    if devs:
        info["devs"] = devs
    return info


async def collect(
    hass: HomeAssistant,
    published: Callable[[str], str | None],
    progress: Callable[[int, int], Awaitable[None]],
) -> dict[str, Any]:
    """Varre todas as redes mesh que o HA enxerga. published(entity_id) -> deviceId no app (ou None)."""
    if "mqtt" not in hass.config.components:
        raise MeshMapError("no_mqtt")
    from homeassistant.components import mqtt  # noqa: PLC0415

    if not await mqtt.async_wait_for_mqtt_client(hass):
        raise MeshMapError("no_mqtt")

    nodes: dict[str, dict[str, Any]] = {}
    nets_done: set[int] = set()
    for gw in await _discover_gateways(hass):
        if gw["net"] in nets_done:
            continue  # gateway secundario da mesma rede: a lista e' a mesma
        async with _AtSession(hass, gw["base"]) as at:
            registered = {int(m.group(1), 16) for line in await at.cmd("AT+MESHNODES?") if (m := _RE_NODE.search(line))}
            if not registered:
                continue  # no' com Wi-Fi, nao e' quem cadastra a rede
            nets_done.add(gw["net"])
            table = _parse_devices(await at.cmd("AT+DEVICES?"))
            local_nb = {
                int(m.group(1), 16): int(m.group(2))
                for line in await at.cmd("AT+NEIGHBORS?")
                if (m := _RE_LOCAL_NB.search(line))
            }
            # O gateway aparece na propria tabela mas nao na lista de cadastrados.
            extra = [a for a in table if a not in registered]
            gw_addr = next((a for a in extra if "PRIM" in table[a]["netcfg"]), extra[0] if extra else None)
            if gw_addr is None:
                gw_addr = 0  # tabela sem o gateway (raro): endereco 0 so' como ancora
                table[0] = {"id": 0, "online": True, "hops": 0, "type": "", "fw": "", "board": gw["board"], "relays": 0, "netcfg": ""}
            for addr in registered:
                table.setdefault(addr, {"id": 0, "online": False, "hops": None, "type": "", "fw": "", "board": "", "relays": 0, "netcfg": ""})

            addrs = sorted(table)[:MAX_NODES]
            todo = [a for a in addrs if a != gw_addr and table[a]["online"]]
            total, done = len(todo), 0
            await progress(done, total)
            neighbors: dict[int, dict[int, int] | None] = {gw_addr: local_nb}
            old_fw: set[int] = set()
            for addr in todo:
                reply = await at.cmd(f"AT+MESHNEIGHBORS=0x{addr:04X}")
                neighbors[addr] = _parse_remote_neighbors(reply)
                if neighbors[addr] is None and any("firmware antigo" in line for line in reply):
                    old_fw.add(addr)  # firmware do no' nao tem a tabela de vizinhos
                done += 1
                await progress(done, total)
                await asyncio.sleep(NODE_GAP_S)

        net = gw["net"]
        for addr in addrs:
            row = table[addr]
            key = f"{net}_{row['id'] or f'a{addr:04x}'}"
            node: dict[str, Any] = {
                "addr": f"{addr:04x}",
                "net": net,
                "online": row["online"],
                "type": row["type"],
                "fw": row["fw"],
                "board": row["board"],
            }
            if row["hops"] is not None:
                node["hops"] = row["hops"]
            if addr == gw_addr:
                node["gw"] = True
                node["board"] = node["board"] or gw["board"][:24]
            nb = neighbors.get(addr)
            if nb:
                node["nb"] = {f"{a:04x}": r for a, r in nb.items()}
            elif addr in neighbors:
                node["noReply"] = True
                if addr in old_fw:
                    node["oldFw"] = True
            if row["id"]:
                node.update(_ha_device_info(hass, net, row["id"], published))
            nodes[key] = node

    if not nodes:
        raise MeshMapError("no_gateway")
    return nodes
