# EcoWatt — Protótipo funcional (Parte B)

Protótipo do sistema IoT de monitoramento e eficiência energética especificado na Parte A.
Tudo roda localmente, sem hardware e sem instalar outros programas além do Python.

## Como rodar no Windows

1. Descompacte a pasta em um local simples, por exemplo `C:\EcoWatt`.
2. Dê dois cliques em **`iniciar.bat`**.
   - Na primeira vez, ele cria o ambiente virtual e instala as dependências (leva 1 a 2 minutos e precisa de internet).
   - Se o Windows perguntar sobre o firewall, pode permitir apenas em redes privadas (tudo roda em `localhost`).
3. O navegador abre sozinho em **http://localhost:8000**. Se não abrir, digite o endereço.
4. Para encerrar, feche a janela preta ou pressione `Ctrl+C` nela.

Para rodar os testes automatizados: dois cliques em **`testar.bat`** (31 testes).

### Pelo terminal (alternativa)

```bat
cd C:\EcoWatt
py -3.13 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python iniciar.py            REM sobe tudo e abre o navegador
python -m pytest -v          REM roda os testes
```

Opções do `iniciar.py`: `--limpar` (apaga o banco antes de iniciar), `--sem-navegador`, `--sem-simulador`
(nesse caso, rode `python simulador.py` em outro terminal).

## O que acontece quando você inicia

| Componente (Figura 1) | No protótipo | Arquivo |
|---|---|---|
| Sensores + gateway ESP32 | Simulador: um cliente MQTT por quadro, com LWT, nº de sequência, buffer local e relé fail-safe | `simulador.py` |
| Broker MQTT (Mosquitto) | Broker MQTT embutido em Python (amqtt), porta 1883 | `ecowatt/servidor.py` |
| Middleware (Node-RED) | Serviço Python: validação, deduplicação, regras RN01–RN05, notificações e comandos | `ecowatt/nucleo.py`, `ecowatt/validacao.py` |
| InfluxDB + PostgreSQL | SQLite (arquivo `ecowatt.db`) | `ecowatt/banco.py` |
| API FastAPI + dashboard | API REST e dashboard web em http://localhost:8000 | `ecowatt/servidor.py`, `ecowatt/static/` |

Tópicos MQTT: `ecowatt/predio01/{quadro}/{circuito}/telemetria`, `…/{quadro}/status` (LWT) e `…/{circuito}/cmd`.
A documentação automática da API fica em http://localhost:8000/docs.

## Roteiro de demonstração

Abra a aba **Simulação e testes** e clique em **Executar** em cada cenário. Depois confira as abas
**Alertas** e **Registro técnico**. Use **Voltar ao normal** entre um cenário e outro.

| Cenário | O que observar |
|---|---|
| RN01 — Luz acesa de madrugada | Relógio simulado vai para 02h; após ~16 s abre alerta e, 20 s depois, o relé da iluminação do 2º andar é desligado |
| RN02 — AC em sala vazia | Alerta com contagem regressiva; clique em **Manter ligado** para cancelar, ou espere o desligamento |
| RN03 — Sobrecarga no QD-03 | Alerta crítico; o aquecedor da copa (prioridade baixa) é desligado e a corrente do quadro volta ao normal |
| RN04 — Meta mensal | Meta reduzida para 3.600 kWh; alerta informativo com os maiores consumidores |
| RN05 — Qualidade de energia | Alertas de tensão baixa no QD-02 e de fator de potência baixo |
| Sensor offline | Gateway da garagem cai; o broker publica o LWT e o alerta aparece imediatamente |
| Queda de internet | QD-02 fica 30 s sem conexão; ao voltar, as leituras do buffer entram com `buffered=true` |
| Mensagens duplicadas | Contador de duplicadas aumenta no Registro técnico; nada é gravado em dobro |
| Dados corrompidos | Mensagens rejeitadas e anomalias aparecem no Registro técnico |
| Relógio adiantado | 5 leituras com horário corrigido pelo servidor |
| Relé não responde | 3 tentativas de comando e alerta escalado para a manutenção |

## Perfis de tempo

Por padrão o protótipo usa o perfil **demo**, com janelas curtas (segundos) para as regras dispararem
durante a apresentação, e leituras a cada 2 s. Para usar os tempos especificados no documento
(leitura a cada 5 s, RN01 com 15 min, RN02 com 30 min etc.):

```bat
set ECOWATT_PERFIL=real
python iniciar.py
```

Os parâmetros ficam em `ecowatt/config.py`.

## Problemas comuns

- **"python não é reconhecido"**: reinstale o Python marcando *Add Python to PATH*, ou use `py` no lugar de `python`.
- **Porta 8000 ou 1883 em uso**: feche outra instância do protótipo, ou defina `set ECOWATT_API_PORTA=8080`
  / `set ECOWATT_MQTT_PORTA=1884` antes de iniciar.
- **Quero começar do zero**: rode `iniciar.bat --limpar` ou clique em **Limpar dados** no dashboard.
