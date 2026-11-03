"use strict";

const $ = id => document.getElementById(id);
const colors = {mint: "#64e1b6", blue: "#77b6ff", amber: "#f0ba72", purple: "#b6a1ff"};
const temperatureSeries = [
  {key: "environment.temperature_c", name: "Ambient", color: colors.mint},
  {key: "thermistor-probe.temperature_c", name: "NTC probe", color: colors.amber},
  {key: "precision-probe.temperature_c", name: "PT100 probe", color: colors.purple},
];
let state = null, history = {}, seen = {}, range = 120, paused = false, frozenHistory = null;
let selectedSeries = new Set(temperatureSeries.map(item => item.key));
let selectedSlave = "", selectedRegister = "", filter = "all", streamConnected = false;
let toastTimer, frameRequested = false;
const sending = new Set();
const number = value => typeof value === "number" && Number.isFinite(value);
const fmt = (value, digits = 1) => number(value) ? value.toLocaleString(undefined, {maximumFractionDigits: digits, minimumFractionDigits: digits}) : "—";
const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[char]);
const clock = at => new Date(at * 1000).toLocaleTimeString([], {hour12: false});
const payload = suffix => state?.latest[suffix]?.payload || {};
const fresh = suffix => {const item = state?.latest[suffix]; return Boolean(streamConnected && state?.connected && item && !item.retained && state.now - item.at < state.stale_after_s);};

function toast(text, error = false) {
  clearTimeout(toastTimer); $("toast").textContent = text; $("toast").className = `toast${error ? " error" : ""}`;
  toastTimer = setTimeout(() => $("toast").classList.add("hidden"), 5000);
}

function initializeCards() {
  $("actuators").innerHTML = [1,2,3,"relay"].map(channel => {
    const key = channel === "relay" ? "relay" : `do-${channel}`;
    return `<div class="actuator"><div class="actuator-info"><div class="output-icon" id="icon-${key}">${channel === "relay" ? "R" : "0"+channel}</div><div><div class="actuator-name">${channel === "relay" ? "Master relay" : "Digital output " + channel}</div><div class="actuator-status" id="status-${key}">Waiting for state</div></div></div><button class="switch" role="switch" aria-checked="false" aria-label="${channel === "relay" ? "Master relay" : "Digital output " + channel}" id="switch-${key}" disabled></button></div>`;
  }).join("");
  [1,2,3,"relay"].forEach(channel => {
    const key = channel === "relay" ? "relay" : `do-${channel}`;
    $("switch-"+key).addEventListener("click", () => sendControl(channel));
  });
  $("analog-channels").innerHTML = [0,1,2,3].map(i => `<div class="analog-row"><div class="analog-info"><span class="analog-label"><span class="channel-id">AI${i}</span><span id="ai-name-${i}">Channel ${i}</span></span><span class="analog-value"><span id="ai-value-${i}">—</span><small id="ai-unit-${i}"></small></span></div><div class="bar-track"><div class="bar-fill" id="ai-bar-${i}"></div></div><div class="analog-meta"><span id="ai-input-${i}">Waiting for signal</span><span id="ai-percent-${i}">—</span></div></div>`).join("");
  [0,1,2,3].forEach((i) => $("ai-bar-"+i).style.background = Object.values(colors)[i]);
  $("digital-inputs").innerHTML = [1,2,3].map(i => `<div class="digital-row"><div class="digital-state"><span class="led" id="di-led-${i}"></span><span id="di-label-${i}">DI ${i}</span></div><div><span class="input-status" id="di-state-${i}">UNKNOWN</span><span class="input-count" id="di-count-${i}">—<small>pulses</small></span></div></div>`).join("");
}

function appendHistory(suffix, item) {
  if (item.retained || seen[suffix] === item.at) return;
  seen[suffix] = item.at;
  let values = {}, data = item.payload;
  if (suffix === "analog-inputs") {
    for (const channel of data.channels || []) values[String(channel.channel)] = channel.engineering_value ?? channel.input_value;
  } else if (suffix.startsWith("modbus-values/")) {
    for (const [key, register] of Object.entries(data.registers || {})) if (register?.valid === true) values[key] = register.value;
  } else {
    for (const key of ["temperature_c", "humidity_percent"]) values[key] = data[key];
  }
  for (const [key, value] of Object.entries(values)) {
    if (!number(value)) continue;
    const points = history[`${suffix}.${key}`] ||= [];
    if (points.at(-1)?.at !== item.at) points.push({at: item.at, value});
    if (points.length > 360) points.splice(0, points.length - 360);
  }
}

function applyState(next, initial = false) {
  state = next;
  if (initial) {history = next.history || {}; seen = Object.fromEntries(Object.entries(next.latest).map(([key,item]) => [key,item.at]));}
  else for (const [suffix, item] of Object.entries(next.latest)) appendHistory(suffix, item);
  render();
}

function setMetric(id, value, unit) {$(id).innerHTML = `${fmt(value)}<small>${esc(unit)}</small>`;}

function render() {
  if (!state) return;
  const env = payload("environment"), ntc = payload("thermistor-probe"), rtd = payload("precision-probe");
  const identity = Object.values(state.latest).map(item => item.payload).find(item => item.device_id) || {};
  $("device-name").textContent = identity.device_id || state.topic_prefix.split("/")[1] || "Waiting for master";
  $("site-name").textContent = identity.site_id ? `Site / ${identity.site_id}` : "Awaiting device identity";
  $("topic-prefix").textContent = state.topic_prefix;
  $("topic-prefix").title = state.topic_prefix;
  $("message-count").textContent = state.message_count.toLocaleString();
  $("last-seen").textContent = state.last_message ? `Last seen ${Math.max(0, Math.floor(state.now - state.last_message))}s ago` : "No telemetry yet";
  const connected = streamConnected && state.connected;
  let status = !streamConnected ? "Server reconnecting" : !state.connected ? "Broker offline" : !state.fresh ? "Awaiting device" : "Device live";
  if (state.mode === "demo" && connected) status = "Demo mode";
  $("connection-badge").className = "badge " + (state.mode === "demo" && connected ? "demo" : connected && state.fresh ? "online" : !connected ? "offline" : "");
  $("connection-badge").innerHTML = `<i></i>${status}`;
  $("connection-badge").title = state.connection_detail;
  document.body.classList.toggle("stale", !state.fresh || !connected);
  const notice = $("notice");
  if (state.mode === "demo") {notice.className = "notice demo"; notice.textContent = "DEMO WORKSPACE · Readings and controls are simulated locally. No MQTT messages are sent.";}
  else if (!streamConnected) {notice.className = "notice"; notice.textContent = "The local server connection was interrupted. Reconnecting automatically…";}
  else if (!state.connected) {notice.className = "notice"; notice.textContent = `MQTT: ${state.connection_detail}. The console will update when the broker connects.`;}
  else if (!state.fresh) {notice.className = "notice"; notice.textContent = "Broker connected. Waiting for fresh telemetry—run the virtual master with the same topic prefix. Controls stay disabled until device state arrives.";}
  else notice.className = "notice hidden";
  setMetric("ambient-value", env.temperature_c, "°C");
  setMetric("humidity-value", env.humidity_percent, "% RH");
  setMetric("ntc-value", ntc.valid === false ? null : ntc.temperature_c, "°C");
  setMetric("rtd-value", rtd.valid === false ? null : rtd.temperature_c, "°C");
  for (const [id, suffix, data] of [["ambient", "environment", env],["humidity","environment",env],["ntc","thermistor-probe",ntc],["rtd","precision-probe",rtd]]) {
    const fault = data.valid === false;
    $(id+"-note").textContent = fault ? `Sensor fault · ${data.fault || "unknown"}` : fresh(suffix) ? "Live reading · signal healthy" : state.latest[suffix] ? "Last received · signal stale" : "Awaiting telemetry";
    $(id+"-note").classList.toggle("fault", fault);
  }
  renderControls(); renderAnalog(); renderDigital(); renderModbus(); renderActivity();
  $("footer-transport").textContent = state.mode === "demo" ? "LOCAL SIMULATION · No broker traffic" : "MQTT over TLS · server-side connection";
  document.querySelector(".tls-label").lastChild.textContent = state.mode === "demo" ? " Local demo" : " TLS secured";
  scheduleCharts();
}

function outputState(channel) {
  return channel === "relay" ? payload("relay").state : (payload("outputs").channels || []).find(item => item.channel === channel)?.active;
}

function renderControls() {
  let available = false;
  for (const channel of [1,2,3,"relay"]) {
    const key = channel === "relay" ? "relay" : `do-${channel}`, actual = outputState(channel);
    const command = state.commands.find(item => item.target === (channel === "relay" ? "relay" : "do") && item.channel === (channel === "relay" ? null : channel));
    const pending = command?.status === "sent" || sending.has(key);
    const ready = fresh(channel === "relay" ? "relay" : "outputs") && typeof actual === "boolean";
    const button = $("switch-"+key), status = $("status-"+key);
    button.disabled = !ready || pending;
    button.setAttribute("aria-checked", String(actual === true));
    button.setAttribute("aria-busy", String(pending));
    $("icon-"+key).classList.toggle("on", actual === true && ready);
    status.className = "actuator-status";
    if (pending) {const desired = command?.state ?? !actual; status.textContent = `Awaiting ${desired ? "ON" : "OFF"} state…`; status.classList.add("pending");}
    else if (command && ["timeout","failed"].includes(command.status) && state.now - command.at < 60) {status.textContent = command.status === "timeout" ? "No device confirmation" : "Command not sent"; status.classList.add("error");}
    else if (!ready) status.textContent = typeof actual === "boolean" ? "Last state · stale" : "Waiting for state";
    else {status.textContent = actual ? "ON · Device reported" : "OFF · Device reported"; status.classList.toggle("on", actual);}
    available ||= ready;
  }
  $("control-ready-dot").classList.toggle("mint", available);
  $("controls-note").textContent = available ? "Commands are confirmed by device telemetry" : "Waiting for fresh device state";
}

async function sendControl(channel) {
  const key = channel === "relay" ? "relay" : `do-${channel}`;
  if (sending.has(key)) return;
  const desired = !outputState(channel);
  sending.add(key); renderControls();
  try {
    const response = await fetch("/api/control", {method: "POST", headers: {"Content-Type":"application/json", "X-Control-Token":state.csrf}, body: JSON.stringify({target:channel === "relay" ? "relay" : "do", channel:channel === "relay" ? null : channel, state:desired})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Could not send control");
    state.commands.unshift(result.command);
    toast(`${channel === "relay" ? "Relay" : "DO " + channel} command sent. Waiting for device state.`);
  } catch (error) {toast(error.message || "Local server unavailable", true);}
  finally {sending.delete(key); renderControls();}
}

function renderAnalog() {
  const channels = payload("analog-inputs").channels || [];
  for (let i = 0; i < 4; i++) {
    const data = channels.find(item => item.channel === i) || {};
    $("ai-name-"+i).textContent = data.name || `Channel ${i}`;
    $("ai-value-"+i).textContent = fmt(data.engineering_value ?? data.input_value, 2);
    $("ai-unit-"+i).textContent = data.unit || "";
    $("ai-bar-"+i).style.width = `${number(data.percent) ? Math.max(0, Math.min(100, data.percent)) : 0}%`;
    $("ai-input-"+i).textContent = data.overrun_alarm ? "Overrun alarm" : data.under_range ? "Below range" : data.over_range ? "Above range" : number(data.input_value) ? `Input ${fmt(data.input_value, 2)} ${i < 2 ? "V" : "mA"}` : "Waiting for signal";
    $("ai-percent-"+i).textContent = `${fmt(data.percent)}%`;
  }
}

function renderDigital() {
  const channels = payload("contact-inputs").channels || [];
  for (let i = 1; i <= 3; i++) {
    const data = channels.find(item => item.channel === i) || {};
    $("di-label-"+i).textContent = data.label || `DI ${i}`;
    $("di-led-"+i).classList.toggle("on", data.active === true && fresh("contact-inputs"));
    $("di-state-"+i).textContent = typeof data.active !== "boolean" ? "UNKNOWN" : !fresh("contact-inputs") ? "STALE" : data.active ? "ACTIVE" : "INACTIVE";
    $("di-count-"+i).innerHTML = `${esc(data.counter ?? "—")}<small>${esc(data.unit || "pulses")}</small>`;
  }
}

function renderModbus() {
  const slaves = Object.keys(state.latest).filter(key => key.startsWith("modbus-values/")).sort((a,b) => Number(a.split("/")[1]) - Number(b.split("/")[1]));
  const select = $("slave-select"), currentKeys = Array.from(select.options).map(option => option.value).join(",");
  if (currentKeys !== slaves.join(",")) {
    select.innerHTML = slaves.length ? slaves.map(key => `<option value="${esc(key)}">Slave ${esc(payload(key).slave_address ?? key.split("/")[1])} · Channel ${esc(payload(key).channel_id ?? key.split("/")[1])}</option>`).join("") : '<option value="">Waiting for slaves</option>';
  }
  if (!slaves.includes(selectedSlave)) selectedSlave = slaves[0] || "";
  select.value = selectedSlave;
  const registers = payload(selectedSlave).registers || {}, keys = Object.keys(registers);
  if (!keys.includes(selectedRegister)) selectedRegister = keys[0] || "";
  $("register-list").innerHTML = keys.length ? keys.map(key => {
    const item = registers[key] || {};
    return `<button class="register-row ${key === selectedRegister ? "selected" : ""}" data-register="${esc(key)}" aria-pressed="${key === selectedRegister}"><span>${esc(key)}</span><span class="register-value">${item.valid === true ? fmt(item.value, 2) : "—"} ${esc(item.unit || "")}</span><span class="${item.overrun_alarm || item.valid !== true ? "invalid-label" : "valid-label"}">${!fresh(selectedSlave) ? "● Stale" : item.valid !== true ? "● Invalid" : item.overrun_alarm ? "● Alarm" : "● Valid"}</span></button>`;
  }).join("") : '<div class="empty-row">Waiting for register data</div>';
  const current = registers[selectedRegister];
  $("modbus-chart-name").textContent = selectedRegister ? `${selectedRegister} / trend` : "Select a register";
  $("modbus-chart-value").textContent = current ? `${current.valid ? fmt(current.value, 2) : "—"} ${current.unit || ""}` : "—";
}

function renderActivity() {
  const events = state.events.filter(item => filter === "all" || (filter === "commands" ? ["tx","confirmed","warning"].includes(item.kind) : item.kind === filter));
  $("activity-body").innerHTML = events.slice(0,35).map(item => `<tr><td>${clock(item.at)}</td><td><span class="event-tag ${esc(item.kind)}">${esc({rx:"RX",tx:"TX",confirmed:"STATE",warning:"WARN",connection:"LINK"}[item.kind] || item.kind)}</span></td><td class="topic-cell">${esc(item.topic || item.detail)}</td><td class="result-cell">${esc(item.kind === "rx" ? "Received" : item.kind === "confirmed" ? item.detail : item.kind === "tx" ? item.detail : "Connection event")}</td></tr>`).join("") || '<tr><td colspan="4" class="empty-row">No matching activity yet.</td></tr>';
}

function canvasContext(canvas) {
  const rect = canvas.getBoundingClientRect(), ratio = window.devicePixelRatio || 1;
  if (rect.width < 1 || rect.height < 1) return null;
  if (canvas.width !== Math.round(rect.width * ratio) || canvas.height !== Math.round(rect.height * ratio)) {canvas.width = Math.round(rect.width * ratio); canvas.height = Math.round(rect.height * ratio);}
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio,0,0,ratio,0,0); ctx.clearRect(0,0,rect.width,rect.height);
  return {ctx, width:rect.width, height:rect.height};
}

function drawLine(canvas, series, compact = false) {
  const surface = canvasContext(canvas); if (!surface) return;
  const {ctx,width,height} = surface, source = paused ? frozenHistory : history;
  const end = paused ? (frozenHistory?.__at || Date.now()/1000) : state?.now || Date.now()/1000, start = end - range;
  const sets = series.map(item => ({...item, points:(source?.[item.key] || []).filter(point => point.at >= start && point.at <= end + 1 && number(point.value))}));
  const all = sets.flatMap(item => item.points.map(point => point.value));
  const pad = compact ? {l:1,r:2,t:5,b:5} : {l:38,r:12,t:27,b:25};
  const w = width - pad.l - pad.r, h = height - pad.t - pad.b;
  let low = Math.min(...all), high = Math.max(...all);
  if (!all.length) {low=0;high=100;}
  const margin = Math.max((high-low)*.18, compact ? .25 : 1);
  low -= margin; high += margin;
  const x = at => pad.l + (at-start)/range * w, y = value => pad.t + (high-value)/(high-low) * h;
  if (!compact) {
    ctx.font = '9px "Segoe UI",sans-serif'; ctx.textBaseline = "middle";
    for (let i=0;i<=4;i++) {const at = pad.t+h*i/4;ctx.beginPath();ctx.strokeStyle="#243246";ctx.lineWidth=.7;ctx.setLineDash([3,4]);ctx.moveTo(pad.l,at);ctx.lineTo(width-pad.r,at);ctx.stroke();ctx.setLineDash([]);ctx.fillStyle="#647c99";ctx.textAlign="right";ctx.fillText((high-(high-low)*i/4).toFixed(high-low<10?1:0),pad.l-9,at);}
    ctx.textAlign="center";ctx.fillStyle="#657d98";
    for(let i=0;i<=4;i++){const at=start+range*i/4;ctx.fillText(new Date(at*1000).toLocaleTimeString([],{minute:"2-digit",second:"2-digit"}),Math.min(width-20,Math.max(32,x(at))),height-8);}
  }
  if (!all.length) {if(!compact){ctx.textAlign="center";ctx.fillStyle="#677d98";ctx.font='11px "Segoe UI",sans-serif';ctx.fillText("Waiting for live samples",pad.l+w/2,pad.t+h/2);}return;}
  ctx.save();ctx.beginPath();ctx.rect(pad.l,pad.t,w,h);ctx.clip();
  for (const set of sets) {
    if (!set.points.length) continue;
    const points = set.points;
    if (compact || sets.length === 1) {const gradient=ctx.createLinearGradient(0,pad.t,0,height);gradient.addColorStop(0,set.color+"30");gradient.addColorStop(1,set.color+"00");ctx.beginPath();ctx.moveTo(x(points[0].at),pad.t+h);points.forEach(point=>ctx.lineTo(x(point.at),y(point.value)));ctx.lineTo(x(points.at(-1).at),pad.t+h);ctx.closePath();ctx.fillStyle=gradient;ctx.fill();}
    ctx.beginPath();ctx.lineWidth=compact?1.6:2;ctx.strokeStyle=set.color;ctx.lineJoin="round";ctx.lineCap="round";points.forEach((point,i)=>i?ctx.lineTo(x(point.at),y(point.value)):ctx.moveTo(x(point.at),y(point.value)));ctx.stroke();
    const last=points.at(-1);ctx.beginPath();ctx.arc(x(last.at),y(last.value),compact?2:3,0,Math.PI*2);ctx.fillStyle=set.color;ctx.fill();
  }
  ctx.restore();canvas.chartData={sets,start,end,pad,width,height};
}

function drawGauge() {
  const surface=canvasContext($("humidity-gauge"));if(!surface)return;
  const {ctx,width,height}=surface, value=payload("environment").humidity_percent;
  const cx=width/2, cy=height*.72, radius=Math.min(width*.38,height*.56);
  ctx.lineWidth=5;ctx.lineCap="round";ctx.strokeStyle="#273749";ctx.beginPath();ctx.arc(cx,cy,radius,Math.PI,Math.PI*2);ctx.stroke();
  if(number(value)){ctx.strokeStyle=colors.blue;ctx.beginPath();ctx.arc(cx,cy,radius,Math.PI,Math.PI+Math.PI*Math.max(0,Math.min(100,value))/100);ctx.stroke();}
  ctx.font='7px "Segoe UI",sans-serif';ctx.fillStyle="#617b98";ctx.textAlign="center";ctx.fillText("0",cx-radius,cy+11);ctx.fillText("100",cx+radius,cy+11);
}

function scheduleCharts() {
  if(frameRequested)return;frameRequested=true;
  requestAnimationFrame(()=>{frameRequested=false;
    drawLine($("temperature-chart"),temperatureSeries.filter(item=>selectedSeries.has(item.key)));
    drawLine($("ambient-spark"),[temperatureSeries[0]],true);
    drawLine($("ntc-spark"),[temperatureSeries[1]],true);
    drawLine($("rtd-spark"),[temperatureSeries[2]],true);drawGauge();
    drawLine($("modbus-chart"),selectedSlave&&selectedRegister?[{key:`${selectedSlave}.${selectedRegister}`,color:colors.blue,name:selectedRegister}]:[]);
  });
}

function activateButton(container, target) {container.querySelectorAll("button").forEach(button=>{const active=button===target;button.classList.toggle("selected",active);button.setAttribute("aria-pressed",String(active));});}

initializeCards();
$("range-control").addEventListener("click",event=>{const button=event.target.closest("button");if(!button)return;range=Number(button.dataset.range);activateButton($("range-control"),button);scheduleCharts();});
$("temperature-legend").addEventListener("click",event=>{const button=event.target.closest("button");if(!button)return;const key=button.dataset.series;if(selectedSeries.has(key)&&selectedSeries.size===1)return;selectedSeries.has(key)?selectedSeries.delete(key):selectedSeries.add(key);button.classList.toggle("selected",selectedSeries.has(key));button.setAttribute("aria-pressed",String(selectedSeries.has(key)));scheduleCharts();});
$("pause-button").addEventListener("click",()=>{paused=!paused;frozenHistory=paused?{...structuredClone(history),__at:state?.now || Date.now()/1000}:null;$("pause-button").textContent=paused?"▶ Resume chart":"Ⅱ Pause chart";$("pause-button").setAttribute("aria-pressed",String(paused));scheduleCharts();});
$("slave-select").addEventListener("change",event=>{selectedSlave=event.target.value;selectedRegister="";renderModbus();scheduleCharts();});
$("register-list").addEventListener("click",event=>{const button=event.target.closest("button");if(!button)return;selectedRegister=button.dataset.register;renderModbus();scheduleCharts();});
$("activity-filter").addEventListener("click",event=>{const button=event.target.closest("button");if(!button)return;filter=button.dataset.filter;activateButton($("activity-filter"),button);renderActivity();});
$("export-button").addEventListener("click",()=>{if(!state)return toast("Waiting for console data",true);const blob=new Blob([JSON.stringify({exported_at:new Date().toISOString(),mode:state.mode,topic_prefix:state.topic_prefix,latest:state.latest,history,commands:state.commands,events:state.events},null,2)],{type:"application/json"});const url=URL.createObjectURL(blob);const link=document.createElement("a");link.href=url;link.download=`pn-master-snapshot-${new Date().toISOString().replace(/[:.]/g,"-")}.json`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);toast("Snapshot exported");});
$("temperature-chart").addEventListener("pointermove",event=>{const chart=event.currentTarget, data=chart.chartData;if(!data)return;const rect=chart.getBoundingClientRect(),position=event.clientX-rect.left,at=data.start+(position-data.pad.l)/(data.width-data.pad.l-data.pad.r)*range;const lines=[];let timestamp=null;for(const series of data.sets){if(!series.points.length)continue;const point=series.points.reduce((best,item)=>Math.abs(item.at-at)<Math.abs(best.at-at)?item:best);timestamp=point.at;lines.push(`${series.name}  ${fmt(point.value)} °C`);}if(!lines.length)return;const tooltip=$("chart-tooltip");tooltip.textContent=`${clock(timestamp)}\n${lines.join("\n")}`;tooltip.classList.remove("hidden");tooltip.style.left=`${Math.max(4,Math.min(rect.width-165,position+10))}px`;tooltip.style.top="28px";});
$("temperature-chart").addEventListener("pointerleave",()=>$("chart-tooltip").classList.add("hidden"));
new ResizeObserver(scheduleCharts).observe(document.querySelector("main"));
setInterval(()=>$("clock").textContent=new Date().toLocaleTimeString([],{hour12:false}),1000);

async function connect() {
  try {
    const response=await fetch("/api/state");if(!response.ok)throw new Error("Local server unavailable");
    streamConnected=true;applyState(await response.json(),true);
    const source=new EventSource("/api/events");
    source.onmessage=event=>{try{streamConnected=true;applyState(JSON.parse(event.data));}catch(error){console.error("Invalid console update",error);}};
    source.onerror=()=>{streamConnected=false;render();};
  } catch(error) {$("notice").className="notice";$("notice").textContent="Cannot reach the local server. Retrying…";setTimeout(connect,3000);}
}
connect();
