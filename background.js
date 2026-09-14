/**
 * NEXORA CONNECTOR - RGM v0.1
 * background.js (service worker, Manifest V3)
 *
 * AJUSTE: flujo directo por URL (mecanismo principal), en vez de abrir el
 * home del RGM y rellenar el formulario. La URL se construye con
 * URL/URLSearchParams (nunca concatenando texto a mano), a partir de una
 * placa normalizada y validada aqui mismo.
 *
 * Prototipo aislado: no hay conexion con NEXORA API todavia.
 */

const BASE_RGM_CONSULTA = "https://www.garantiasmobiliarias.com.co/rgm/Garantias/ConsultaGarantia.aspx";
const REGEX_PLACA_AUTO = /^[A-Z]{3}[0-9]{3}$/;

// Pestana Judicial actualmente abierta por este Connector (si hay una),
// para poder cerrarla automaticamente al terminar. NO afecta ni comparte
// nada con el flujo de RGM: RGM nunca asigna esta variable, y su pestana
// nunca se cierra automaticamente (sigue igual que siempre).
let tabJudicialActual = null;

// --- Orquestacion RGM -> JUDICIAL (nueva, esta correccion) --------------
// resolverEsperaRgm: si esta definido, el PROXIMO RESULTADO_FINAL que
// llegue se le entrega a el en vez de solo guardarse (ver mas abajo). Es
// el mecanismo para que ejecutarFlujoRgmJudicial() pueda "esperar" a que
// RGM termine antes de decidir si lanza Judicial. No cambia en nada el uso
// normal de RGM o Judicial por separado: si nadie arma esta espera,
// resolverEsperaRgm es null y el codigo se comporta exactamente igual que
// antes de esta correccion.
let resolverEsperaRgm = null;
// placa_origen pendiente de asociarse al proximo resultado de Judicial que
// provenga especificamente del flujo encadenado (no de un uso aislado de
// Judicial desde el popup).
let placaOrigenPendiente = null;

chrome.runtime.onMessage.addListener((mensaje, sender, sendResponse) => {
  if (mensaje.tipo === "INICIAR_CONSULTA_RGM") {
    iniciarConsulta(mensaje.placa);
    sendResponse({ ok: true });
    return true;
  }

  if (mensaje.tipo === "INICIAR_CONSULTA_JUDICIAL") {
    iniciarConsultaJudicial(mensaje.nombre);
    sendResponse({ ok: true });
    return true;
  }

  if (mensaje.tipo === "INICIAR_FLUJO_RGM_JUDICIAL") {
    ejecutarFlujoRgmJudicial(mensaje.placa);
    sendResponse({ ok: true });
    return true;
  }

  // Reenvio/registro de lo que informa el content script (RGM o Judicial,
  // ambos usan los mismos dos tipos de mensaje genericos).
  if (mensaje.tipo === "ESTADO_ACTUALIZADO") {
    chrome.storage.local.set({
      trabajoActual: {
        estado: mensaje.estado,
        detalle: mensaje.detalle,
        timestamp: mensaje.timestamp,
      },
    });
  }
  if (mensaje.tipo === "RESULTADO_FINAL") {
    let resultado = mensaje.resultado;

    // Si este resultado es de Judicial Y viene de un flujo encadenado en
    // curso, se le adjunta la placa de origen (nunca se sustituye nada de
    // RGM: esto solo etiqueta el resultado de Judicial).
    if (placaOrigenPendiente && resultado && resultado.fuente === "RAMA_JUDICIAL") {
      resultado = { ...resultado, placa_origen: placaOrigenPendiente };
      placaOrigenPendiente = null;
    }

    chrome.storage.local.set({ resultadoFinal: resultado });

    // Si alguien esta esperando este resultado (ejecutarFlujoRgmJudicial
    // esperando a que RGM termine), se lo entregamos ademas de guardarlo.
    if (resolverEsperaRgm) {
      const resolver = resolverEsperaRgm;
      resolverEsperaRgm = null;
      resolver(resultado);
    }

    // Si el resultado vino de la pestana temporal de Judicial, cerrarla
    // automaticamente desde el service worker (nunca window.close()).
    if (sender.tab && tabJudicialActual !== null && sender.tab.id === tabJudicialActual) {
      chrome.tabs.remove(tabJudicialActual).catch(() => {});
      tabJudicialActual = null;
    }
  }
  return true;
});

/**
 * Normaliza una placa de entrada: mayusculas, sin espacios, sin caracteres
 * no alfanumericos. Valida formato de automovil colombiano LLLDDD.
 * No altera el contenido en si (no "corrige" letras/numeros), solo limpia
 * formato.
 */
function normalizarPlaca(entrada) {
  const limpia = (entrada || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
  return { placa: limpia, valida: REGEX_PLACA_AUTO.test(limpia) };
}

/**
 * Construye la URL de consulta mediante URL + URLSearchParams (codificacion
 * segura de parametros, nunca concatenacion manual de texto).
 */
function construirUrlConsultaGarantia(placa) {
  const url = new URL(BASE_RGM_CONSULTA);
  url.searchParams.set("NumeroBien", placa);
  url.searchParams.set("ConsultaOficial", "false");
  return url.toString();
}

async function publicarEstado(estado, detalle) {
  await chrome.storage.local.set({
    trabajoActual: { estado, detalle: detalle || null, timestamp: new Date().toISOString() },
  });
}

async function iniciarConsulta(placaCruda) {
  await chrome.storage.local.set({ resultadoFinal: null });
  await publicarEstado("VALIDANDO_PLACA", `Validando "${placaCruda}"...`);

  const { placa, valida } = normalizarPlaca(placaCruda);
  if (!valida) {
    await publicarEstado(
      "ERROR",
      `Formato de placa invalido: "${placaCruda}" (se espera LLLDDD, ej. GCT953).`
    );
    return;
  }

  const url = construirUrlConsultaGarantia(placa);
  await publicarEstado("ABRIENDO_RGM", `Abriendo ${url}`);

  const tab = await chrome.tabs.create({ url });

  const listener = (tabId, changeInfo) => {
    if (tabId === tab.id && changeInfo.status === "complete") {
      chrome.tabs.onUpdated.removeListener(listener);
      // Pequena espera adicional por si la pagina sigue cargando contenido
      // via JS/AJAX tras el evento "complete".
      setTimeout(() => {
        chrome.tabs.sendMessage(tab.id, { tipo: "ANALIZAR_PAGINA_RGM", placa });
      }, 500);
    }
  };
  chrome.tabs.onUpdated.addListener(listener);
}

// =========================================================================
// JUDICIAL v0.1 - independiente de RGM (funciones separadas, sin tocar
// nada de lo anterior). Abre una pestana temporal, la cierra automatica-
// mente al terminar (ver el listener de RESULTADO_FINAL mas arriba).
// =========================================================================

const URL_CONSULTA_JUDICIAL = "https://consultaprocesos.ramajudicial.gov.co/Procesos/NombreRazonSocial";

async function iniciarConsultaJudicial(nombreCrudo) {
  const nombre = (nombreCrudo || "").trim();

  await chrome.storage.local.set({ resultadoFinal: null });

  if (!nombre) {
    await chrome.storage.local.set({
      trabajoActual: {
        estado: "ERROR",
        detalle: "Nombre vacio: se requiere un nombre o razon social para consultar.",
        timestamp: new Date().toISOString(),
      },
    });
    return;
  }

  await chrome.storage.local.set({
    trabajoActual: {
      estado: "ABRIENDO_JUDICIAL",
      detalle: `Abriendo Consulta Judicial para "${nombre}"...`,
      timestamp: new Date().toISOString(),
    },
  });

  const tab = await chrome.tabs.create({ url: URL_CONSULTA_JUDICIAL });
  tabJudicialActual = tab.id;

  const listener = (tabId, changeInfo) => {
    if (tabId === tab.id && changeInfo.status === "complete") {
      chrome.tabs.onUpdated.removeListener(listener);
      // Espera mayor que RGM: esta pagina es una SPA de Vue que sigue
      // montando componentes despues del evento "complete" del documento
      // base (que solo tiene el shell vacio, ver informe de diagnostico).
      setTimeout(() => {
        chrome.tabs.sendMessage(tab.id, { tipo: "EJECUTAR_CONSULTA_JUDICIAL", nombre });
      }, 1200);
    }
  };
  chrome.tabs.onUpdated.addListener(listener);
}

// =========================================================================
// ORQUESTACION RGM -> JUDICIAL (correccion de esta tarea)
//
// Esta es la unica pieza nueva: coordina, llamando a iniciarConsulta() y
// iniciarConsultaJudicial() TAL CUAL existen (cero cambios en su interior,
// cero cambios en content.js ni judicial.js), el paso:
//
//   rgm.deudor_garante.nombre  ->  nombre_consulta  ->  campo Judicial
//
// Nunca usa la placa como texto de busqueda judicial. La placa se
// conserva unicamente como placa_origen para asociarla al resultado final.
// =========================================================================

function esperarProximoResultadoFinal() {
  return new Promise((resolve) => {
    resolverEsperaRgm = resolve;
  });
}

async function ejecutarFlujoRgmJudicial(placaCruda) {
  await chrome.storage.local.set({
    trabajoActual: { estado: "RGM_INICIANDO", detalle: `Ejecutando RGM para "${placaCruda}"...`, timestamp: new Date().toISOString() },
    resultadoFinal: null,
  });

  const esperaResultadoRgm = esperarProximoResultadoFinal();
  await iniciarConsulta(placaCruda); // funcion RGM EXISTENTE, sin modificar
  const resultadoRgm = await esperaResultadoRgm;

  const placaOrigen = resultadoRgm ? resultadoRgm.placa_consultada : placaCruda;
  const nombreCrudo = resultadoRgm && resultadoRgm.deudor_garante ? resultadoRgm.deudor_garante.nombre : null;
  // Normalizacion minima (espacios), NUNCA alteracion del contenido.
  const nombre = (nombreCrudo || "").replace(/\s+/g, " ").trim();

  console.log("[NEXORA-RGM]");
  console.log(`placa_consultada=${placaOrigen}`);
  console.log(`deudor_nombre=${nombreCrudo || "(vacio)"}`);

  const rgmOk = resultadoRgm && resultadoRgm.estado === "OK";

  if (!rgmOk || !nombre) {
    await chrome.storage.local.set({
      trabajoActual: {
        estado: "RGM_SIN_DEUDOR",
        detalle: !rgmOk
          ? `RGM no devolvio estado OK (estado real: ${resultadoRgm ? resultadoRgm.estado : "desconocido"}). Judicial NO se inicia.`
          : "RGM no devolvio un nombre de deudor/garante utilizable. Judicial NO se inicia.",
        timestamp: new Date().toISOString(),
      },
      resultadoFinal: resultadoRgm,
    });
    console.log("[NEXORA-JUDICIAL] no iniciado (RGM_SIN_DEUDOR)");
    return;
  }

  console.log("[NEXORA-JUDICIAL]");
  console.log(`placa_origen=${placaOrigen}`);
  console.log(`nombre_consulta=${nombre}`);

  await chrome.storage.local.set({
    trabajoActual: {
      estado: "JUDICIAL_INICIANDO",
      detalle: `Nombre obtenido de RGM: "${nombre}" (placa_origen=${placaOrigen})`,
      timestamp: new Date().toISOString(),
    },
  });

  placaOrigenPendiente = placaOrigen;
  await iniciarConsultaJudicial(nombre); // funcion Judicial EXISTENTE, sin modificar
}
