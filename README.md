# AutoDomo para Home Assistant

Leve seus dispositivos do Home Assistant para o app **AutoDomo** — controle em
tempo real, várias pessoas na mesma casa, sem abrir porta nem expor o HA na
internet. O Home Assistant vira uma *ponte* da sua casa no AutoDomo.

[![Abrir no HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=autodomo-oss&repository=ha-autodomo&category=integration)
[![Adicionar integração](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=autodomo)

## Instalar em 3 passos

1. **HACS** → clique no botão *Abrir no HACS* acima (ou *Repositórios
   personalizados* → `autodomo-oss/ha-autodomo`, categoria *Integration*) →
   *Baixar* → reinicie o Home Assistant.
2. No **app AutoDomo**, abra a casa → **Pontes → Adicionar Home Assistant**.
   Aparece um código de 8 letras (vale 10 minutos, uso único).
3. No HA, *Configurações → Dispositivos e serviços → Adicionar integração →*
   **AutoDomo** (ou o botão *Adicionar integração* acima) → digite o código →
   confirme a lista do que o app pode ver. Pronto.

Luzes, interruptores, persianas, clima, fechaduras, ventiladores, aspiradores,
válvulas, aquecedores de água e umidificadores já vêm marcados; sensores,
TVs/som e alarme você escolhe. Tudo pode ser mudado depois em *Configurar*.

## Como funciona

- O HA nunca recebe sua senha: o código vira uma identidade própria da ponte,
  presa àquela casa. No app você vê a ponte e pode **revogar** quando quiser —
  o HA perde o acesso na hora e pede um código novo (*Reautenticar*).
- Estado → app a cada mudança (em ~1 s). Comando do app → serviço do HA.
- Presença: o app mostra a ponte online/offline.
- Entidade exposta que deixa de existir no HA sai do app sozinha (na hora, se
  for apagada; em 5 min, se já não existir quando o HA liga) e o HA avisa em
  *Reparos*. Se ela voltar, reaparece no app.
- *Baixar diagnóstico* na integração gera um relatório sem segredos para suporte.
- Mapa da rede (só para gateways AutoDomo BLE Mesh, só quando o dono da casa
  pede no app): o HA lê pelo próprio MQTT o sinal entre os aparelhos
  (`AT+MESHNODES?`, `AT+DEVICES?`, `AT+NEIGHBORS?`, `AT+MESHNEIGHBORS=`) — só
  leitura, um aparelho por vez — e grava no app endereço, tipo, firmware,
  nome/cômodo do aparelho no HA e o RSSI de cada vizinho. Sem MQTT no HA, nada
  acontece.

## O que é exposto

| Domínio HA | No app |
|---|---|
| light | liga/desliga, brilho, cor, temperatura de cor |
| switch, input_boolean | liga/desliga |
| cover | abrir/fechar/parar, posição, inclinação |
| climate | modo, temperatura alvo/atual, ventilação |
| lock | trancar/destrancar |
| fan | liga/desliga, velocidade |
| sensor, binary_sensor | valor e unidade / ligado-desligado |
| scene, button | ativar / pressionar |
| media_player | liga/desliga, tocar/pausar, faixa, volume, mudo, fonte |
| vacuum | limpar, pausar, parar, voltar à base, localizar, potência |
| alarm_control_panel | armar (em casa/fora/noite/férias) e desarmar, com código |
| valve | abrir/fechar/parar, posição |
| water_heater | liga/desliga, temperatura alvo/atual, modo |
| humidifier | liga/desliga, umidade alvo/atual, modo |
| camera | vídeo ao vivo no app por WebRTC, direto do HA para o celular (o vídeo não passa pela nuvem do AutoDomo; precisa de câmera com stream, ex.: RTSP via go2rtc do HA). **Fotos** (não vídeo) sobem para a nuvem nos avisos de sensor e no Assistente — ver [Fotos das câmeras](#fotos-das-câmeras-o-que-sai-de-casa). Só entra se escolhida nas opções |
| remote | controle no app: Android TV, Apple TV, Roku e Samsung com teclas prontas; Broadlink e outros: os comandos que você digitar em *Configurar* (um por linha, `aparelho/comando`, os nomes usados ao aprender no HA); atividades (ex.: Harmony). Só envia comando que está na lista. Só entra se escolhido nas opções |

Contrato de dados: [`AutoDomo/autodomo-cloud`](https://github.com/AutoDomo/autodomo-cloud)
(`docs/modelo-dados.md`).

## Fotos das câmeras (o que sai de casa)

O vídeo ao vivo vai direto do HA para o celular. **Fotos** paradas, não: elas
saem de casa e ficam na nuvem do AutoDomo por até 24 h. Isso só vale para as
câmeras **escolhidas nas opções** desta integração — câmera não escolhida
nunca é lida.

Quando uma foto é tirada:

- **Aviso de sensor** (ex.: "Porta da frente: aberta"): a nuvem pede uma foto
  da câmera do mesmo cômodo do sensor (ou da que você escolheu para aquele
  sensor). Desligar: no app, na tela do sensor, **Foto no aviso → Sem foto**.
- **Assistente AutoDomo**: quando alguém da casa pede para ele olhar uma câmera
  (ex.: "tem alguém na garagem?"). Essa foto também é enviada ao provedor de IA
  do Assistente (Anthropic, modelo Claude) para ser analisada.

Como é: um JPEG de até 640 px, enviado pelo HA (`camera.async_get_image`) para
`snapshots/{casa}/{aviso}/img` no Firebase Realtime Database do AutoDomo. Só os
membros da casa recebem o link da foto (com uma chave aleatória) no aviso e no
histórico do app.

Como some: o link para de funcionar 24 h depois da foto, e uma tarefa agendada
da nuvem (`snapshotCleanup`, a cada 60 min) apaga do banco as fotos com mais de
24 h. Para nunca mais enviar fotos de uma câmera, desmarque-a em *Configurar*
(ela sai do app junto).

### Camera stills (English)

Live video goes straight from HA to the phone. Still images do leave the house:
only for cameras **selected in this integration's options**, HA uploads a JPEG
(max 640 px) to `snapshots/{home}/{alert}/img` in the AutoDomo Firebase Realtime
Database (1) when a sensor alert is sent (camera in the sensor's room, or the one
chosen for that sensor; turn off per sensor in the app: *Foto no aviso → Sem
foto*) and (2) when a home member asks the AutoDomo Assistant to look at a
camera — that image is also sent to the Assistant's AI provider (Anthropic,
Claude) for analysis. Home members get a link with a random key. The link stops
working after 24 h and a scheduled cloud job (`snapshotCleanup`, every 60 min)
deletes images older than 24 h. Unselect the camera in *Configure* to stop all
uploads from it.

## Problemas comuns

- **Código inválido ou expirado** → gere outro no app (vale 10 min, uma vez).
- **Este código é de outra casa** → na reautenticação, gere o código na mesma
  casa da ponte.
- **Entidade não aparece no app** → confira se está marcada em *Configurar*;
  entidades de integrações MQTT entram alguns segundos depois do boot.
- Logs detalhados: `logger: logs: custom_components.autodomo: debug`.

## Desenvolvimento

Sem dependências além do próprio Home Assistant. A seção *Avançado* do
pareamento aponta para o emulador do `autodomo-cloud`;
`tests/emulator_smoke.py` exercita o cliente Firebase fora do HA.

Licença MIT.
