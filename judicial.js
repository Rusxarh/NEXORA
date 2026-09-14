/**
 * NEXORA CONNECTOR - JUDICIAL v0.1
 * judicial.js
 *
 * Content script INDEPENDIENTE de content.js (RGM). Cero codigo
 * compartido a proposito (seccion 26 de la especificacion: "Judicial NO
 * debe quedar acoplado innecesariamente al extractor RGM"). Matched
 * unicamente contra consultaprocesos.ramajudicial.gov.co (ver manifest.json).
 *
 * NIVEL DE CONFIANZA POR SECCION (para que quede explicito en el propio
 * codigo, no solo en el informe):
 *
 *   ALTA  - confirmado por diagnostico DOM real:
 *           - campo de nombre (label real + fallback documentado a
 *             "input-78")
 *           - boton "Consultar por nombre o razon social" (aria-label real)
 *           - tabla de Actuaciones (columnas reales confirmadas)
 *           - seleccion de "Natural" en "Tipo de Persona": confirmado por
 *             diagnostico DOM v1/v2/v3 como v-select de Vuetify (input
 *             readonly + [role=button][aria-haspopup=listbox][aria-owns] +
 *             lista que solo existe abierta); verificado via
 *             aria-activedescendant del input, nunca input.value
 *
 *   MEDIA - deteccion semantica razonada, sin selector fijo confirmado:
 *           - seleccion de "Todos los Procesos..." (por texto visible
 *             exacto que SI fue confirmado, pero el mecanismo del control
 *             -radio/checkbox/boton- no)
 *           - deteccion de radicados en el listado (por patron numerico de
 *             20-25 digitos, formato real de radicado colombiano
 *             confirmado en dos casos reales de esta conversacion)
 *           - control de "siguiente pagina" de actuaciones (por
 *             aria-label/texto "siguiente", cerca de la tabla, validando
 *             que el contenido realmente cambio)
 *
 *   BAJA  - mejor esfuerzo sin ninguna confirmacion DOM:
 *           - "Datos del Proceso" (etiquetas buscadas por texto, sin tabla
 *             ni contenedor confirmado)
 *           - mecanismo de apertura de un proceso individual (se asume
 *             navegacion en la MISMA pestana + history.back() para volver;
 *             si el sitio real abre pestana nueva por proceso, esto
 *             necesitara ajuste en una siguiente iteracion, igual que paso
 *             con RGM)
 *
 * No se hacen peticiones HTTP propias. No se intenta evadir CAPTCHA ni
 * ningun control de seguridad: si aparece, el flujo se detiene y reporta
 * INFORMACION_NO_DISPONIBLE o ERROR_CONSULTA, nunca intenta continuar.
 */

const NEXORA_LOG = (msg) => console.log(`[NEXORA-JUDICIAL] ${msg}`);

function enviarEstado(estado, detalle) {
  NEXORA_LOG(`Estado: ${estado}${detalle ? " - " + detalle : ""}`);
  chrome.runtime
    .sendMessage({ tipo: "ESTADO_ACTUALIZADO", estado, detalle: detalle || null, timestamp: new Date().toISOString() })
    .catch(() => {});
}

function enviarResultadoFinal(resultado) {
  chrome.runtime.sendMessage({ tipo: "RESULTADO_FINAL", resultado }).catch(() => {});
}

function normalizarTexto(s) {
  return (s || "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .trim();
}

function limpiarTexto(s) {
  return (s || "").replace(/\s+/g, " ").trim();
}

function buscarTextoAproximado(frasesPosibles) {
  const texto = normalizarTexto(document.body.innerText);
  return frasesPosibles.some((f) => texto.includes(normalizarTexto(f)));
}

// =========================================================================
// FORMULARIO (confianza ALTA: campo/boton confirmados; MEDIA: selectores
// de tipo persona/tipo de proceso, por texto exacto confirmado sin
// selector de control confirmado)
// =========================================================================

function encontrarBotonPorAriaLabel(ariaLabel) {
  return document.querySelector(`[aria-label="${ariaLabel}"]`);
}

/**
 * Preferido: localizar por el LABEL real "Nombre(s) Apellido o Razón
 * Social" (semantico, resistente a que Vuetify regenere ids). Respaldo
 * documentado: el id "input-78" observado en el diagnostico -- fragil
 * porque Vuetify genera esos ids secuencialmente y pueden cambiar.
 */
function encontrarCampoNombre() {
  const TEXTO_LABEL = "Nombre(s) Apellido o Razón Social";
  const labels = Array.from(document.querySelectorAll("label"));
  const labelEncontrado = labels.find((l) => normalizarTexto(l.textContent).includes(normalizarTexto(TEXTO_LABEL)));

  if (labelEncontrado) {
    if (labelEncontrado.htmlFor) {
      const porFor = document.getElementById(labelEncontrado.htmlFor);
      if (porFor) return porFor;
    }
    const contenedor = labelEncontrado.closest(".v-input, .v-text-field, div");
    const inputCercano = contenedor ? contenedor.querySelector("input") : null;
    if (inputCercano) return inputCercano;
  }

  return document.getElementById("input-78");
}

/**
 * Localiza el <input> REAL asociado a un elemento de texto (label/span).
 * CORRECCION: Vuetify (v2 y v3) coloca el <input type="radio"/"checkbox">
 * y su <label> visible como HERMANOS dentro de un contenedor comun
 * (.v-radio, .v-selection-control, .v-input), nunca como padre-hijo. Por
 * eso buscar el input DENTRO del label (como hacia la version anterior)
 * nunca lo encontraba. Aqui se sube por los contenedores comunes (hasta
 * 4 niveles) buscando el input hermano/primo real.
 */
function encontrarInputAsociado(elementoTexto) {
  if (elementoTexto.tagName === "INPUT") return elementoTexto;

  const dentro = elementoTexto.querySelector("input");
  if (dentro) return dentro;

  if (elementoTexto.htmlFor) {
    const porFor = document.getElementById(elementoTexto.htmlFor);
    if (porFor) return porFor;
  }

  let contenedor = elementoTexto.parentElement;
  for (let nivel = 0; nivel < 4 && contenedor; nivel++) {
    const input = contenedor.querySelector("input");
    if (input) return input;
    contenedor = contenedor.parentElement;
  }
  return null;
}

/**
 * Busca el elemento MAS ESPECIFICO cuyo texto visible coincide
 * exactamente (normalizado) con el texto dado. Si encuentra un input real
 * de radio/checkbox asociado, hace click() NATIVO directamente sobre ESE
 * input (dispara click/input/change en la secuencia que Vue escucha) y
 * VERIFICA que input.checked realmente quedo en true -- no basta con
 * "lo intente", se confirma que Vuetify registro el cambio. Si no se
 * encuentra un input real, cae al respaldo anterior (click sobre el
 * control clicable mas cercano) para no romper casos con otra estructura.
 */
function seleccionarOpcionPorTexto(textoExacto) {
  const normalizado = normalizarTexto(textoExacto);
  const candidatos = Array.from(
    document.querySelectorAll("label, span, div, button, [role='radio'], [role='button'], li")
  );
  let mejor = null;
  for (const el of candidatos) {
    if (normalizarTexto(el.textContent) === normalizado) {
      if (!mejor || el.textContent.length < mejor.textContent.length) mejor = el;
    }
  }
  if (!mejor) return { exito: false, motivo: "TEXTO_NO_ENCONTRADO" };

  const input = encontrarInputAsociado(mejor);
  if (input && (input.type === "radio" || input.type === "checkbox")) {
    if (!input.checked) input.click();
    return input.checked
      ? { exito: true, motivo: null }
      : { exito: false, motivo: "INPUT_NO_QUEDO_CHECKED" };
  }

  const controlRespaldo = mejor.closest("[role='radio'], [role='button'], [role='checkbox'], button, label") || mejor;
  controlRespaldo.click();
  return { exito: true, motivo: "RESPALDO_CLICK_GENERICO_SIN_INPUT" };
}

/**
 * "Tipo de Persona" NO es un radio (correccion tras diagnostico DOM real
 * v1/v2/v3): es un v-select de Vuetify. Evidencia confirmada:
 *   - input#input-72 (readonly=true) -- su .value NUNCA cambia a "Natural",
 *     por eso NO se usa input.value como criterio de exito.
 *   - contenedor real: [role="button"][aria-haspopup="listbox"] con
 *     aria-owns apuntando a una lista que SOLO existe en el DOM mientras el
 *     desplegable esta abierto (las opciones "Natural"/"Juridica" no
 *     existen antes de abrirlo).
 *   - tras seleccionar una opcion, Vuetify marca en el INPUT el atributo
 *     aria-activedescendant con el id de la opcion elegida (observado real:
 *     list-item-121-0) -- ese es el indicador real de exito. El id exacto
 *     no se asume fijo entre sesiones: se lee dinamicamente de la opcion
 *     encontrada en ESTA sesion, nunca hardcodeado.
 */
function localizarComponenteTipoPersona() {
  let input = null;
  const labels = Array.from(document.querySelectorAll("label"));
  const labelEncontrado = labels.find((l) =>
    normalizarTexto(l.textContent).includes(normalizarTexto("Tipo de Persona"))
  );
  if (labelEncontrado && labelEncontrado.htmlFor) {
    input = document.getElementById(labelEncontrado.htmlFor);
  }
  if (!input) input = document.getElementById("input-72"); // respaldo observado, no fijo
  if (!input) return null;

  const contenedorSelect = input.closest(".v-select") || input.closest(".v-input");
  const boton =
    (contenedorSelect && contenedorSelect.querySelector("[role='button'][aria-haspopup='listbox']")) ||
    input.closest("[role='button']") ||
    (input.parentElement ? input.parentElement.querySelector("[role='button']") : null);

  if (!boton) return null;
  return { input, boton };
}

/**
 * Abre el v-select (click sobre su contenedor real [role=button]) y espera
 * -- via MutationObserver + polling, maximo 5s -- a que el elemento
 * referenciado por aria-owns exista con contenido. Si ya estaba abierto
 * (aria-owns ya resuelve a una lista con hijos), no vuelve a hacer click.
 */
function abrirYObtenerOpcionesTipoPersona(boton, callback) {
  const TIMEOUT_MS = 5000;
  const ariaOwns = boton.getAttribute("aria-owns");
  if (!ariaOwns) {
    callback(null);
    return;
  }

  const listaConContenido = () => {
    const lista = document.getElementById(ariaOwns);
    return lista && lista.children.length > 0 ? lista : null;
  };

  const yaAbierta = listaConContenido();
  if (yaAbierta) {
    callback(yaAbierta);
    return;
  }

  boton.click();

  let resuelto = false;
  const intentar = () => {
    if (resuelto) return true;
    const lista = listaConContenido();
    if (lista) {
      resuelto = true;
      callback(lista);
      return true;
    }
    return false;
  };

  if (intentar()) return;

  const observer = new MutationObserver(() => {
    if (intentar()) {
      observer.disconnect();
      clearInterval(intervalo);
    }
  });
  observer.observe(document.body, { childList: true, subtree: true });

  const intervalo = setInterval(() => {
    if (intentar()) {
      observer.disconnect();
      clearInterval(intervalo);
    }
  }, 200);

  setTimeout(() => {
    if (resuelto) return;
    resuelto = true;
    observer.disconnect();
    clearInterval(intervalo);
    callback(null);
  }, TIMEOUT_MS);
}

function encontrarOpcionDeListaPorTexto(lista, textoExacto) {
  const normalizado = normalizarTexto(textoExacto);
  const opciones = Array.from(lista.querySelectorAll("[role='option']"));
  const candidatas =
    opciones.length > 0 ? opciones : Array.from(lista.querySelectorAll("li, div")).filter((el) => el.textContent.trim());
  return candidatas.find((el) => normalizarTexto(el.textContent) === normalizado) || null;
}

/**
 * Selecciona "Natural" en el v-select "Tipo de Persona" y VERIFICA el
 * resultado real (aria-activedescendant del input apuntando al id de la
 * opcion elegida, o aria-selected="true" en la propia opcion como
 * respaldo) -- nunca input.value, que permanece vacio segun evidencia real.
 */
function seleccionarTipoPersonaNatural(callback) {
  const componente = localizarComponenteTipoPersona();
  if (!componente) {
    callback({ exito: false, motivo: "ERROR_TIPO_PERSONA_NO_ENCONTRADO" });
    return;
  }

  abrirYObtenerOpcionesTipoPersona(componente.boton, (lista) => {
    if (!lista) {
      callback({ exito: false, motivo: "ERROR_TIPO_PERSONA_NO_ABIERTO" });
      return;
    }

    const opcionNatural = encontrarOpcionDeListaPorTexto(lista, "Natural");
    if (!opcionNatural) {
      callback({ exito: false, motivo: "ERROR_OPCION_NATURAL_NO_ENCONTRADA" });
      return;
    }

    const idOpcion = opcionNatural.id || null;
    opcionNatural.click();

    let verificaciones = 0;
    const verificar = () => {
      verificaciones++;
      const activo = componente.input.getAttribute("aria-activedescendant");
      const confirmado = (idOpcion && activo === idOpcion) || opcionNatural.getAttribute("aria-selected") === "true";

      if (confirmado) {
        callback({ exito: true, motivo: null });
        return;
      }
      if (verificaciones > 10) {
        callback({ exito: false, motivo: "TIPO_PERSONA_NO_SELECCIONADO" });
        return;
      }
      setTimeout(verificar, 200);
    };
    setTimeout(verificar, 200);
  });
}

/**
 * Vuelve a localizar el boton "Consultar por nombre o razon social" JUSTO
 * ANTES del click, en vez de reutilizar la referencia capturada al inicio
 * de ejecutarConsultaJudicial (que puede quedar obsoleta -stale- si Vue
 * re-renderiza el boton, o seguir con disabled/aria-disabled="true" si la
 * validacion reactiva del v-form aun no proceso el nombre recien escrito).
 * Usa polling corto (150ms), con timeout de 5s -- nunca esperas fijas
 * largas -- hasta confirmar que el boton esta: presente, isConnected,
 * disabled !== true y aria-disabled !== "true".
 */
function esperarBotonConsultarInteractuable(callback) {
  const TIMEOUT_MS = 5000;
  const INTERVALO_MS = 150;
  let resuelto = false;
  let intentos = 0;

  const revisar = () => {
    if (resuelto) return;
    intentos++;

    const boton = encontrarBotonPorAriaLabel("Consultar por nombre o razón social");
    const diagnostico = boton
      ? {
          encontrado: true,
          isConnected: boton.isConnected,
          disabled: boton.disabled === true,
          ariaDisabled: boton.getAttribute("aria-disabled"),
          className: boton.className,
          ariaLabel: boton.getAttribute("aria-label"),
        }
      : { encontrado: false };

    const interactuable =
      !!boton && boton.isConnected === true && boton.disabled !== true && boton.getAttribute("aria-disabled") !== "true";

    if (interactuable) {
      resuelto = true;
      NEXORA_LOG(`boton_consultar diagnostico: ${JSON.stringify(diagnostico)}`);
      callback(boton, null);
      return;
    }

    if (intentos * INTERVALO_MS >= TIMEOUT_MS) {
      resuelto = true;
      NEXORA_LOG(`boton_consultar diagnostico (timeout): ${JSON.stringify(diagnostico)}`);
      callback(null, "ERROR_BOTON_CONSULTAR_NO_INTERACTUABLE");
      return;
    }

    setTimeout(revisar, INTERVALO_MS);
  };

  revisar();
}

function ejecutarConsultaJudicial(nombre) {
  enviarEstado("VALIDANDO_FORMULARIO", "Localizando formulario de Consulta por Nombre o Razón Social...");

  const campoNombre = encontrarCampoNombre();
  const botonConsultar = encontrarBotonPorAriaLabel("Consultar por nombre o razón social");

  if (!campoNombre || !botonConsultar) {
    capturarSnapshotDesconocido(nombre, "FORMULARIO_NO_ENCONTRADO", {
      campoNombreEncontrado: !!campoNombre,
      botonConsultarEncontrado: !!botonConsultar,
    });
    return;
  }

  seleccionarTipoPersonaNatural((resultadoNatural) => {
    NEXORA_LOG(`tipo_persona seleccion: exito=${resultadoNatural.exito} motivo=${resultadoNatural.motivo || "ok"}`);

    if (!resultadoNatural.exito) {
      // No se confirma "Natural" realmente seleccionado: detenerse aqui,
      // NO pulsar CONSULTAR (fallaria de todos modos por validacion).
      capturarSnapshotDesconocido(nombre, resultadoNatural.motivo, resultadoNatural);
      return;
    }
    NEXORA_LOG("tipo_persona=NATURAL");

    const resultadoTodosProcesos = seleccionarOpcionPorTexto("Todos los Procesos (consulta completa, menos rápida)");
    NEXORA_LOG(
      `tipo_consulta seleccion: exito=${resultadoTodosProcesos.exito} motivo=${resultadoTodosProcesos.motivo || "ok"}`
    );

    setTimeout(() => {
      campoNombre.focus();
      campoNombre.value = nombre.toUpperCase().trim();
      campoNombre.dispatchEvent(new Event("input", { bubbles: true }));
      campoNombre.dispatchEvent(new Event("change", { bubbles: true }));

      enviarEstado("CONSULTANDO", `Ejecutando consulta para "${nombre}"...`);

      esperarBotonConsultarInteractuable((botonConsultarActual, motivoError) => {
        if (!botonConsultarActual) {
          capturarSnapshotDesconocido(nombre, motivoError, { paso: "CLICK_CONSULTAR" });
          return;
        }

        botonConsultarActual.click();
        observarListadoResultados(nombre);
      });
    }, 500);
  });
}

// =========================================================================
// LISTADO DE RESULTADOS (confianza MEDIA: deteccion por patron de
// radicado, formato real confirmado: 20-25 digitos)
// =========================================================================

const PATRON_RADICADO = /^\d{20,25}$/;

function encontrarCeldasConRadicado() {
  const elementos = Array.from(document.querySelectorAll("td, span, div, a"));
  const vistos = new Set();
  const resultado = [];
  for (const el of elementos) {
    const textoDirecto = limpiarTexto(
      Array.from(el.childNodes)
        .filter((n) => n.nodeType === Node.TEXT_NODE)
        .map((n) => n.textContent)
        .join("")
    );
    if (PATRON_RADICADO.test(textoDirecto) && !vistos.has(textoDirecto)) {
      vistos.add(textoDirecto);
      resultado.push({ radicado: textoDirecto, elemento: el });
    }
  }
  return resultado;
}

function observarListadoResultados(nombre) {
  const TIMEOUT_MS = 20000;
  let resuelto = false;

  const intentar = () => {
    if (resuelto) return true;

    if (buscarTextoAproximado(["no se encontraron", "sin resultados", "no existen procesos", "no hay resultados"])) {
      resuelto = true;
      enviarEstado("SIN_RESULTADOS", "La Rama Judicial no reporto procesos para este nombre.");
      enviarResultadoFinal({ fuente: "RAMA_JUDICIAL", nombre_consultado: nombre, estado_consulta: "SIN_RESULTADOS", procesos: [] });
      return true;
    }

    const radicados = encontrarCeldasConRadicado();
    if (radicados.length > 0) {
      resuelto = true;
      enviarEstado("RESULTADO_DETECTADO", `${radicados.length} radicado(s) detectado(s) en el listado.`);
      iniciarProcesamientoDeProcesos(nombre, radicados.map((r) => r.radicado));
      return true;
    }
    return false;
  };

  if (intentar()) return;

  const observer = new MutationObserver(() => {
    if (intentar()) observer.disconnect();
  });
  observer.observe(document.body, { childList: true, subtree: true });

  setTimeout(() => {
    if (resuelto) return;
    resuelto = true;
    observer.disconnect();
    capturarSnapshotDesconocido(nombre, "LISTADO_TIMEOUT");
  }, TIMEOUT_MS);
}

// =========================================================================
// PROCESAMIENTO SECUENCIAL DE CADA PROCESO (cola persistida en
// chrome.storage.local, para sobrevivir a navegaciones completas de pagina)
// =========================================================================

function iniciarProcesamientoDeProcesos(nombre, radicados) {
  chrome.storage.local.set(
    {
      nexoraJudicialTrabajo: {
        nombre,
        radicadosPendientes: radicados,
        radicadosProcesados: [],
        procesosExtraidos: [],
        timestamp: Date.now(),
      },
    },
    () => continuarConSiguienteProceso()
  );
}

function continuarConSiguienteProceso() {
  chrome.storage.local.get("nexoraJudicialTrabajo", (datos) => {
    const trabajo = datos.nexoraJudicialTrabajo;
    if (!trabajo) return;

    if (trabajo.radicadosPendientes.length === 0) {
      finalizarConsultaJudicial(trabajo);
      return;
    }

    const radicadoActual = trabajo.radicadosPendientes[0];
    const candidatos = encontrarCeldasConRadicado();
    const candidato = candidatos.find((c) => c.radicado === radicadoActual);

    if (!candidato) {
      // No estamos (todavia) en la pagina de listado -- probablemente
      // seguimos en el detalle del proceso anterior. Intentar volver.
      enviarEstado("VOLVIENDO_AL_LISTADO", `Regresando para procesar el radicado ${radicadoActual}...`);
      history.back();
      setTimeout(() => continuarConSiguienteProceso(), 1500);
      return;
    }

    const controlClicable = candidato.elemento.closest("a, button, [role='button']") || candidato.elemento;
    enviarEstado("ABRIENDO_PROCESO", `Abriendo proceso ${radicadoActual}...`);
    controlClicable.click();
    observarDetalleProceso(radicadoActual);
  });
}

// =========================================================================
// DETALLE DEL PROCESO (confianza BAJA para Datos del Proceso; ALTA para
// Actuaciones)
// =========================================================================

function buscarValorPorEtiquetaGenerico(etiquetaBuscada) {
  const normalizado = normalizarTexto(etiquetaBuscada).replace(/:$/, "");
  const elementos = Array.from(document.querySelectorAll("td, th, div, span, dt, label"));
  for (const el of elementos) {
    const texto = normalizarTexto(el.textContent).replace(/:$/, "");
    if (texto !== normalizado) continue;

    const siguiente = el.nextElementSibling;
    if (siguiente) {
      const valor = limpiarTexto(siguiente.textContent);
      if (valor) return valor;
    }
    const contenedor = el.parentElement;
    const siguienteContenedor = contenedor ? contenedor.nextElementSibling : null;
    if (siguienteContenedor) {
      const valor = limpiarTexto(siguienteContenedor.textContent);
      if (valor) return valor;
    }
  }
  return null;
}

function extraerDatosDelProceso() {
  return {
    radicado: buscarValorPorEtiquetaGenerico("Radicación") || buscarValorPorEtiquetaGenerico("Número de Radicado") || null,
    fecha_radicacion: buscarValorPorEtiquetaGenerico("Fecha de Radicación"),
    despacho: buscarValorPorEtiquetaGenerico("Despacho"),
    tipo_proceso: buscarValorPorEtiquetaGenerico("Tipo de Proceso"),
  };
}

const ENCABEZADOS_ACTUACIONES = [
  "fecha de actuacion",
  "actuacion",
  "anotacion",
  "fecha inicia termino",
  "fecha finaliza termino",
  "fecha de registro",
];

function encontrarTablaActuaciones() {
  const tablas = Array.from(document.querySelectorAll("table"));
  let mejor = null;
  let mejorPuntaje = 0;
  for (const tabla of tablas) {
    const encabezados = Array.from(tabla.querySelectorAll("th, tr:first-child td")).map((c) => normalizarTexto(c.textContent));
    let puntaje = 0;
    for (const esperado of ENCABEZADOS_ACTUACIONES) {
      if (encabezados.some((t) => t.includes(esperado))) puntaje++;
    }
    if (puntaje > mejorPuntaje) {
      mejorPuntaje = puntaje;
      mejor = tabla;
    }
  }
  return mejorPuntaje >= 3 ? mejor : null;
}

function indiceColumnaActuacion(tabla, nombres) {
  const encabezados = Array.from(tabla.querySelectorAll("th, tr:first-child td")).map((c) => normalizarTexto(c.textContent));
  for (const nombre of nombres) {
    const idx = encabezados.findIndex((t) => t.includes(normalizarTexto(nombre)));
    if (idx !== -1) return idx;
  }
  return -1;
}

function extraerActuacionesDeTabla(tabla) {
  const indices = {
    fecha_actuacion: indiceColumnaActuacion(tabla, ["fecha de actuacion"]),
    actuacion: indiceColumnaActuacion(tabla, ["actuacion"]),
    anotacion: indiceColumnaActuacion(tabla, ["anotacion"]),
    fecha_inicia_termino: indiceColumnaActuacion(tabla, ["fecha inicia termino"]),
    fecha_finaliza_termino: indiceColumnaActuacion(tabla, ["fecha finaliza termino"]),
    fecha_registro: indiceColumnaActuacion(tabla, ["fecha de registro"]),
  };
  return Array.from(tabla.querySelectorAll("tr"))
    .slice(1)
    .map((fila) => {
      const celdas = fila.children;
      const registro = {};
      for (const [campo, idx] of Object.entries(indices)) {
        registro[campo] = idx !== -1 && celdas[idx] ? limpiarTexto(celdas[idx].textContent) || null : null;
      }
      return registro;
    })
    .filter((r) => Object.values(r).some((v) => v));
}

/**
 * Control de "siguiente pagina": buscado CERCA de la tabla (su
 * contenedor), nunca en toda la pagina, para no confundirlo con otro
 * boton generico. Se descarta si esta deshabilitado.
 */
function encontrarControlSiguientePagina(tabla) {
  const zona = tabla.closest("div") || tabla.parentElement || document;
  const candidatos = Array.from(zona.querySelectorAll("button, a, [role='button']"));
  return (
    candidatos.find((el) => {
      const etiqueta = normalizarTexto(el.getAttribute("aria-label") || el.textContent || "");
      const deshabilitado = el.disabled || el.getAttribute("aria-disabled") === "true";
      return etiqueta.includes("siguiente") && !deshabilitado;
    }) || null
  );
}

/**
 * Recolecta TODAS las actuaciones recorriendo paginacion real: tras cada
 * clic en "siguiente", valida que la primera fila de la tabla realmente
 * cambio antes de continuar (evita falsos positivos de paginacion y
 * bucles infinitos). Deduplica por combinacion fecha_actuacion+actuacion+
 * fecha_registro.
 */
function recolectarTodasLasActuaciones(callback, acumulado = []) {
  const tabla = encontrarTablaActuaciones();
  if (!tabla) {
    callback(acumulado, acumulado.length === 0 ? "TABLA_ACTUACIONES_NO_ENCONTRADA" : null);
    return;
  }

  const nuevasFilas = extraerActuacionesDeTabla(tabla);
  const firmaAntes = JSON.stringify(nuevasFilas[0] || null);

  const clave = (a) => `${a.fecha_actuacion}|${a.actuacion}|${a.fecha_registro}`;
  const clavesExistentes = new Set(acumulado.map(clave));
  const filasNuevas = nuevasFilas.filter((f) => !clavesExistentes.has(clave(f)));
  const totalAcumulado = acumulado.concat(filasNuevas);

  const controlSiguiente = encontrarControlSiguientePagina(tabla);
  if (!controlSiguiente) {
    callback(totalAcumulado, null);
    return;
  }

  controlSiguiente.click();

  let verificaciones = 0;
  const verificar = () => {
    verificaciones++;
    const tablaNueva = encontrarTablaActuaciones();
    const primeraFilaNueva = tablaNueva ? extraerActuacionesDeTabla(tablaNueva)[0] : null;
    const firmaAhora = JSON.stringify(primeraFilaNueva || null);

    if (firmaAhora !== firmaAntes) {
      recolectarTodasLasActuaciones(callback, totalAcumulado);
      return;
    }
    if (verificaciones > 10) {
      callback(totalAcumulado, null); // se asume ultima pagina real
      return;
    }
    setTimeout(verificar, 400);
  };
  setTimeout(verificar, 400);
}

// =========================================================================
// ANALISIS DE ACTUACIONES RELEVANTES (nunca una decision juridica: solo
// marca coincidencias de texto, conservando TODA la evidencia)
// =========================================================================

const PALABRAS_TERMINACION = ["terminacion", "archivo", "desistimiento", "sentencia", "transaccion"];
const PALABRAS_LEVANTAMIENTO = ["levantamiento"];

function clasificarProcesoPorActuaciones(actuaciones) {
  if (!actuaciones || actuaciones.length === 0) return { estado: "SIN_INFORMACION", relevantes: [] };

  const relevantes = [];
  let hayTerminacion = false;
  let hayLevantamiento = false;

  for (const a of actuaciones) {
    const texto = normalizarTexto(`${a.actuacion || ""} ${a.anotacion || ""}`);
    const esTerminacion = PALABRAS_TERMINACION.some((p) => texto.includes(p));
    const esLevantamiento = PALABRAS_LEVANTAMIENTO.some((p) => texto.includes(p));
    if (esTerminacion || esLevantamiento) {
      relevantes.push({ fecha_actuacion: a.fecha_actuacion, actuacion: a.actuacion, anotacion: a.anotacion });
      if (esTerminacion) hayTerminacion = true;
      if (esLevantamiento) hayLevantamiento = true;
    }
  }

  let estado = "ACTIVO";
  if (hayTerminacion) estado = "TERMINADO";
  else if (hayLevantamiento) estado = "LEVANTAMIENTO_MEDIDAS";

  return { estado, relevantes };
}

function observarDetalleProceso(radicadoEsperado) {
  const TIMEOUT_MS = 20000;
  let resuelto = false;

  const intentar = () => {
    if (resuelto) return true;
    if (!document.body.innerText.includes(radicadoEsperado)) return false;

    resuelto = true;
    enviarEstado("EXTRAYENDO_PROCESO", `Extrayendo datos del proceso ${radicadoEsperado}...`);

    const datosProceso = extraerDatosDelProceso();
    recolectarTodasLasActuaciones((actuaciones, motivoError) => {
      const clasificacion = clasificarProcesoPorActuaciones(actuaciones);
      const proceso = {
        datos_proceso: datosProceso,
        estado_proceso: clasificacion.estado,
        actuaciones_relevantes: clasificacion.relevantes,
        actuaciones,
      };
      if (motivoError) proceso._diagnostico = { motivo: motivoError };
      guardarProcesoExtraido(radicadoEsperado, proceso);
    });
    return true;
  };

  if (intentar()) return;
  const observer = new MutationObserver(() => {
    if (intentar()) observer.disconnect();
  });
  observer.observe(document.body, { childList: true, subtree: true });
  setTimeout(() => {
    if (resuelto) return;
    resuelto = true;
    observer.disconnect();
    guardarProcesoExtraido(radicadoEsperado, {
      datos_proceso: { radicado: radicadoEsperado, fecha_radicacion: null, despacho: null, tipo_proceso: null },
      estado_proceso: "REQUIERE_REVISION",
      actuaciones_relevantes: [],
      actuaciones: [],
      _diagnostico: { motivo: "DETALLE_TIMEOUT" },
    });
  }, TIMEOUT_MS);
}

function guardarProcesoExtraido(radicado, procesoExtraido) {
  chrome.storage.local.get("nexoraJudicialTrabajo", (datos) => {
    const trabajo = datos.nexoraJudicialTrabajo;
    if (!trabajo) return;

    trabajo.radicadosPendientes = trabajo.radicadosPendientes.filter((r) => r !== radicado);
    trabajo.radicadosProcesados.push(radicado);
    trabajo.procesosExtraidos.push(procesoExtraido);

    chrome.storage.local.set({ nexoraJudicialTrabajo: trabajo }, () => {
      if (trabajo.radicadosPendientes.length > 0) {
        enviarEstado("VOLVIENDO_AL_LISTADO", "Regresando al listado para el siguiente proceso...");
        history.back();
        setTimeout(() => continuarConSiguienteProceso(), 1500);
      } else {
        finalizarConsultaJudicial(trabajo);
      }
    });
  });
}

function finalizarConsultaJudicial(trabajo) {
  enviarEstado("EXTRACCION_COMPLETADA", `${trabajo.procesosExtraidos.length} proceso(s) procesado(s).`);
  chrome.storage.local.set({ nexoraJudicialTrabajo: null });
  enviarResultadoFinal({
    fuente: "RAMA_JUDICIAL",
    nombre_consultado: trabajo.nombre,
    estado_consulta: "OK",
    procesos: trabajo.procesosExtraidos,
  });
}

// =========================================================================
// DIAGNOSTICO Y ERRORES
// =========================================================================

function capturarSnapshotDesconocido(nombre, motivo, extra) {
  enviarEstado("ERROR_CONSULTA", `Motivo: ${motivo}. Se guardo un snapshot de diagnostico.`);
  chrome.storage.local.set({
    ultimoSnapshotJudicialDesconocido: {
      nombre,
      motivo,
      extra: extra || null,
      url: location.href,
      titulo: document.title,
      timestamp: new Date().toISOString(),
    },
    nexoraJudicialTrabajo: null,
  });
  enviarResultadoFinal({
    fuente: "RAMA_JUDICIAL",
    nombre_consultado: nombre,
    estado_consulta: "ERROR_CONSULTA",
    motivo,
    procesos: [],
    _nota: "Revisa 'ultimoSnapshotJudicialDesconocido' en chrome.storage.local para disenar la correccion.",
  });
}

// =========================================================================
// PUNTO DE ENTRADA
// =========================================================================

chrome.runtime.onMessage.addListener((mensaje, sender, sendResponse) => {
  if (mensaje.tipo === "EJECUTAR_CONSULTA_JUDICIAL") {
    NEXORA_LOG(`Connector Judicial iniciado. Nombre: ${mensaje.nombre}`);
    ejecutarConsultaJudicial(mensaje.nombre);
    sendResponse({ ok: true });
  }
  return true;
});

// Si esta pagina se cargo por una navegacion completa en medio de un
// trabajo Judicial en curso (radicados pendientes), retomamos donde
// quedamos en vez de tratarla como una consulta nueva.
(function reanudarTrabajoJudicialSiExiste() {
  chrome.storage.local.get("nexoraJudicialTrabajo", (datos) => {
    const trabajo = datos.nexoraJudicialTrabajo;
    if (!trabajo) return;
    const antiguedadMs = Date.now() - (trabajo.timestamp || 0);
    if (antiguedadMs > 120000) {
      chrome.storage.local.set({ nexoraJudicialTrabajo: null });
      return;
    }
    NEXORA_LOG("Reanudando trabajo Judicial en curso tras navegacion completa...");
    setTimeout(() => continuarConSiguienteProceso(), 800);
  });
})();

NEXORA_LOG("Content script Judicial cargado en " + location.href);
