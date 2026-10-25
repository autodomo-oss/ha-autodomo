"""Constantes da integracao AutoDomo."""

from __future__ import annotations

DOMAIN = "autodomo"

# Projeto Firebase da plataforma (ver AutoDomo/autodomo-cloud).
DEFAULT_PROJECT_ID = "autodomo-cloud"
DEFAULT_FUNCTIONS_URL = "https://us-central1-autodomo-cloud.cloudfunctions.net"
DEFAULT_DATABASE_URL = "https://autodomo-cloud-default-rtdb.firebaseio.com"
# Chave Web do Firebase e' publica por natureza (o app tambem a carrega);
# quem protege os dados sao as regras do RTDB. claimBridge devolve a chave
# atual, este e' so' o valor inicial.
DEFAULT_API_KEY = "AIzaSyBx-1cHyfhz56gnWmGnvC9SKD4UY3HBHBg"

# Dados guardados no config entry.
CONF_CODE = "code"
CONF_BRIDGE_ID = "bridge_id"
CONF_HOME_ID = "home_id"
CONF_PROJECT_ID = "project_id"
CONF_DATABASE_URL = "database_url"
CONF_FUNCTIONS_URL = "functions_url"
CONF_API_KEY = "api_key"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_AUTH_EMULATOR_HOST = "auth_emulator_host"
CONF_ADVANCED = "advanced"

# Opcoes.
CONF_ENTITIES = "entities"
CONF_AUTO_ADD = "auto_add"

# Dominios do HA que a ponte sabe publicar (= kinds do contrato).
SUPPORTED_DOMAINS: tuple[str, ...] = (
    "light",
    "switch",
    "input_boolean",
    "cover",
    "sensor",
    "binary_sensor",
    "climate",
    "lock",
    "fan",
    "scene",
    "button",
    "media_player",
    "vacuum",
    "alarm_control_panel",
    "valve",
    "water_heater",
    "humidifier",
    "camera",
    "remote",
)

# Pre-selecionados na instalacao e cobertos pelo "expor automaticamente":
# o que a pessoa controla. Sensores ficam de fora por padrao (costumam ser
# dezenas de entidades de sistema); media_player tambem (o HA cria varios de
# Chromecast/Spotify/grupos), remote (cada TV/Broadlink cria um) e alarme
# (seguranca: so' se a pessoa escolher).
AUTO_DOMAINS: tuple[str, ...] = ("light", "switch", "cover", "climate", "lock", "fan", "vacuum", "valve", "water_heater", "humidifier")

STATE_DEBOUNCE_S = 0.3
HEARTBEAT_INTERVAL_S = 30
COMMAND_MAX_AGE_S = 120  # comandos da fila offline do app sobem ate 2 min depois
STREAM_RETRY_MIN_S = 2
STREAM_RETRY_MAX_S = 60
# Camera (WebRTC): oferta mais velha que isso ao chegar e' descartada; sessao
# aberta (app travou sem apagar) e' fechada depois do limite.
RTC_OFFER_MAX_AGE_S = 60
RTC_SESSION_MAX_S = 1800

# Controle remoto (remote.*): teclas do controle do app -> comando de cada
# integracao que aceita nomes fixos. Broadlink (comandos aprendidos) vem do
# arquivo de codigos dele; o resto so' liga/desliga e atividades.
REMOTE_KEYSETS: dict[str, dict[str, str]] = {
    "androidtv_remote": {
        "power": "POWER", "up": "DPAD_UP", "down": "DPAD_DOWN", "left": "DPAD_LEFT", "right": "DPAD_RIGHT",
        "ok": "DPAD_CENTER", "back": "BACK", "home": "HOME", "menu": "MENU", "input": "TV_INPUT",
        "volUp": "VOLUME_UP", "volDn": "VOLUME_DOWN", "mute": "VOLUME_MUTE", "chUp": "CHANNEL_UP", "chDn": "CHANNEL_DOWN",
    },
    "apple_tv": {
        "up": "up", "down": "down", "left": "left", "right": "right", "ok": "select", "back": "menu",
        "home": "home", "volUp": "volume_up", "volDn": "volume_down",
    },
    "roku": {
        "power": "power", "up": "up", "down": "down", "left": "left", "right": "right", "ok": "select",
        "back": "back", "home": "home", "volUp": "volume_up", "volDn": "volume_down", "mute": "volume_mute",
        "chUp": "channel_up", "chDn": "channel_down",
    },
    "samsungtv": {
        "power": "KEY_POWER", "up": "KEY_UP", "down": "KEY_DOWN", "left": "KEY_LEFT", "right": "KEY_RIGHT",
        "ok": "KEY_ENTER", "back": "KEY_RETURN", "home": "KEY_HOME", "menu": "KEY_MENU", "input": "KEY_SOURCE",
        "volUp": "KEY_VOLUP", "volDn": "KEY_VOLDOWN", "mute": "KEY_MUTE", "chUp": "KEY_CHUP", "chDn": "KEY_CHDOWN",
    },
}
REMOTE_MAX_COMMANDS = 300

ISSUE_MISSING_ENTITY = "missing_entity"
