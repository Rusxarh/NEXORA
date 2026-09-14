/**
 * NEXORA CONNECTOR - RGM v0.1
 * popup.js
 * Interfaz minima: dispara la consulta y muestra estado/JSON resultante.
 * Ahora tambien permite ver/copiar el diagnostico DOM (separado del JSON
 * de resultado, para no confundir ambos artefactos).
 */

const elEstado = document.getElementById("estado");
const elFuente = document.getElementById("fuente");
const elLabelEntrada = document.getElementById("labelEntrada");
const elPlaca = document.getElementById("placa");
const elBtnConsultar = document.getElementById("btnConsultar");
const elAcciones = document.getElementById("acciones");
const elBtnVerJson = document.getElementById("btnVerJson");
const elBtnCopiarJson = document.getElementById("btnCopiarJson");
const elAccionesDiagnostico = document.getElementById("accionesDiagnostico");
const elBtnVerDiagnostico = document.getElementById("btnVerDiagnostico");
const elBtnCopiarDiagnostico = document.getElementById("btnCopiarDiagnostico");
const elJsonBox = document.getElementById("jsonBox");

let ultimoResultado = null;
let ultimoDiagnostico = null;

function pintarEstado(trabajo) {
  if (!trabajo) return;
  elEstado.textContent = `${trabajo.estado}${trabajo.detalle ? "\n" + trabajo.detalle : ""}`;
}

async function cargarEstadoGuardado() {
  const { trabajoActual, resultadoFinal, ultimoDiagnosticoDOM } = await chrome.storage.local.get([
    "trabajoActual",
    "resultadoFinal",
    "ultimoDiagnosticoDOM",
  ]);
  if (trabajoActual) pintarEstado(trabajoActual);
  if (resultadoFinal) {
    ultimoResultado = resultadoFinal;
    elAcciones.style.display = "flex";
  }
  if (ultimoDiagnosticoDOM) {
    ultimoDiagnostico = ultimoDiagnosticoDOM;
    elAccionesDiagnostico.style.display = "flex";
  }
}
cargarEstadoGuardado();

chrome.storage.onChanged.addListener((cambios) => {
  if (cambios.trabajoActual) pintarEstado(cambios.trabajoActual.newValue);
  if (cambios.resultadoFinal && cambios.resultadoFinal.newValue) {
    ultimoResultado = cambios.resultadoFinal.newValue;
    elAcciones.style.display = "flex";
  }
  if (cambios.ultimoDiagnosticoDOM && cambios.ultimoDiagnosticoDOM.newValue) {
    ultimoDiagnostico = cambios.ultimoDiagnosticoDOM.newValue;
    elAccionesDiagnostico.style.display = "flex";
  }
});

// Cambia solo la etiqueta visible del campo; RGM sigue funcionando
// exactamente igual (no se renombra ni se toca el input #placa).
function actualizarEtiquetaSegunFuente() {
  elLabelEntrada.textContent = elFuente.value === "JUDICIAL" ? "Nombre / Razón Social" : "Placa";
}
elFuente.addEventListener("change", actualizarEtiquetaSegunFuente);
actualizarEtiquetaSegunFuente();

elBtnConsultar.addEventListener("click", () => {
  const valor = elPlaca.value.trim();
  if (!valor) return;
  elAcciones.style.display = "none";
  elAccionesDiagnostico.style.display = "none";
  elJsonBox.style.display = "none";
  ultimoResultado = null;
  ultimoDiagnostico = null;

  if (elFuente.value === "JUDICIAL") {
    elEstado.textContent = "CARGANDO\nAbriendo Consulta Judicial...";
    chrome.runtime.sendMessage({ tipo: "INICIAR_CONSULTA_JUDICIAL", nombre: valor.toUpperCase() });
  } else if (elFuente.value === "RGM_JUDICIAL") {
    elEstado.textContent = "CARGANDO\nEjecutando RGM...";
    chrome.runtime.sendMessage({ tipo: "INICIAR_FLUJO_RGM_JUDICIAL", placa: valor.toUpperCase() });
  } else {
    elEstado.textContent = "CARGANDO\nAbriendo RGM...";
    chrome.runtime.sendMessage({ tipo: "INICIAR_CONSULTA_RGM", placa: valor.toUpperCase() });
  }
});

elBtnVerJson.addEventListener("click", () => {
  if (!ultimoResultado) return;
  elJsonBox.textContent = JSON.stringify(ultimoResultado, null, 2);
  elJsonBox.style.display = elJsonBox.style.display === "none" ? "block" : "none";
});

elBtnCopiarJson.addEventListener("click", async () => {
  if (!ultimoResultado) return;
  try {
    await navigator.clipboard.writeText(JSON.stringify(ultimoResultado, null, 2));
    elBtnCopiarJson.textContent = "Copiado!";
  } catch (e) {
    elBtnCopiarJson.textContent = "Error al copiar";
  }
  setTimeout(() => (elBtnCopiarJson.textContent = "Copiar JSON"), 1200);
});

elBtnVerDiagnostico.addEventListener("click", () => {
  if (!ultimoDiagnostico) return;
  elJsonBox.textContent = JSON.stringify(ultimoDiagnostico, null, 2);
  elJsonBox.style.display = elJsonBox.style.display === "none" ? "block" : "none";
});

elBtnCopiarDiagnostico.addEventListener("click", async () => {
  if (!ultimoDiagnostico) return;
  try {
    await navigator.clipboard.writeText(JSON.stringify(ultimoDiagnostico, null, 2));
    elBtnCopiarDiagnostico.textContent = "Copiado!";
  } catch (e) {
    elBtnCopiarDiagnostico.textContent = "Error al copiar";
  }
  setTimeout(() => (elBtnCopiarDiagnostico.textContent = "Copiar diagnostico"), 1200);
});
