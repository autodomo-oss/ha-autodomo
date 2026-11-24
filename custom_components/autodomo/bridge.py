"""A ponte: sincroniza entidades escolhidas com o AutoDomo e executa comandos."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from functools import partial
import logging
import random
import re
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_STATE_CHANGED, __version__ as HA_VERSION
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.event import EventStateChangedData, async_call_later, async_track_state_change_event

from .const import (
    AUTO_DOMAINS,
    COMMAND_MAX_AGE_S,
    CONF_REMOTE_COMMANDS,
    DOMAIN,
    HEARTBEAT_INTERVAL_S,
    ISSUE_MISSING_ENTITY,
    MESHMAP_MIN_INTERVAL_S,
    MISSING_GRACE_S,
    REMOTE_KEYSETS,
    REMOTE_MAX_COMMANDS,
    RTC_OFFER_MAX_AGE_S,
    RTC_SESSION_MAX_S,
    STATE_DEBOUNCE_S,
    STREAM_RETRY_MAX_S,
    STREAM_RETRY_MIN_S,
)
from . import alexa, meshmap
from .firebase import FirebaseAuthError, FirebaseClient, FirebaseError, StreamTokenExpired
from .mapping import device_id_for, device_payload, entity_id_for, service_for_command, state_payload

_LOGGER = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _supports_webrtc(camera: Any) -> bool:
    """A camera entrega WebRTC (go2rtc do HA ou nativo)?"""
    try:
        types = camera.camera_capabilities.frontend_stream_types
    except Exception:  # noqa: BLE001 - HA antigo / camera sem stream
        return False
    return any(getattr(t, "value", t) == "web_rtc" for t in types)


class AutodomoBridge:
    """Uma instancia por config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: FirebaseClient,
        *,
        home_id: str,
        bridge_id: str,
        entity_ids: set[str],
        auto_add: bool,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.client = client
        self.home_id = home_id
        self.bridge_id = bridge_id
        self.selected = set(entity_ids)  # escolha explicita (opcoes)
        self.auto_add = auto_add
        self.entity_ids = set(entity_ids)  # efetivo: selecionadas + auto (se ligado)
        self.auth_failed = False

        self._published: set[str] = set()  # entidades cujo devices/{id} ja' foi escrito
        # Escolhida que sumiu do HA: desde quando (monotonic) e quais ja' foram
        # tiradas do app. Continua escolhida - se voltar, o reconcile republica.
        self._missing_since: dict[str, float] = {}
        self._gone: set[str] = set()
        # Controle remoto (ex: modulo IR): botoes do mesmo aparelho do HA de um
        # botao escolhido entram juntos - o app monta a tela do controle com eles.
        self._remote_devices: set[str] = set()
        self._unsub: list[CALLBACK_TYPE] = []
        self._debounce: dict[str, CALLBACK_TYPE] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._stopped = asyncio.Event()
        self._alexa_published: dict[str, Any] | None = None  # ultima lista de Echo publicada
        # Camera: sessoes WebRTC abertas (sid -> camera, contador de candidatos,
        # trava pra resposta sair antes dos candidatos, timer do limite).
        self._rtc: dict[str, dict[str, Any]] = {}
        # remote.* do Broadlink: comandos aprendidos ([{d: aparelho, c: comando}]),
        # lidos do arquivo de codigos dele - tambem e' a lista do que o app pode enviar.
        self._remote_cmds: dict[str, list[dict[str, str]]] = {}
        # Mapa da rede mesh (tela escondida do app): uma varredura por vez.
        self._meshmap_task: asyncio.Task[None] | None = None
        self._meshmap_last = 0.0

    # ----- ciclo de vida -----

    async def async_start(self) -> None:
        if self.auto_add:
            self.entity_ids |= {s.entity_id for s in self.hass.states.async_all() if s.domain in AUTO_DOMAINS}
        self.entity_ids |= self._remote_siblings()
        self._unsub.append(self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._on_registry_updated))
        # Assina ANTES de publicar a foto: as publicacoes esperam a rede, e o que
        # mudar nesse meio tempo se perdia (no boot do HA as entidades MQTT vao de
        # unavailable pra unknown justo ai' - botao ficava offline no app pra sempre).
        if self.auto_add:
            self._unsub.append(self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._on_any_state_changed))
        else:
            self._unsub.append(
                async_track_state_change_event(self.hass, list(self.entity_ids), self._on_state_changed)
            )
        self._load_remote_commands()
        await self._publish_devices()
        await self._publish_all_states()
        self._check_missing_entities()
        self._tasks.append(
            self.hass.async_create_background_task(
                self._stream_loop(self._commands_path(), self._on_command_event), "autodomo_commands"
            )
        )
        self._tasks.append(
            self.hass.async_create_background_task(self._stream_loop(self._rtc_path(), self._on_rtc_event, critical=False), "autodomo_rtc")
        )
        self._tasks.append(self.hass.async_create_background_task(self._heartbeat_loop(), "autodomo_heartbeat"))
        _LOGGER.info(
            "ponte %s da casa %s ativa com %d entidades (auto: %s)",
            self.bridge_id,
            self.home_id,
            len(self.entity_ids),
            self.auto_add,
        )

    async def async_stop(self) -> None:
        self._stopped.set()
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        for cancel in self._debounce.values():
            cancel()
        self._debounce.clear()
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        for sid in list(self._rtc):
            self._rtc_close(sid)
        if not self.auth_failed:
            try:
                await self.client.patch(self._bridge_path(), {"online": False, "lastSeen": _now_ms()})
            except FirebaseError as err:
                _LOGGER.debug("nao deu pra marcar offline: %s", err)

    # ----- caminhos -----

    def _bridge_path(self) -> str:
        return f"homes/{self.home_id}/bridges/{self.bridge_id}"

    def _devices_path(self) -> str:
        return f"homes/{self.home_id}/devices"

    def _state_path(self, device_id: str) -> str:
        return f"homes/{self.home_id}/state/{device_id}"

    def _commands_path(self) -> str:
        return f"commands/{self.home_id}/{self.bridge_id}"

    def _rtc_path(self) -> str:
        return f"rtc/{self.home_id}/{self.bridge_id}"

    def _meshmap_path(self) -> str:
        return f"homes/{self.home_id}/meshMap/{self.bridge_id}"

    # ----- devices -----

    def _room_for(self, entity_id: str) -> str | None:
        ent_reg = er.async_get(self.hass)
        entry = ent_reg.async_get(entity_id)
        if entry is None:
            return None
        area_id = entry.area_id
        if area_id is None and entry.device_id:
            device = dr.async_get(self.hass).async_get(entry.device_id)
            area_id = device.area_id if device else None
        if area_id is None:
            return None
        area = ar.async_get(self.hass).async_get_area(area_id)
        return area.name if area else None

    # ----- controle remoto: botoes agrupados por aparelho -----

    def _remote_siblings(self) -> set[str]:
        """Todos os botoes dos aparelhos que tem algum botao escolhido."""
        ent_reg = er.async_get(self.hass)
        self._remote_devices = {
            e.device_id
            for eid in self.selected
            if eid.startswith("button.") and (e := ent_reg.async_get(eid)) is not None and e.device_id
        }
        return {
            e.entity_id
            for dev in self._remote_devices
            for e in er.async_entries_for_device(ent_reg, dev)
            if e.domain == "button" and e.disabled_by is None
        }

    def _remote_caps(self, entity_id: str, state: Any) -> dict[str, Any] | None:
        """caps.remote do botao: aparelho do HA + nome original (o app reconhece
        a tecla pelo nome com que o botao foi gravado, nao pelo renomeado)."""
        if not entity_id.startswith("button."):
            return None
        entry = er.async_get(self.hass).async_get(entity_id)
        if entry is None or not entry.device_id:
            return None
        return {"id": entry.device_id, "key": entry.original_name or state.name}

    def _with_extras(self, entity_id: str, state: Any, payload: dict[str, Any]) -> dict[str, Any]:
        """caps que dependem de registro/objeto do HA, nao so' do estado."""
        remote = self._remote_caps(entity_id, state)
        if remote is not None:
            payload["caps"] = {**payload.get("caps", {}), "remote": remote}
        if entity_id.startswith("camera.") and (camera := self._camera(entity_id)) is not None:
            payload["caps"] = {**payload.get("caps", {}), "webrtc": _supports_webrtc(camera)}
        if entity_id.startswith("remote."):
            payload["caps"] = {**payload.get("caps", {}), **self._remote_extras(entity_id)}
        return payload

    # ----- remote.*: teclas fixas por integracao + comandos digitados nas opcoes -----

    def _remote_platform(self, entity_id: str) -> str | None:
        entry = er.async_get(self.hass).async_get(entity_id)
        return entry.platform if entry else None

    def _remote_extras(self, entity_id: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if keys := REMOTE_KEYSETS.get(self._remote_platform(entity_id) or ""):
            out["keys"] = dict(keys)
        if cmds := self._remote_cmds.get(entity_id):
            out["commands"] = cmds
        return out

    def _load_remote_commands(self) -> set[str]:
        """Comandos dos remote.* digitados nas opcoes (um por linha, "aparelho/comando";
        "remote.x: aparelho/comando" so' pra esse). Devolve os remotes cuja lista mudou."""
        per: dict[str, list[dict[str, str]]] = {}
        remotes = [e for e in self.entity_ids if e.startswith("remote.")]
        for raw in str(self.entry.options.get(CONF_REMOTE_COMMANDS, "")).splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            target = None
            m = re.match(r"^(remote\.[a-z0-9_]+)\s*:\s*(.+)$", line)
            if m:
                target, line = m.group(1), m.group(2).strip()
            dev, sep, cmd = line.partition("/")
            dev, cmd = (dev.strip(), cmd.strip()) if sep else ("", line)
            if not cmd or len(cmd) > 100 or len(dev) > 100:
                continue
            for entity_id in [target] if target else remotes:
                if entity_id in remotes and len(per.setdefault(entity_id, [])) < REMOTE_MAX_COMMANDS:
                    item = {"d": dev, "c": cmd}
                    if item not in per[entity_id]:
                        per[entity_id].append(item)
        changed = {e for e in set(per) | set(self._remote_cmds) if per.get(e, []) != self._remote_cmds.get(e, [])}
        self._remote_cmds = per
        return changed

    def _remote_allowed(self, entity_id: str, data: dict[str, Any]) -> bool:
        """So' comandos/atividades que o app recebeu na lista - nada de codigo cru."""
        if "command" in data:
            command, device = data["command"], data.get("device", "")
            keys = REMOTE_KEYSETS.get(self._remote_platform(entity_id) or "", {})
            if not device and command in keys.values():
                return True
            return any(c["c"] == command and c["d"] == device for c in self._remote_cmds.get(entity_id, []))
        if "activity" in data:
            state = self.hass.states.get(entity_id)
            return state is not None and data["activity"] in (state.attributes.get("activity_list") or [])
        return True

    # ----- camera: video direto HA -> celular (WebRTC) -----

    def _camera(self, entity_id: str) -> Any | None:
        """Objeto da camera no HA (None se nao existe ou esta' desligada)."""
        from homeassistant.components.camera.helper import get_camera_from_entity_id  # noqa: PLC0415
        from homeassistant.exceptions import HomeAssistantError  # noqa: PLC0415

        try:
            return get_camera_from_entity_id(self.hass, entity_id)
        except HomeAssistantError:
            return None

    async def _snapshot(self, entity_id: str, params: Any) -> None:
        """Foto da camera pro aviso de sensor: JPEG (640 px) em snapshots/{casa}/{id}/img.

        A nuvem pede (notifySensor) e reenvia o aviso com a imagem; a foto some em 24 h.
        """
        alert_id = str((params or {}).get("id", "")) if isinstance(params, dict) else ""
        if not re.fullmatch(r"[\w-]{1,64}", alert_id) or not entity_id.startswith("camera."):
            return
        from homeassistant.components.camera import async_get_image  # noqa: PLC0415
        from homeassistant.exceptions import HomeAssistantError  # noqa: PLC0415

        try:
            image = await async_get_image(self.hass, entity_id, timeout=10, width=640)
        except HomeAssistantError as err:
            _LOGGER.info("camera %s sem foto pro aviso: %s", entity_id, err)
            return
        data = base64.b64encode(image.content).decode()
        if len(data) >= 1_500_000:
            _LOGGER.info("foto de %s grande demais (%d KB) - ignorada", entity_id, len(image.content) // 1024)
            return
        await self.client.put(f"snapshots/{self.home_id}/{alert_id}/img", data)

    async def _on_rtc_event(self, event: str, path: str, data: Any) -> None:
        if path in ("", "/"):
            # Carga inicial (ou no' inteiro apagado): sessoes pendentes.
            if isinstance(data, dict):
                for sid, sess in list(data.items()):
                    await self._rtc_open(sid, sess)
            return
        parts = path.strip("/").split("/")
        if len(parts) != 1:
            return  # answer/c/error: escritas nossas voltando pelo stream
        if data is None:
            self._rtc_close(parts[0])  # app saiu da tela (ou nos mesmos apagamos)
        elif isinstance(data, dict):
            await self._rtc_open(parts[0], data)

    async def _rtc_open(self, sid: str, sess: Any) -> None:
        if sid in self._rtc or not isinstance(sess, dict) or "answer" in sess or "error" in sess:
            return
        node = f"{self._rtc_path()}/{sid}"
        ts = int(sess.get("ts") or 0)
        if _now_ms() - ts > RTC_OFFER_MAX_AGE_S * 1000:
            await self._rtc_delete(node)  # ninguem mais esperando
            return
        entity_id = entity_id_for(str(sess.get("deviceId", "")))
        offer = sess.get("offer")
        camera = self._camera(entity_id) if entity_id and entity_id.startswith("camera.") else None
        if camera is None or entity_id not in self.entity_ids or not isinstance(offer, str):
            await self._rtc_error(node, "camera_unavailable")
            return
        if not _supports_webrtc(camera):
            await self._rtc_error(node, "webrtc_not_supported")
            return

        info: dict[str, Any] = {"camera": camera, "n": 0, "lock": asyncio.Lock()}
        info["timer"] = async_call_later(self.hass, RTC_SESSION_MAX_S, partial(self._rtc_expire, sid))
        self._rtc[sid] = info

        @callback
        def send_message(message: Any) -> None:
            # Resposta e candidatos chegam pelo callback do go2rtc; a trava
            # mantem a ordem (resposta antes dos candidatos).
            self.hass.async_create_task(self._rtc_send(sid, node, message.as_dict()))

        _LOGGER.debug("camera %s: sessao %s aberta", entity_id, sid)
        try:
            await camera.async_handle_async_webrtc_offer(offer, sid, send_message)
        except Exception as err:  # noqa: BLE001 - erro da camera nao derruba a ponte
            _LOGGER.warning("camera %s: oferta recusada: %s", entity_id, err)
            self._rtc_close(sid)
            await self._rtc_error(node, "webrtc_offer_failed")

    async def _rtc_send(self, sid: str, node: str, msg: dict[str, Any]) -> None:
        info = self._rtc.get(sid)
        if info is None:
            return
        async with info["lock"]:
            try:
                if msg.get("type") == "answer":
                    await self.client.put(f"{node}/answer", msg["answer"])
                elif msg.get("type") == "candidate":
                    cand = msg.get("candidate") or {}
                    await self.client.put(f"{node}/c/{info['n']}", {k: v for k, v in cand.items() if v is not None})
                    info["n"] += 1
                elif msg.get("type") == "error":
                    _LOGGER.warning("camera: erro na sessao %s: %s", sid, msg.get("message"))
                    await self.client.put(f"{node}/error", str(msg.get("code") or "error")[:200])
            except FirebaseError as err:
                _LOGGER.debug("camera: nao deu pra responder a sessao %s: %s", sid, err)

    async def _rtc_error(self, node: str, code: str) -> None:
        try:
            await self.client.put(f"{node}/error", code)
        except FirebaseError as err:
            _LOGGER.debug("camera: nao deu pra avisar erro em %s: %s", node, err)

    async def _rtc_delete(self, node: str) -> None:
        try:
            await self.client.delete(node)
        except FirebaseError as err:
            _LOGGER.debug("camera: nao deu pra apagar %s: %s", node, err)

    async def _rtc_expire(self, sid: str, _now: Any) -> None:
        self._rtc_close(sid)
        await self._rtc_delete(f"{self._rtc_path()}/{sid}")

    @callback
    def _rtc_close(self, sid: str) -> None:
        info = self._rtc.pop(sid, None)
        if info is None:
            return
        info["timer"]()
        try:
            info["camera"].close_webrtc_session(sid)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("camera: erro fechando a sessao %s", sid, exc_info=True)
        _LOGGER.debug("camera: sessao %s fechada", sid)

    @callback
    def _on_registry_updated(self, event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        """Entidade apagada do HA sai do app; botao novo do controle remoto entra."""
        entity_id = event.data["entity_id"]
        action = event.data["action"]
        if action == "remove" and entity_id in self.selected:
            # Escolhida nas opcoes: sai do app ja', mas continua escolhida (se o
            # aparelho voltar - ex: re-pareado - o reconcile publica de novo).
            self._published.discard(entity_id)
            self._gone.add(entity_id)
            self.hass.async_create_task(self._unpublish(entity_id))
            return
        if not entity_id.startswith("button.") and not (self.auto_add and action == "remove"):
            return
        if entity_id in self.selected:
            return
        if action == "create":
            if not entity_id.startswith("button."):
                return
            entry = er.async_get(self.hass).async_get(entity_id)
            if entry is None or entry.device_id not in self._remote_devices or entity_id in self.entity_ids:
                return
            self.entity_ids.add(entity_id)
            if not self.auto_add:
                self._unsub.append(async_track_state_change_event(self.hass, [entity_id], self._on_state_changed))
            if self.hass.states.get(entity_id) is not None:
                self.hass.async_create_task(self._push_state(entity_id))
        elif action == "remove" and entity_id in self.entity_ids:
            self.entity_ids.discard(entity_id)
            self._published.discard(entity_id)
            self.hass.async_create_task(self._unpublish(entity_id))

    async def _unpublish(self, entity_id: str) -> None:
        did = device_id_for(entity_id)
        try:
            # state antes do device: a regra de state exige que o device exista
            await self.client.delete(self._state_path(did))
            await self.client.delete(f"{self._devices_path()}/{did}")
        except FirebaseError as err:
            _LOGGER.debug("nao deu pra tirar %s do app: %s", entity_id, err)

    async def _publish_device(self, entity_id: str, state: Any) -> bool:
        payload = device_payload(state, self._room_for(entity_id))
        if payload is None:
            _LOGGER.warning("entidade %s de dominio nao suportado - ignorada", entity_id)
            return False
        payload = self._with_extras(entity_id, state, payload)
        payload["bridge"] = self.bridge_id
        await self.client.put(f"{self._devices_path()}/{device_id_for(entity_id)}", payload)
        self._published.add(entity_id)
        self._gone.discard(entity_id)  # voltou: se sumir de novo, sai do app de novo
        self._missing_since.pop(entity_id, None)
        _LOGGER.debug("dispositivo %s publicado", entity_id)
        return True

    async def _publish_devices(self) -> None:
        """Escreve devices/* desta ponte e apaga os que sairam da selecao."""
        updates: dict[str, Any] = {}
        wanted: set[str] = set()
        for entity_id in sorted(self.entity_ids):
            did = device_id_for(entity_id)
            wanted.add(did)  # mesmo sem estado ainda: nao e' pra apagar
            state = self.hass.states.get(entity_id)
            if state is None:
                # Entidades de MQTT/discovery podem aparecer depois do boot -
                # publica quando o primeiro estado chegar.
                _LOGGER.debug("entidade %s ainda nao existe - publica quando aparecer", entity_id)
                continue
            payload = device_payload(state, self._room_for(entity_id))
            if payload is None:
                _LOGGER.warning("entidade %s de dominio nao suportado - ignorada", entity_id)
                continue
            payload = self._with_extras(entity_id, state, payload)
            payload["bridge"] = self.bridge_id
            updates[did] = payload
            self._published.add(entity_id)

        # Devices desta ponte que ja' estao no servidor e nao foram mais escolhidos.
        try:
            existing = await self.client.get(
                self._devices_path(), query={"orderBy": '"bridge"', "equalTo": f'"{self.bridge_id}"'}
            )
        except FirebaseError as err:
            _LOGGER.debug("nao deu pra listar devices existentes: %s", err)
            existing = None
        stale = [did for did in (existing or {}) if did not in wanted] if isinstance(existing, dict) else []

        if updates:
            await self.client.patch(self._devices_path(), updates)
        for did in stale:
            # state antes do device: a regra de state exige que o device exista
            await self.client.delete(self._state_path(did))
            await self.client.delete(f"{self._devices_path()}/{did}")
        if stale:
            _LOGGER.info("%d dispositivo(s) removido(s) do app", len(stale))

    async def _publish_all_states(self) -> None:
        """Estado inicial de tudo que ja' existe. Entidade que apareceu entre o
        _publish_devices e aqui ganha o device agora - state/{id} sem
        devices/{id} e' negado pelas regras e derruba o PATCH todo."""
        updates: dict[str, Any] = {}
        ts = _now_ms()
        for entity_id in self.entity_ids:
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            if entity_id not in self._published and not await self._publish_device(entity_id, state):
                continue
            updates[device_id_for(entity_id)] = state_payload(state, ts)
        if updates:
            await self.client.patch(f"homes/{self.home_id}/state", updates)

    async def _reconcile_unpublished(self) -> None:
        """Entidade escolhida com estado mas sem device (janela entre a foto
        inicial e o listener, ou que estava indisponivel) - publica agora."""
        for entity_id in self.entity_ids - self._published:
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            if await self._publish_device(entity_id, state):
                await self.client.put(self._state_path(device_id_for(entity_id)), state_payload(state, _now_ms()))

    # ----- reparos: entidade escolhida que nao existe mais -----

    @callback
    def _check_missing_entities(self) -> None:
        ent_reg = er.async_get(self.hass)
        now = time.monotonic()
        for entity_id in self.selected:
            issue_id = f"{ISSUE_MISSING_ENTITY}_{self.entry.entry_id}_{entity_id}"
            if self.hass.states.get(entity_id) is None and ent_reg.async_get(entity_id) is None:
                # Tira do app depois de MISSING_GRACE_S com o HA ja' rodando: no
                # boot entidade de YAML/MQTT ainda pode nao ter carregado.
                if self.hass.is_running and entity_id not in self._gone:
                    since = self._missing_since.setdefault(entity_id, now)
                    if now - since >= MISSING_GRACE_S:
                        self._gone.add(entity_id)
                        self._published.discard(entity_id)
                        _LOGGER.info("entidade %s nao existe mais no HA - tirada do app", entity_id)
                        self.hass.async_create_task(self._unpublish(entity_id))
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    issue_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key=ISSUE_MISSING_ENTITY,
                    translation_placeholders={"entity_id": entity_id},
                )
            else:
                self._missing_since.pop(entity_id, None)
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    # ----- state -----

    @callback
    def _on_any_state_changed(self, event: Event[EventStateChangedData]) -> None:
        """Modo auto: tudo dos AUTO_DOMAINS entra sozinho; o resto so' se escolhido."""
        entity_id = event.data["entity_id"]
        if entity_id not in self.entity_ids:
            if event.data.get("old_state") is None and entity_id.split(".", 1)[0] in AUTO_DOMAINS:
                self.entity_ids.add(entity_id)
                _LOGGER.info("entidade nova %s exposta automaticamente", entity_id)
            else:
                return
        self._on_state_changed(event)

    @callback
    def _on_state_changed(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        if event.data.get("new_state") is None:
            return
        # Debounce por entidade: sensores que mudam varias vezes por segundo
        # viram uma escrita a cada 300 ms.
        if cancel := self._debounce.pop(entity_id, None):
            cancel()
        # Corrotina (via partial): o HA agenda no loop. Funcao comum iria pro executor.
        self._debounce[entity_id] = async_call_later(
            self.hass, STATE_DEBOUNCE_S, partial(self._push_state_later, entity_id)
        )

    async def _push_state_later(self, entity_id: str, _now: Any) -> None:
        await self._push_state(entity_id)

    async def _push_state(self, entity_id: str) -> None:
        self._debounce.pop(entity_id, None)
        state = self.hass.states.get(entity_id)
        if state is None:
            return
        try:
            if entity_id not in self._published and not await self._publish_device(entity_id, state):
                return
            await self.client.put(self._state_path(device_id_for(entity_id)), state_payload(state, _now_ms()))
        except FirebaseAuthError as err:
            self._on_auth_failed(err)
        except FirebaseError as err:
            _LOGGER.warning("falha ao publicar estado de %s: %s", entity_id, err)

    # ----- comandos (e sessoes de camera: mesmo esquema de stream) -----

    async def _stream_loop(
        self, path: str, on_event: Callable[[str, str, Any], Awaitable[None]], *, critical: bool = True
    ) -> None:
        """critical=False (camera): acesso negado so' espera e tenta de novo - nunca
        pede reautenticacao (regra ainda nao publicada nao e' ponte revogada)."""
        delay = STREAM_RETRY_MIN_S
        while not self._stopped.is_set():
            try:
                await self.client.stream(path, on_event)
                delay = STREAM_RETRY_MIN_S
            except StreamTokenExpired:
                # Normal a cada ~1 h: renova o ID token e reabre ja'. So' e'
                # revogacao se o proprio refresh for recusado.
                try:
                    await self.client.refresh()
                except FirebaseAuthError as err:
                    self._on_auth_failed(err)
                    return
                except FirebaseError as err:
                    _LOGGER.debug("refresh falhou (%s), tentando de novo", err)
                    await asyncio.sleep(STREAM_RETRY_MIN_S)
                _LOGGER.debug("token do stream renovado, reconectando")
                delay = STREAM_RETRY_MIN_S
                continue
            except FirebaseAuthError as err:
                if not critical:
                    _LOGGER.debug("stream %s negado (%s), tentando de novo em %ss", path, err, STREAM_RETRY_MAX_S)
                    await asyncio.sleep(STREAM_RETRY_MAX_S)
                    continue
                # 401 ao abrir: pode ser so' token velho - confirma com um refresh.
                try:
                    await self.client.refresh()
                    continue
                except FirebaseAuthError:
                    self._on_auth_failed(err)
                    return
                except FirebaseError:
                    pass
            except FirebaseError as err:
                _LOGGER.debug("stream %s caiu (%s), reconectando em %ss", path, err, delay)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _LOGGER.exception("erro inesperado no stream %s", path)
            await asyncio.sleep(delay + random.uniform(0, 1))
            delay = min(delay * 2, STREAM_RETRY_MAX_S)

    async def _on_command_event(self, event: str, path: str, data: Any) -> None:
        if data is None:
            return  # remocao (nossa propria, depois de executar)
        if path in ("", "/"):
            # Carga inicial: tudo que estava na fila.
            if isinstance(data, dict):
                for cmd_id, cmd in list(data.items()):
                    await self._handle_command(cmd_id, cmd)
            return
        parts = path.strip("/").split("/")
        if len(parts) == 1 and isinstance(data, dict):
            await self._handle_command(parts[0], data)
        # patch em campo de um comando existente: ignorado (comando e' imutavel)

    async def _publish_alexa(self) -> None:
        """Echo disponiveis (Alexa lendo a conversa) - so' escreve quando muda."""
        echos = alexa.discover_echos(self.hass)
        if echos == self._alexa_published:
            return
        await self.client.put(f"{self._bridge_path()}/alexa", {"echos": echos} if echos else None)
        self._alexa_published = echos
        _LOGGER.debug("Echo publicados: %s", ", ".join(e["name"] for e in echos.values()) or "nenhum")

    async def _handle_command(self, cmd_id: str, cmd: Any) -> None:
        try:
            if not isinstance(cmd, dict):
                return
            ts = int(cmd.get("ts") or 0)
            if ts and _now_ms() - ts > COMMAND_MAX_AGE_S * 1000:
                _LOGGER.debug("comando %s velho (%ds) - descartado", cmd_id, (_now_ms() - ts) // 1000)
                return
            if cmd.get("action") == "speak":
                await alexa.speak(self.hass, cmd.get("params"))
                return
            if cmd.get("action") == "mesh_map":
                self._start_meshmap()
                return
            entity_id = entity_id_for(str(cmd.get("deviceId", "")))
            if entity_id is None or entity_id not in self.entity_ids:
                _LOGGER.warning("comando %s para entidade nao exposta: %s", cmd_id, cmd.get("deviceId"))
                return
            if cmd.get("action") == "snapshot":
                await self._snapshot(entity_id, cmd.get("params"))
                return
            mapped = service_for_command(entity_id.split(".", 1)[0], str(cmd.get("action", "")), cmd.get("params"))
            if mapped is None:
                _LOGGER.warning("comando %s invalido para %s: %s", cmd_id, entity_id, cmd.get("action"))
                return
            domain, service, data = mapped
            if domain == "remote" and not self._remote_allowed(entity_id, data):
                _LOGGER.warning("comando %s fora da lista de %s - ignorado", cmd_id, entity_id)
                return
            _LOGGER.debug("comando %s: %s.%s %s -> %s", cmd_id, domain, service, data, entity_id)
            await self.hass.services.async_call(domain, service, {"entity_id": entity_id, **data}, blocking=True)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("falha ao executar comando %s", cmd_id)
        finally:
            try:
                await self.client.delete(f"{self._commands_path()}/{cmd_id}")
            except FirebaseError as err:
                _LOGGER.debug("nao deu pra apagar comando %s: %s", cmd_id, err)

    # ----- mapa da rede mesh -----

    def _start_meshmap(self) -> None:
        if self._meshmap_task is not None and not self._meshmap_task.done():
            _LOGGER.debug("mapa da rede ja' em andamento")
            return
        if time.monotonic() - self._meshmap_last < MESHMAP_MIN_INTERVAL_S:
            _LOGGER.debug("mapa da rede pedido de novo cedo demais - ignorado")
            return
        self._meshmap_last = time.monotonic()
        self._meshmap_task = self.hass.async_create_background_task(self._run_meshmap(), "autodomo_meshmap")
        self._tasks.append(self._meshmap_task)

    def _published_device_id(self, entity_id: str) -> str | None:
        return device_id_for(entity_id) if entity_id in self._published else None

    async def _run_meshmap(self) -> None:
        path = self._meshmap_path()

        async def progress(done: int, total: int) -> None:
            try:  # progresso e' so' enfeite na tela - falha aqui nao derruba a varredura
                await self.client.patch(path, {"state": "running", "done": done, "total": total, "ts": _now_ms()})
            except FirebaseError as err:
                _LOGGER.debug("progresso do mapa nao gravado: %s", err)

        try:
            await self.client.patch(path, {"state": "running", "done": 0, "total": 0, "ts": _now_ms(), "error": None})
            started = time.monotonic()
            nodes = await meshmap.collect(self.hass, self._published_device_id, progress)
            await self.client.put(path, {"state": "done", "ts": _now_ms(), "nodes": nodes})
            _LOGGER.info("mapa da rede: %d no(s) em %.0f s", len(nodes), time.monotonic() - started)
        except meshmap.MeshMapError as err:
            _LOGGER.info("mapa da rede indisponivel: %s", err)
            await self._meshmap_fail(path, str(err))
        except asyncio.CancelledError:
            raise
        except FirebaseError as err:
            _LOGGER.warning("mapa da rede nao foi gravado: %s", err)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("falha no mapa da rede")
            await self._meshmap_fail(path, "internal")

    async def _meshmap_fail(self, path: str, code: str) -> None:
        try:
            await self.client.patch(path, {"state": "error", "error": code, "ts": _now_ms()})
        except FirebaseError as err:
            _LOGGER.debug("nao deu pra gravar o erro do mapa: %s", err)

    # ----- presenca -----

    async def _heartbeat_loop(self) -> None:
        info = {"type": "homeassistant", "version": HA_VERSION, "platform": "ha"}
        first = True
        while not self._stopped.is_set():
            try:
                payload: dict[str, Any] = {"online": True, "lastSeen": _now_ms()}
                if first:
                    payload["info"] = info
                await self.client.patch(self._bridge_path(), payload)
                first = False
                await self._publish_alexa()
                await self._reconcile_unpublished()
                for entity_id in self._load_remote_commands():
                    if entity_id in self._published and (state := self.hass.states.get(entity_id)) is not None:
                        await self._publish_device(entity_id, state)
                self._check_missing_entities()
            except FirebaseAuthError as err:
                self._on_auth_failed(err)
                return
            except FirebaseError as err:
                _LOGGER.debug("heartbeat falhou: %s", err)
            except asyncio.CancelledError:
                raise
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)

    def _on_auth_failed(self, err: Exception | None = None) -> None:
        # Ponte ja' parada (reload/reauth): uma escrita em voo com o token
        # antigo pode dar 401 depois - nao e' motivo pra pedir reauth de novo.
        if self.auth_failed or self._stopped.is_set():
            return
        self.auth_failed = True
        _LOGGER.warning(
            "acesso negado pelo AutoDomo (%s) - ponte revogada no app? Pedindo reconexao (um codigo novo).", err
        )
        # Aparece em Configuracoes > Integracoes como "Reautenticar".
        self.entry.async_start_reauth(self.hass)
