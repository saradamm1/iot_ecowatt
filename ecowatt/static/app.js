// EcoWatt — dashboard (consulta a API a cada 2 s)
const $ = (id) => document.getElementById(id);
const fmt = (v, d = 1) => (v === null || v === undefined) ? "—" : Number(v).toLocaleString("pt-BR", {minimumFractionDigits: d, maximumFractionDigits: d});
const hora = (ms) => new Date(ms).toLocaleTimeString("pt-BR");
const SEV = {critico: ["crit", "Crítico"], atencao: ["warn", "Atenção"], info: ["info", "Info"], sistema: ["off", "Sistema"]};
const NOME_REGRA = {RN01: "RN01 Desperdício", RN02: "RN02 Sala vazia", RN03: "RN03 Sobrecarga", RN04: "RN04 Meta mensal",
  RN05: "RN05 Qualidade", OFFLINE: "Sensor offline", CMD: "Comando falhou", DADOS: "Dados inválidos"};
let E = null;

document.querySelectorAll(".nav a").forEach(a => a.onclick = () => {
  document.querySelectorAll(".nav a").forEach(x => x.classList.remove("on"));
  document.querySelectorAll(".tab").forEach(x => x.classList.remove("on"));
  a.classList.add("on"); $("tab-" + a.dataset.tab).classList.add("on");
});

function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.style.display = "block";
  clearTimeout(t._h); t._h = setTimeout(() => t.style.display = "none", 3500);
}
async function post(url, body) {
  const r = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: body ? JSON.stringify(body) : null});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) toast(j.detail || j.erro || "Erro"); return r.ok;
}
async function cen(nome) { if (await post("/api/simulacao/" + nome)) toast("Cenário acionado: " + nome); atualizar(); }
async function limpar() { if (confirm("Apagar todas as leituras, alertas e registros?")) { await post("/api/limpar"); atualizar(); } }
async function rele(id, ligar) { if (await post(`/api/circuitos/${id}/rele`, {ligar})) toast((ligar ? "Ligar" : "Desligar") + " enviado"); }
async function reconhecer(id) { await post(`/api/alertas/${id}/reconhecer`); atualizar(); }
async function manter(id) { if (await post(`/api/alertas/${id}/manter-ligado`)) toast("Desligamento cancelado"); atualizar(); }

function pill(cls, txt) { return `<span class="pill ${cls}">${txt}</span>`; }
function statusCircuito(c) {
  if (!c.online) return pill("off", "Offline");
  const ab = (E.alertas || []).filter(a => a.status !== "resolvido" && a.alvo === c.id);
  if (ab.length) return pill(SEV[ab[0].severidade][0], NOME_REGRA[ab[0].regra] || ab[0].regra);
  if (c.rele === false) return pill("warn", "Desligado");
  return pill("ok", "Normal");
}
function toggle(c) {
  const cls = "tog" + (c.rele === false ? " offt" : "") + (!c.desligavel ? " dis" : "");
  const act = c.desligavel ? `onclick="rele('${c.id}', ${c.rele === false})"` : `title="Carga essencial: não pode ser desligada"`;
  return `<span class="${cls}" ${act}></span>`;
}

function grafico(serie, demanda) {
  const W = 760, H = 260, L = 40, R = 10, T = 14, B = 30;
  if (!serie.length) { $("graf").innerHTML = `<text x="380" y="130" text-anchor="middle" fill="#829ab1">aguardando dados…</text>`; return; }
  const vmax = Math.max(demanda * 1.1, ...serie.map(p => p[1])) ;
  const t0 = serie[0][0], t1 = Math.max(serie[serie.length - 1][0], t0 + 1);
  const x = t => L + (t - t0) / (t1 - t0) * (W - L - R), y = v => T + (1 - v / vmax) * (H - T - B);
  let s = "";
  for (let i = 0; i <= 4; i++) { const v = vmax * i / 4; s += `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="#e2e8f0"/><text x="${L - 6}" y="${y(v) + 4}" font-size="11" fill="#52606d" text-anchor="end">${fmt(v, 0)}</text>`; }
  s += `<line x1="${L}" x2="${W - R}" y1="${y(demanda)}" y2="${y(demanda)}" stroke="#c53030" stroke-dasharray="6 4"/><text x="${W - R}" y="${y(demanda) - 5}" font-size="11" fill="#c53030" text-anchor="end">demanda contratada ${fmt(demanda, 0)} kW</text>`;
  s += `<polyline fill="none" stroke="#2b6cb0" stroke-width="2.5" points="${serie.map(p => x(p[0]) + "," + y(p[1])).join(" ")}"/>`;
  [0, 0.5, 1].forEach(f => { const t = t0 + f * (t1 - t0); s += `<text x="${x(t)}" y="${H - 8}" font-size="11" fill="#52606d" text-anchor="${f == 0 ? "start" : f == 1 ? "end" : "middle"}">${hora(t)}</text>`; });
  $("graf").innerHTML = s;
}

const CENARIOS = [
  ["Regras de negócio"],
  ["madrugada", "RN01 — Luz acesa de madrugada", "Simula 02h, sem ninguém no prédio, com a iluminação do 2º andar ligada (2,3× o consumo base). Espera-se alerta e desligamento programado."],
  ["sala_vazia", "RN02 — AC ligado em sala vazia", "A sala 204 fica sem presença com o ar-condicionado ligado. Espera-se notificação com contagem regressiva; teste também o botão “Manter ligado”."],
  ["sobrecarga", "RN03 — Sobrecarga no QD-03", "AC do 3º andar e aquecedor da copa elevam a corrente do quadro acima de 90% do disjuntor geral. Espera-se corte da carga de menor prioridade."],
  ["meta", "RN04 — Meta mensal em risco", "Reduz a meta para 3.600 kWh. A projeção passa de 110% da meta e um relatório é enviado ao gestor."],
  ["tensao", "RN05 — Qualidade de energia", "Tensão do QD-02 cai para 196 V e o fator de potência do AC da sala 204 cai para 0,84."],
  ["Falhas e resiliência (Tabela 2)"],
  ["offline", "Sensor/gateway offline", "O gateway da garagem (QD-00) é desligado abruptamente. O broker publica o LWT e o sistema abre alerta."],
  ["queda_internet", "Queda de internet", "O QD-02 fica sem conexão por alguns segundos, guarda leituras agregadas no buffer local e as reenvia ao voltar (buffered=true). Relés voltam a LIGADO (fail-safe)."],
  ["duplicadas", "Mensagens duplicadas", "O QD-03 reenvia as últimas mensagens com o mesmo número de sequência. Todas devem ser descartadas."],
  ["corrompidos", "Dados nulos ou corrompidos", "O QD-01 envia JSON inválido, campo obrigatório ausente, valor nulo, valor fora da faixa, tipo errado e energia menor que a anterior."],
  ["relogio", "Relógio adiantado", "O AC do 3º andar envia 5 leituras com horário 60 s no futuro. O servidor substitui pelo próprio horário."],
  ["rele_travado", "Relé não responde", "O relé do aquecedor da copa passa a ignorar comandos. O sistema tenta 3 vezes e escala para a manutenção."],
];
$("scen").innerHTML = CENARIOS.map(c => c.length === 1 ? `<h4>${c[0]}</h4>` :
  `<div class="card"><b>${c[1]}</b><p>${c[2]}</p><span class="btn p" onclick="cen('${c[0]}')">Executar</span></div>`).join("");

function render() {
  const e = E, abertos = e.alertas.filter(a => a.status === "aberto");
  $("predio").textContent = e.predio;
  $("perfil").textContent = `Perfil de tempo: ${e.perfil}`;
  const on = e.circuitos.filter(c => c.online).length;
  $("online").innerHTML = `Atualizado ${hora(e.agora)} · ${pill(on === e.circuitos.length ? "ok" : "warn", `● ${on} de ${e.circuitos.length} sensores online`)}`;
  $("nAlertas").textContent = abertos.length;
  $("banner").style.display = e.hora_simulada !== null ? "block" : "none";
  $("banner").innerHTML = `🕑 Relógio do prédio simulado em <b>${String(e.hora_simulada).padStart(2, "0")}:00</b> — ${e.fora_expediente ? "<b>fora do expediente</b>. Use “Voltar ao normal” para retornar ao horário comercial." : "horário de expediente (modo demonstração)."}`;

  $("kpiPot").innerHTML = `${fmt(e.potencia_kw, 1)} <small>kW</small>`;
  $("kpiDem").innerHTML = `Demanda contratada: ${fmt(e.demanda_kw, 0)} kW`;
  const pct = e.projecao_kwh / e.meta_kwh;
  $("kpiProj").innerHTML = `${fmt(e.projecao_kwh, 0)} <small>kWh</small>`;
  $("kpiMeta").innerHTML = `<span class="${pct > 1 ? "up" : "dn"}">Meta: ${fmt(e.meta_kwh, 0)} kWh (${fmt(pct * 100, 0)}%)</span>`;
  $("kpiAl").innerHTML = abertos.length;
  $("kpiAlD").innerHTML = abertos.filter(a => a.severidade === "critico").length + " crítico(s)";
  $("kpiTemp").innerHTML = `${fmt(e.temperatura_media, 1)} <small>°C</small>`;
  $("kpiHora").textContent = e.fora_expediente ? "Fora do expediente" : "Em horário de expediente";
  grafico(e.serie, e.demanda_kw);

  const totalUso = Object.values(e.uso_final).reduce((a, b) => a + b, 0) || 1;
  const cores = {"Climatização": "#2b6cb0", "Iluminação": "#38a169", "Tomadas / TI": "#805ad5", "TI crítica": "#4a5568", "Cozinha": "#dd6b20"};
  $("uso").innerHTML = Object.entries(e.uso_final).sort((a, b) => b[1] - a[1]).map(([k, v]) =>
    `<tr><td>${k}</td><td style="width:45%"><div class="bar"><i style="width:${v / totalUso * 100}%;background:${cores[k] || "#718096"}"></i></div></td><td>${fmt(v / totalUso * 100, 0)}%</td></tr>`).join("");
  $("quadros").innerHTML = e.quadros.map(q => {
    const r = q.corrente / q.disjuntor_a, cor = r > 0.9 ? "#c53030" : r > 0.8 ? "#dd6b20" : "#38a169";
    return `<tr><td>${q.nome}</td><td style="width:40%"><div class="bar"><i style="width:${Math.min(100, r * 100)}%;background:${cor}"></i></div></td><td>${fmt(q.corrente, 1)} / ${q.disjuntor_a} A</td></tr>`;
  }).join("");

  $("tabGeral").innerHTML = `<tr><th>Circuito</th><th>Quadro</th><th>Potência</th><th>Corrente / disjuntor</th><th>Tensão</th><th>FP</th><th>Presença</th><th>Relé</th><th>Status</th></tr>` +
    e.circuitos.map(c => `<tr><td>${c.nome}</td><td>${c.quadro.toUpperCase()}</td><td>${fmt((c.potencia ?? null) / 1000, 2)} kW</td>
    <td>${fmt(c.corrente, 1)} A / ${c.disjuntor_a} A</td><td>${fmt(c.tensao, 1)} V</td><td>${fmt(c.fp, 2)}</td>
    <td>${c.ambiente ? (c.presenca ? "Sim" : `Não (${c.sem_presenca_s} s)`) : "—"}</td><td>${toggle(c)}</td><td>${statusCircuito(c)}</td></tr>`).join("");

  $("tabAlertas").innerHTML = `<tr><th>Severidade</th><th>Regra</th><th>Título</th><th>Detalhe</th><th>Início</th><th>Ação</th><th></th></tr>` +
    (e.alertas.length ? e.alertas.map(a => {
      const sev = SEV[a.severidade] || ["off", a.severidade];
      let acao = a.acao || "";
      if (a.acao_pendente) {
        const s = Math.max(0, Math.round((a.acao_executa_em - e.agora) / 1000));
        acao += `<br><span class="countdown">Desliga em ${s} s</span> <span class="btn r" onclick="manter(${a.id})">Manter ligado</span>`;
      }
      const bt = a.status === "aberto" ? `<span class="btn p" onclick="reconhecer(${a.id})">Reconhecer</span>` :
        pill(a.status === "resolvido" ? "ok" : "info", a.status === "resolvido" ? "Resolvido" : "Reconhecido");
      return `<tr><td>${pill(sev[0], sev[1])}</td><td>${NOME_REGRA[a.regra] || a.regra}</td><td>${a.titulo}</td><td>${a.detalhe}</td><td>${hora(a.criado_em)}</td><td>${acao}</td><td>${bt}</td></tr>`;
    }).join("") : `<tr><td colspan="7" class="muted">Nenhum alerta. Use a aba “Simulação e testes” para provocar os cenários.</td></tr>`);
  $("tabNotif").innerHTML = e.notificacoes.map(n => `<tr><td>${hora(n.enviado_em)}</td><td>${n.canal.toUpperCase()} → ${n.destino}</td><td>${n.mensagem}</td></tr>`).join("") || `<tr><td class="muted">—</td></tr>`;
  const ST = {aguardando: "warn", confirmado: "ok", falhou: "crit"};
  $("tabCmd").innerHTML = e.comandos.map(c => `<tr><td>${hora(c.enviado_em)}</td><td><code>${c.acao}</code> → ${(E.circuitos.find(x => x.id === c.circuito) || {}).nome}</td><td>${c.origem}</td><td>tent. ${c.tentativas}</td><td>${pill(ST[c.status], c.status)}</td></tr>`).join("") || `<tr><td class="muted">—</td></tr>`;

  $("tabCirc").innerHTML = `<tr><th>Circuito</th><th>Gateway</th><th>Uso final</th><th>Disjuntor</th><th>Prioridade</th><th>Desligável</th><th>Último dado</th><th>Sinal</th><th>Relé</th><th>Status</th></tr>` +
    e.circuitos.map(c => `<tr><td>${c.nome}</td><td><code>esp32-${c.id}</code></td><td>${c.uso}</td><td>${c.disjuntor_a} A</td>
    <td>${c.prioridade} — ${["", "essencial", "média", "baixa"][c.prioridade]}</td><td>${c.desligavel ? "Sim" : "Não"}</td>
    <td>${c.visto_ha_s === null ? "—" : "há " + fmt(c.visto_ha_s, 0) + " s"}</td><td>${c.rssi ?? "—"} dBm</td><td>${toggle(c)}</td><td>${c.online ? pill("ok", "Online") : pill("off", "Offline")}</td></tr>`).join("");
  const p = e.parametros;
  $("tabRegras").innerHTML = `<tr><th>Regra</th><th>SE</th><th>ENTÃO</th></tr>
   <tr><td><b>RN01</b></td><td>fora do expediente E potência média de ${p.rn01_janela_s} s &gt; ${p.rn01_fator_base}× consumo base E sem presença</td><td>alerta + desligar após ${p.rn01_espera_desligar_s} s (se desligável)</td></tr>
   <tr><td><b>RN02</b></td><td>sem presença por ≥ ${p.rn02_sem_presenca_s} s E corrente &gt; ${p.rn02_corrente_min_a} A (climatização desligável)</td><td>notificar + desligar após ${p.rn02_espera_desligar_s} s sem cancelamento</td></tr>
   <tr><td><b>RN03</b></td><td>corrente do quadro &gt; ${p.rn03_limite * 100}% do disjuntor geral por ≥ ${p.rn03_janela_s} s OU potência total &gt; demanda contratada</td><td>alerta crítico + cortar cargas de menor prioridade até &lt; ${p.rn03_retorno * 100}%</td></tr>
   <tr><td><b>RN04</b></td><td>dia ≥ ${p.rn04_dia_minimo} E projeção do mês &gt; ${Math.round(p.rn04_limite * 100)}% da meta</td><td>alerta informativo + relatório por e-mail</td></tr>
   <tr><td><b>RN05</b></td><td>tensão fora de ${p.tensao_min_v}–${p.tensao_max_v} V por ≥ ${p.rn05_janela_tensao_s} s OU FP médio de ${p.rn05_janela_fp_s} s &lt; ${p.fp_min}</td><td>alerta para a manutenção</td></tr>`;

  const st = e.stats;
  $("stats").innerHTML = [["Mensagens recebidas", st.recebidas], ["Leituras gravadas", e.total_leituras], ["Duplicadas descartadas", st.duplicadas],
    ["Rejeitadas (inválidas)", st.invalidas], ["Recuperadas do buffer", st.recuperadas_buffer], ["Anomalias tratadas", st.anomalias],
    ["Relógio corrigido", st.relogio_corrigido], ["Alertas gerados", st.alertas]]
    .map(([k, v]) => `<div class="card"><div class="lbl">${k}</div><div class="val">${v || 0}</div></div>`).join("");
  $("tabInv").innerHTML = e.invalidas.map(i => `<tr><td>${hora(i.recebido_em)}</td><td>${i.motivo}</td><td><code>${(i.payload || "").slice(0, 60).replace(/</g, "&lt;")}…</code></td></tr>`).join("") || `<tr><td class="muted">Nenhuma mensagem rejeitada</td></tr>`;
  $("tabAnom").innerHTML = e.anomalias.map(a => `<tr><td>${hora(a.recebido_em)}</td><td><code>${a.device_id}</code></td><td>${a.descricao}</td></tr>`).join("") || `<tr><td class="muted">Nenhuma anomalia</td></tr>`;
}

async function atualizar() {
  try { const r = await fetch("/api/estado"); if (r.ok) { E = await r.json(); render(); } }
  catch (err) { $("online").innerHTML = pill("crit", "Sem conexão com a API"); }
}
atualizar(); setInterval(atualizar, 2000);
