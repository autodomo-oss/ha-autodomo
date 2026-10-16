"""Atualizacao da propria integracao pelo GitHub - sem HACS e sem login.

O repositorio e' publico: a versao nova vem de releases/latest (API anonima,
uma consulta a cada 6 h) e o pacote do codeload. Instalar troca os arquivos de
custom_components/autodomo e abre um reparo "Reiniciar" (o codigo novo so'
carrega depois do reinicio). Se o HACS ja' cuida do AutoDomo neste HA, a
entidade nao e' criada (seriam dois avisos da mesma atualizacao).
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import tempfile
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import Any

from awesomeversion import AwesomeVersion

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.loader import async_get_integration

from .const import DOMAIN, GITHUB_REPO, ISSUE_RESTART_REQUIRED

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(hours=6)
LATEST_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
ZIP_URL = "https://codeload.github.com/" + GITHUB_REPO + "/zip/refs/tags/{tag}"
MAX_ZIP_BYTES = 5 * 1024 * 1024
HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "ha-autodomo"}


def _managed_by_hacs(hass: HomeAssistant) -> bool:
    """O HACS registra update.<repo> com plataforma hacs."""
    return any(
        e.platform == "hacs" and e.domain == "update" and "autodomo" in (e.entity_id + str(e.original_name or "")).lower()
        for e in er.async_get(hass).entities.values()
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    # Uma entidade so' por HA, mesmo com mais de uma ponte.
    if hass.config_entries.async_entries(DOMAIN)[0].entry_id != entry.entry_id or _managed_by_hacs(hass):
        return
    integration = await async_get_integration(hass, DOMAIN)
    async_add_entities([AutodomoUpdate(str(integration.version), Path(integration.file_path))], update_before_add=True)


def _install_files(zip_bytes: bytes, target: Path, work: Path, version: str) -> None:
    """Extrai custom_components/autodomo do zip e troca a pasta da integracao."""
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        prefix = next((n[: -len("manifest.json")] for n in zf.namelist() if n.endswith("custom_components/autodomo/manifest.json")), None)
        if prefix is None:
            raise HomeAssistantError("pacote sem custom_components/autodomo")
        new = work / "new"
        for name in zf.namelist():
            if not name.startswith(prefix) or name.endswith("/"):
                continue
            dest = (new / name[len(prefix):]).resolve()
            if new.resolve() not in dest.parents:
                raise HomeAssistantError(f"caminho invalido no pacote: {name}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(name))
    manifest = json.loads((new / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("domain") != DOMAIN or manifest.get("version") != version:
        raise HomeAssistantError(f"manifesto inesperado: {manifest.get('domain')} {manifest.get('version')}")
    old = work / "old"
    os.replace(target, old)
    try:
        shutil.move(str(new), str(target))
    except Exception:
        os.replace(old, target)  # volta a versao que estava
        raise
    shutil.rmtree(work, ignore_errors=True)


class AutodomoUpdate(UpdateEntity):
    _attr_name = "AutoDomo"
    _attr_title = "AutoDomo"
    _attr_unique_id = f"{DOMAIN}_integration_update"
    _attr_supported_features = UpdateEntityFeature.INSTALL | UpdateEntityFeature.RELEASE_NOTES
    _attr_should_poll = True

    def __init__(self, installed: str, path: Path) -> None:
        self._attr_installed_version = installed
        self._attr_latest_version = installed
        self._path = path
        self._tag: str | None = None
        self._notes: str | None = None

    async def async_update(self) -> None:
        try:
            async with async_get_clientsession(self.hass).get(LATEST_URL, headers=HEADERS, timeout=30) as resp:
                if resp.status != 200:
                    _LOGGER.debug("GitHub releases/latest: HTTP %s", resp.status)
                    return
                data = await resp.json()
        except Exception as err:  # noqa: BLE001 - sem internet nao e' erro da integracao
            _LOGGER.debug("nao deu para consultar a versao nova: %s", err)
            return
        tag = str(data.get("tag_name") or "")
        if not tag:
            return
        self._tag = tag
        self._attr_latest_version = tag.lstrip("v")
        self._attr_release_url = data.get("html_url")
        self._notes = data.get("body") or None

    async def async_release_notes(self) -> str | None:
        return self._notes

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        version = (version or self.latest_version or "").lstrip("v")
        if not version or AwesomeVersion(version) <= AwesomeVersion(self.installed_version or "0"):
            raise HomeAssistantError("nenhuma versao nova para instalar")
        tag = self._tag if self._tag and self._tag.lstrip("v") == version else f"v{version}"
        async with async_get_clientsession(self.hass).get(ZIP_URL.format(tag=tag), headers=HEADERS, timeout=120) as resp:
            if resp.status != 200:
                raise HomeAssistantError(f"download da versao {tag} falhou: HTTP {resp.status}")
            body = await resp.content.read(MAX_ZIP_BYTES + 1)
        if len(body) > MAX_ZIP_BYTES:
            raise HomeAssistantError("pacote grande demais")
        work = Path(self.hass.config.path(".autodomo_update"))
        await self.hass.async_add_executor_job(_install_files, body, self._path, work, version)
        _LOGGER.info("AutoDomo %s instalado - reinicie o Home Assistant", version)
        self._attr_installed_version = version
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_RESTART_REQUIRED,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_RESTART_REQUIRED,
            translation_placeholders={"version": version},
        )
        self.async_write_ha_state()
