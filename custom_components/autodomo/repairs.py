"""Reparos com correcao: "Reiniciar" depois de instalar uma versao nova."""

from __future__ import annotations

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant

from .const import ISSUE_RESTART_REQUIRED


class RestartRequiredFlow(RepairsFlow):
    async def async_step_init(self, user_input: dict[str, str] | None = None) -> data_entry_flow.FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, str] | None = None) -> data_entry_flow.FlowResult:
        if user_input is not None:
            await self.hass.services.async_call("homeassistant", "restart")
            return self.async_create_entry(data={})
        return self.async_show_form(step_id="confirm")


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data: dict | None) -> RepairsFlow:
    if issue_id == ISSUE_RESTART_REQUIRED:
        return RestartRequiredFlow()
    raise ValueError(f"reparo desconhecido: {issue_id}")
