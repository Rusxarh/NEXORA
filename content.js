/**
 * NEXORA CONNECTOR - RGM v0.1
 * content.js
 *
 * Flujo directo por URL (ConsultaGarantia.aspx?NumeroBien=...) + deteccion
 * del boton real "Consultar detalle garantia" en la tabla de resultados +
 * seguimiento hasta la pagina de detalle real (ResultadoConsultaGarantias.aspx
 * u otra, la que sea que el propio RGM decida).
 *
 * REGLA RESPETADA: nunca se construye la URL de detalle manualmente. Se
 * localiza el control real en el DOM, se lee su destino tal cual existe
 * (href real, o se identifica que es un mecanismo de postback/onclick sin
 * intentar reproducirlo), se hace clic real sobre el, y se observa a donde
 * navega realmente el navegador. Ese es el "destino real".
 *
 * LIMITE HONESTO: la estructura EXACTA de la pagina de detalle (que campos,
 * en que elementos) sigue sin verificarse. Esta iteracion se detiene al
 * llegar ahi: confirma la llegada, valida la placa, y captura un snapshot.
 * No extrae los 11 campos finales todavia.
 */

const NEXORA_LOG = (msg) => console.log(`[NEXORA] ${msg}`);

function enviarEstado(estado, detalle) {
  NEXORA_LOG(`Estado: ${estado}${detalle ? " - " + detalle : ""}`);
  chrome.runtime
    .sendMessage({
      tipo: "ESTADO_ACTUALIZADO",
      estado,
      detalle: detalle || null,
      timestamp: new Date().toISOString(),
    })
    .catch(() => {
      // El popup puede estar cerrado; no es un fallo real del connector.
    });
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

function buscarTextoAproximado(frasesPosibles) {
  const texto = normalizarTexto(document.body.innerText);
  return frasesPosibles.some((f) => texto.includes(normalizarTexto(f)));
}

// --- Interceptor de alert() (solo observabilidad, NUNCA bypass) --------
(function instalarInterceptorAlert() {
  const alertaOriginal = window.alert;
  window.alert = function (mensaje) {
    NEXORA_LOG(`alert() de la pagina interceptado (solo lectura): "${mensaje}"`);
    enviarEstado("ERROR", `La pagina mostro un aviso: "${mensaje}"`);
    return alertaOriginal.call(window, mensaje);
  };
})();

// =========================================================================
// DETECCION DE LA TABLA DE RESULTADOS (deteccion por CONTENIDO, verificado)
// =========================================================================

const ENCABEZADOS_ESPERADOS = [
  "folio electronico",
  "acreedor",
  "garante",
  "deudor",
  "numero de identificacion",
  "fecha de inscripcion",
  "ultima operacion",
  "acciones",
];

function encontrarTablaResultados() {
  const tablas = Array.from(document.querySelectorAll("table"));
  let mejor = null;
  let mejorPuntaje = 0;

  for (const tabla of tablas) {
    const celdasEncabezado = Array.from(tabla.querySelectorAll("th, tr:first-child td"));
    const textos = celdasEncabezado.map((c) => normalizarTexto(c.textContent));
    let puntaje = 0;
    for (const esperado of ENCABEZADOS_ESPERADOS) {
      if (textos.some((t) => t.includes(esperado))) puntaje++;
    }
    if (puntaje > mejorPuntaje) {
      mejorPuntaje = puntaje;
      mejor = tabla;
    }
  }
  return mejorPuntaje >= 3 ? mejor : null;
}

function indiceColumnaAcciones(tabla) {
  const encabezados = Array.from(tabla.querySelectorAll("th, tr:first-child td"));
  return encabezados.findIndex((c) => normalizarTexto(c.textContent).includes("acciones"));
}

function filasDeDatos(tabla) {
  return Array.from(tabla.querySelectorAll("tr")).slice(1);
}

// =========================================================================
// IDENTIFICACION DEL CONTROL "Consultar detalle garantia"
// No se asume que sea <a>: se buscan enlaces, botones, imagenes, controles
// ASP.NET, o cualquier elemento con onclick.
// =========================================================================

function candidatosAccion(celdaAcciones) {
  return Array.from(
    celdaAcciones.querySelectorAll(
      'a, button, img, i, span, input[type="button"], input[type="image"], input[type="submit"], [onclick]'
    )
  );
}

/**
 * Estrategia 1 (preferida): coincidencia semantica por title/aria-label/alt
 * que mencione "detalle".
 * Estrategia 2 (respaldo, confirmada por inspeccion manual real): el PRIMER
 * control de los disponibles en la celda de Acciones. Se usa solo si la
 * estrategia semantica no encuentra exactamente un candidato.
 */
function identificarControlDetalle(celdaAcciones) {
  const candidatos = candidatosAccion(celdaAcciones);
  if (candidatos.length === 0) {
    return { control: null, estrategia: "sin_candidatos", candidatos };
  }

  const semanticos = candidatos.filter((el) => {
    const etiqueta = normalizarTexto(
      el.getAttribute("title") || el.getAttribute("aria-label") || el.getAttribute("alt") || ""
    );
    return etiqueta.includes("detalle");
  });

  if (semanticos.length === 1) {
    return { control: semanticos[0], estrategia: "semantica_title_aria_alt", candidatos };
  }

  // Respaldo: el primer icono, segun confirmacion manual del usuario.
  return { control: candidatos[0], estrategia: "posicional_primer_icono", candidatos };
}

/**
 * Lee el destino real del control TAL CUAL existe en el DOM, sin construir
 * ni calcular nada. Si es un enlace real, se devuelve la URL absoluta. Si
 * usa javascript:/onclick (tipico de controles ASP.NET con __doPostBack),
 * se identifica y se registra ese mecanismo como texto, sin intentar
 * reproducirlo como peticion HTTP.
 */
function describirDestino(el) {
  const href = el.getAttribute && el.getAttribute("href");
  if (href && href.trim() && !href.trim().toLowerCase().startsWith("javascript:")) {
    try {
      return { tipo: "href_directo", valor: new URL(href, location.href).toString() };
    } catch (e) {
      return { tipo: "href_no_resoluble", valor: href };
    }
  }
  if (href && href.trim().toLowerCase().startsWith("javascript:")) {
    return { tipo: "javascript_uri", valor: href };
  }
  const onclick = el.getAttribute && el.getAttribute("onclick");
  if (onclick) {
    return { tipo: "onclick_postback", valor: onclick };
  }
  const padreEnlace = el.closest && el.closest("a[href]");
  if (padreEnlace) {
    return describirDestino(padreEnlace);
  }
  return { tipo: "desconocido", valor: null };
}

// =========================================================================
// FLUJO PRINCIPAL: ConsultaGarantia.aspx -> tabla -> boton -> navegacion
// =========================================================================

function manejarAnalizarPaginaRGM(placa) {
  enviarEstado("CONSULTANDO", "Analizando la pagina de ConsultaGarantia.aspx...");

  const TIMEOUT_MS = 15000;
  let resuelto = false;

  const intentarDetectar = () => {
    if (resuelto) return true;

    if (
      buscarTextoAproximado(["no se encontraron", "sin resultados", "no existen registros", "no se encontro"])
    ) {
      resuelto = true;
      enviarEstado("SIN_RESULTADO", "El RGM no reporto garantias para esta placa.");
      enviarResultadoFinal({ fuente: "RGM", placa_consultada: placa, estado: "SIN_RESULTADO", garantias: [] });
      return true;
    }

    const tabla = encontrarTablaResultados();
    if (tabla) {
      resuelto = true;
      NEXORA_LOG("RESULTADO_DETECTADO");
      NEXORA_LOG(`PLACA_CONSULTADA: ${placa}`);
      enviarEstado("RESULTADO_DETECTADO", "Tabla de resultados detectada (columnas coinciden con lo esperado).");
      procesarTablaResultados(tabla, placa);
      return true;
    }
    return false;
  };

  if (intentarDetectar()) return;

  const observer = new MutationObserver(() => {
    if (intentarDetectar()) observer.disconnect();
  });
  observer.observe(document.body, { childList: true, subtree: true });

  setTimeout(() => {
    if (resuelto) return;
    resuelto = true;
    observer.disconnect();
    NEXORA_LOG("Timeout esperando la tabla de resultados.");
    capturarSnapshotDesconocido(placa, "RESULTADOS_TIMEOUT");
  }, TIMEOUT_MS);
}

function procesarTablaResultados(tabla, placa) {
  const filas = filasDeDatos(tabla);
  if (filas.length === 0) {
    enviarEstado("SIN_RESULTADO", "La tabla de resultados no tiene filas de datos.");
    enviarResultadoFinal({ fuente: "RGM", placa_consultada: placa, estado: "SIN_RESULTADO", garantias: [] });
    return;
  }

  NEXORA_LOG(
    `Se detectaron ${filas.length} fila(s) de resultado. v0.1 procesa solo la primera (varias garantias se disenaran mas adelante).`
  );

  const fila = filas[0];
  const idxAcciones = indiceColumnaAcciones(tabla);
  if (idxAcciones === -1) {
    capturarSnapshotDesconocido(placa, "SIN_COLUMNA_ACCIONES", tabla.outerHTML.slice(0, 20000));
    return;
  }

  const celdaAcciones = fila.children[idxAcciones];
  if (!celdaAcciones) {
    capturarSnapshotDesconocido(placa, "FILA_SIN_CELDA_ACCIONES", fila.outerHTML);
    return;
  }

  const { control, estrategia, candidatos } = identificarControlDetalle(celdaAcciones);

  if (!control) {
    capturarSnapshotDesconocido(
      placa,
      "ACCION_DETALLE_NO_IDENTIFICADA",
      JSON.stringify({ celdaAccionesHTML: celdaAcciones.outerHTML.slice(0, 3000) }, null, 2)
    );
    return;
  }

  const destino = describirDestino(control);

  NEXORA_LOG("BOTON_DETALLE_DETECTADO");
  NEXORA_LOG(`  estrategia_usada: ${estrategia}`);
  NEXORA_LOG(`  elemento: <${control.tagName.toLowerCase()}> title="${control.getAttribute("title") || ""}"`);
  NEXORA_LOG(`DESTINO_DETALLE: ${destino.tipo} -> ${destino.valor}`);

  enviarEstado(
    "RESULTADO_DETECTADO",
    `Boton de detalle identificado (${estrategia}). Destino detectado: ${destino.tipo}.`
  );

  // Guardamos que estamos a punto de navegar hacia el detalle, para que
  // -si la pagina recarga por completo- la nueva instancia de este script
  // sepa que debe tratar la pagina siguiente como el detalle esperado, y no
  // como una nueva consulta.
  chrome.storage.local
    .set({
      nexoraEsperandoDetalle: {
        placa,
        estrategiaUsada: estrategia,
        destinoDetectado: destino,
        timestamp: Date.now(),
      },
    })
    .then(() => {
      enviarEstado("ABRIENDO_DETALLE", "Ejecutando clic real sobre el control de detalle...");
      // Clic real: dejamos que sea el propio RGM quien decida como navegar
      // (href, __doPostBack, lo que sea). No reproducimos nada por nuestra
      // cuenta.
      control.click();

      // Cubre el caso de actualizacion en el mismo documento (AJAX/UpdatePanel,
      // sin recarga completa de pagina). Si hay recarga completa, este
      // observer queda irrelevante y la deteccion la hace el chequeo de
      // storage al cargar la pagina nueva (ver mas abajo).
      observarLlegadaEnMismoDocumento(placa);
    });
}

function observarLlegadaEnMismoDocumento(placa) {
  const TIMEOUT_MS = 12000;
  let resuelto = false;
  const urlOrigen = location.href;

  const observer = new MutationObserver(() => {
    if (resuelto) return;
    // Si seguimos en la misma URL pero el contenido cambio sustancialmente,
    // lo tratamos como posible llegada al detalle (actualizacion via AJAX).
    if (location.href === urlOrigen && (document.body.innerText || "").length > 300) {
      resuelto = true;
      observer.disconnect();
      chrome.storage.local.get("nexoraEsperandoDetalle", (datos) => {
        if (datos.nexoraEsperandoDetalle) evaluarLlegadaDetalle(placa);
      });
    }
  });
  observer.observe(document.body, { childList: true, subtree: true });

  setTimeout(() => {
    if (resuelto) return;
    observer.disconnect();
    // Si no hubo cambio en el mismo documento, probablemente hubo una
    // navegacion completa: el chequeo al cargar la pagina nueva se encarga.
  }, TIMEOUT_MS);
}

/**
 * Confirma la llegada al detalle y dispara la extraccion real. La
 * validacion de placa YA NO se hace por texto libre de la pagina: se hace
 * contra el campo "placa" extraido de la seccion Bienes Garantizados (ver
 * extraerYValidar), tal como exige la especificacion.
 */
function evaluarLlegadaDetalle(placa) {
  NEXORA_LOG("DETALLE_ABIERTO");
  NEXORA_LOG(`URL_DETALLE: ${location.href}`);
  NEXORA_LOG(`TITULO_DETALLE: ${document.title}`);

  chrome.storage.local.set({ nexoraEsperandoDetalle: null });

  enviarEstado("DETALLE_DETECTADO", "Se llego a la pagina de detalle real.");

  // FASE 2: extractor rediseñado sobre el DOM real ya confirmado (IDs
  // exactos de gvDeudores/gvAcreedores/gvBienesSerial). El diagnostico DOM
  // (ejecutarDiagnosticoDOM) y el extractor generico anterior
  // (extraerYValidar) quedan intactos y disponibles, solo que ya no son el
  // paso que se ejecuta automaticamente al llegar al detalle.
  extraerDatosDetalleReal(placa);
}

// =========================================================================
// DIAGNOSTICO DOM (esta iteracion) — NO interpreta campos, solo describe
// la estructura real de cada seccion: titulo, contenedor, tablas
// descendientes, filas (marcando si son de encabezado o de datos, y si
// estan dentro de <thead>), celdas (con sus atributos y si contienen una
// tabla ANIDADA -- esto explicaria por que el extractor generico confundio
// sub-tablas de detalle con registros).
// =========================================================================

function resumirElemento(el, maxTexto = 200) {
  if (!el) return null;
  return {
    tag: el.tagName ? el.tagName.toLowerCase() : null,
    id: el.id || null,
    clase: typeof el.className === "string" ? el.className : null,
    atributos: el.attributes
      ? Array.from(el.attributes).reduce((acc, a) => {
          acc[a.name] = a.value;
          return acc;
        }, {})
      : {},
    textoDirecto: limpiarTexto(
      Array.from(el.childNodes || [])
        .filter((n) => n.nodeType === Node.TEXT_NODE)
        .map((n) => n.textContent)
        .join(" ")
    ).slice(0, maxTexto),
    textoCompleto: limpiarTexto(el.textContent || "").slice(0, maxTexto),
  };
}

function resumirCelda(celda) {
  const tablaAnidada = celda.querySelector ? celda.querySelector("table") : null;
  return {
    tag: celda.tagName.toLowerCase(),
    id: celda.id || null,
    clase: typeof celda.className === "string" ? celda.className : null,
    colspan: celda.getAttribute("colspan") || null,
    rowspan: celda.getAttribute("rowspan") || null,
    texto: limpiarTexto(celda.textContent || "").slice(0, 300),
    contieneTablaAnidada: !!tablaAnidada,
    numeroDeControles: celda.querySelectorAll ? celda.querySelectorAll("a,button,input,img").length : 0,
  };
}

function resumirFila(fila) {
  const celdas = Array.from(fila.children); // th y/o td
  return {
    tag: fila.tagName.toLowerCase(),
    dentroDeThead: !!fila.closest("thead"),
    esSoloTh: celdas.length > 0 && celdas.every((c) => c.tagName === "TH"),
    numeroDeCeldas: celdas.length,
    celdas: celdas.map(resumirCelda),
  };
}

function resumirTabla(tabla) {
  const filas = Array.from(tabla.querySelectorAll("tr"));
  return {
    tag: "table",
    id: tabla.id || null,
    clase: typeof tabla.className === "string" ? tabla.className : null,
    tieneThead: !!tabla.querySelector("thead"),
    numeroDeFilasTotal: filas.length,
    tablasAnidadasDetectadas: tabla.querySelectorAll("table").length,
    // Limite razonable para no volcar tablas gigantes; si hay mas filas de
    // las mostradas, numeroDeFilasTotal lo deja claro.
    filas: filas.slice(0, 15).map(resumirFila),
  };
}

/**
 * Captura la estructura real de una seccion SIN interpretar que es cada
 * cosa. No asume "cada fila = registro": solo describe lo que hay.
 */
function capturarEstructuraSeccion(tituloBuscado) {
  const elementoTitulo = encontrarEncabezadoSeccion(tituloBuscado);
  if (!elementoTitulo) {
    return { encontrada: false, motivo: "TITULO_NO_ENCONTRADO", tituloBuscado };
  }

  const contenedor =
    elementoTitulo.closest("section, fieldset, table") || elementoTitulo.parentElement || elementoTitulo;

  const tablasDentroDelContenedor = Array.from(contenedor.querySelectorAll("table"));
  const tablaPropia = elementoTitulo.closest("table");
  if (tablaPropia && !tablasDentroDelContenedor.includes(tablaPropia)) {
    tablasDentroDelContenedor.unshift(tablaPropia);
  }

  // Ademas, y solo para diagnostico (no para decidir nada), las 3 tablas
  // siguientes en el documento tras el titulo, por si el contenedor
  // detectado no las incluyera.
  const tablasSiguientesEnDocumento = Array.from(document.querySelectorAll("table"))
    .filter((t) => elementoTitulo.compareDocumentPosition(t) & Node.DOCUMENT_POSITION_FOLLOWING)
    .slice(0, 3);

  const todasLasTablasRelevantes = Array.from(new Set([...tablasDentroDelContenedor, ...tablasSiguientesEnDocumento]));

  return {
    encontrada: true,
    tituloBuscado,
    elementoTitulo: resumirElemento(elementoTitulo),
    elementoPadre: resumirElemento(elementoTitulo.parentElement),
    contenedorDetectado: resumirElemento(contenedor),
    tablasEncontradas: todasLasTablasRelevantes.length,
    tablas: todasLasTablasRelevantes.map(resumirTabla),
  };
}

function ejecutarDiagnosticoDOM(placa) {
  enviarEstado(
    "DIAGNOSTICO_DOM",
    "Capturando estructura real de la pagina de detalle (sin interpretar campos todavia)..."
  );

  const diagnostico = {
    fuente: "RGM",
    url: location.href,
    titulo: document.title,
    placa_consultada: placa,
    timestamp: new Date().toISOString(),
    secciones: {
      deudores_garantes: capturarEstructuraSeccion(TITULOS_SECCION.deudoresGarantes),
      acreedores: capturarEstructuraSeccion(TITULOS_SECCION.acreedores),
      bienes_garantizados: capturarEstructuraSeccion(TITULOS_SECCION.vehiculo),
    },
  };

  NEXORA_LOG(
    "DIAGNOSTICO_DOM capturado. Secciones encontradas: " +
      JSON.stringify({
        deudores_garantes: diagnostico.secciones.deudores_garantes.encontrada,
        acreedores: diagnostico.secciones.acreedores.encontrada,
        bienes_garantizados: diagnostico.secciones.bienes_garantizados.encontrada,
      })
  );

  chrome.storage.local.set({ ultimoDiagnosticoDOM: diagnostico });

  enviarEstado(
    "DIAGNOSTICO_DOM",
    "Diagnostico capturado y guardado. Usa 'DIAGNOSTICO DOM' en el popup para verlo/copiarlo."
  );
  enviarResultadoFinal({
    fuente: "RGM",
    placa_consultada: placa,
    estado: "DIAGNOSTICO_DOM",
    _nota:
      "Esto NO es el resultado final de extraccion, es un mapa estructural para disenar el extractor real. Usa el boton 'DIAGNOSTICO DOM' del popup para ver/copiar el detalle completo.",
    diagnostico_resumen: {
      deudores_garantes_encontrada: diagnostico.secciones.deudores_garantes.encontrada,
      acreedores_encontrada: diagnostico.secciones.acreedores.encontrada,
      bienes_garantizados_encontrada: diagnostico.secciones.bienes_garantizados.encontrada,
    },
  });
}

// =========================================================================
// EXTRACTOR REAL (FASE 2) — basado en el DOM real confirmado por el
// diagnostico, NO en suposiciones. Cada seccion se localiza por su ID real
// de GridView de ASP.NET (unico en la pagina por construccion del HTML),
// nunca por cercania de titulo ni por recorrer todo el documento. Dentro de
// cada tabla, cada valor se localiza por el TEXTO REAL de su etiqueta (la
// celda hermana siguiente es el valor), nunca por posicion fija.
// =========================================================================

const IDS_TABLAS_DETALLE = {
  deudores: "ContentPlaceHolderSeguridad_ContentPlaceHolderContenido_ConsultaGM1_gvDeudores",
  acreedores: "ContentPlaceHolderSeguridad_ContentPlaceHolderContenido_ConsultaGM1_gvAcreedores",
  bienes: "ContentPlaceHolderSeguridad_ContentPlaceHolderContenido_ConsultaGM1_gvBienesSerial",
};

/**
 * Busca la tabla por su ID EXACTO confirmado en el DOM real. Como
 * respaldo (documentado, no una suposicion nueva: mismo control, distinto
 * prefijo de ContentPlaceHolder si algun dia cambia el master page),
 * intenta por el sufijo propio del control (gvDeudores, etc.), que es la
 * parte estable y con significado real.
 */
function encontrarTablaPorIdReal(idExacto) {
  const exacta = document.getElementById(idExacto);
  if (exacta) return exacta;
  const sufijo = idExacto.split("_").pop();
  return document.querySelector(`[id$="_${sufijo}"], [id="${sufijo}"]`);
}

/**
 * Dentro de un contenedor (una tabla especifica, incluyendo lo que haya en
 * tablas anidadas dentro de ella, ya que querySelectorAll recorre todos
 * los descendientes), busca la celda cuyo texto sea exactamente la
 * etiqueta buscada (con o sin ":" final) y devuelve el texto de la celda
 * INMEDIATAMENTE siguiente en la misma fila (el valor asociado real, no
 * una posicion fija global).
 */
function buscarValorPorEtiqueta(contenedor, etiquetaBuscada) {
  if (!contenedor) return null;
  const normalizado = normalizarTexto(etiquetaBuscada).replace(/:$/, "");
  const celdas = Array.from(contenedor.querySelectorAll("td, th"));

  for (const celda of celdas) {
    const textoCelda = normalizarTexto(celda.textContent).replace(/:$/, "");
    if (textoCelda === normalizado) {
      const siguienteCelda = celda.nextElementSibling;
      return siguienteCelda ? limpiarTexto(siguienteCelda.textContent) || null : null;
    }
  }
  return null;
}

/**
 * CORRECCION DEFINITIVA - Deudores/Garantes, basada en el DOM real
 * confirmado por el diagnostico (gvDeudores: 2 filas, sin thead, sin
 * tablas anidadas; fila 1 = <th>Deudor o garante</th>, fila 2 = un UNICO
 * <td> con todas las etiquetas y valores como texto consecutivo). No usa
 * celda hermana ni columnas de grilla: esas hipotesis no aplican a esta
 * estructura real.
 */
function normalizarEspacios(s) {
  return (s || "").replace(/\s+/g, " ").trim();
}

/**
 * Dentro de la tabla dada, localiza la fila que tiene un <td> (nunca la de
 * solo <th>) y devuelve el texto de esa unica celda, con espacios
 * normalizados. Localizacion por estructura, no por posicion fija: si la
 * tabla tuviera mas filas, seguiria encontrando la que realmente trae los
 * datos.
 */
function obtenerTextoUnicaCeldaDeDatos(tabla) {
  const filaConDatos = Array.from(tabla.querySelectorAll("tr")).find((fila) => fila.querySelector("td"));
  const celda = filaConDatos ? filaConDatos.querySelector("td") : null;
  return celda ? normalizarEspacios(celda.textContent) : null;
}

/**
 * Extrae el texto entre dos etiquetas literales dentro de un texto ya
 * normalizado en espacios, usando limites por indexOf (sin regex, sin
 * busqueda global del documento).
 */
function extraerCampoEntreEtiquetas(texto, etiquetaInicio, etiquetaFin) {
  const idxInicio = texto.indexOf(etiquetaInicio);
  if (idxInicio === -1) return null;
  const desde = idxInicio + etiquetaInicio.length;
  const idxFin = etiquetaFin ? texto.indexOf(etiquetaFin, desde) : -1;
  const hasta = idxFin === -1 ? texto.length : idxFin;
  const valor = texto.slice(desde, hasta).trim();
  return valor || null;
}

function extraerDeudorGaranteReal() {
  const tabla = encontrarTablaPorIdReal(IDS_TABLAS_DETALLE.deudores);
  if (!tabla) return { encontrada: false, valor: null };

  const texto = obtenerTextoUnicaCeldaDeDatos(tabla);
  if (!texto) {
    return {
      encontrada: true,
      valor: { nombre: null, tipo_identificacion: null, numero_identificacion: null, ciudad: null },
    };
  }

  return {
    encontrada: true,
    valor: {
      nombre: extraerCampoEntreEtiquetas(texto, "Razón Social o Nombre", "Tipo Identificación"),
      tipo_identificacion: extraerCampoEntreEtiquetas(texto, "Tipo Identificación", "Número de Identificación"),
      numero_identificacion: extraerCampoEntreEtiquetas(
        texto,
        "Número de Identificación",
        "Dígito De Verificación"
      ),
      ciudad: extraerCampoEntreEtiquetas(texto, "Ciudad", "Dirección"),
    },
  };
}

function extraerAcreedorReal() {
  const tabla = encontrarTablaPorIdReal(IDS_TABLAS_DETALLE.acreedores);
  if (!tabla) return { encontrada: false, valor: null };

  return {
    encontrada: true,
    valor: {
      razon_social: buscarValorPorEtiqueta(tabla, "Razón Social o Nombre"),
    },
  };
}

/**
 * Extrae "T. Servicio: X" desde el texto de descripcion del bien, SOLO
 * como fallback cuando el campo directo "Tipo de Servicio" viene vacio
 * (regla explicita de la especificacion). Corta la captura justo antes de
 * la siguiente etiqueta tipo "Palabra:" (p. ej. "Linea:") o al final del
 * texto. No inventa nada: si no encuentra el patron, devuelve null.
 *
 * Caso real verificado:
 *   "...Serie: 9BGEP76C0MB201888T. Servicio: ParticularLinea: TRACKER"
 *   -> "Particular"
 */
function extraerTipoServicioDeDescripcion(descripcion) {
  if (!descripcion) return null;
  const match = descripcion.match(/T\.\s*Servicio:\s*([^:]*?)(?=[A-ZÁÉÍÓÚÑ][a-záéíóúñ.]*:|$)/);
  if (match && match[1] && match[1].trim()) return limpiarTexto(match[1]);
  return null;
}

function extraerVehiculoReal() {
  const tabla = encontrarTablaPorIdReal(IDS_TABLAS_DETALLE.bienes);
  if (!tabla) return { encontrada: false, valor: null };

  const descripcionBien = buscarValorPorEtiqueta(tabla, "Descripción del Bien");
  let tipoServicio = buscarValorPorEtiqueta(tabla, "Tipo de Servicio");
  let tipoServicioOrigen = tipoServicio ? "campo_directo" : null;

  if (!tipoServicio) {
    const desdeDescripcion = extraerTipoServicioDeDescripcion(descripcionBien);
    if (desdeDescripcion) {
      tipoServicio = desdeDescripcion;
      tipoServicioOrigen = "fallback_descripcion_bien";
    }
  }

  return {
    encontrada: true,
    tipoServicioOrigen,
    valor: {
      tipo_bien: buscarValorPorEtiqueta(tabla, "Tipo de Bien"),
      marca: buscarValorPorEtiqueta(tabla, "Marca"),
      fabricante: buscarValorPorEtiqueta(tabla, "Fabricante"),
      modelo: buscarValorPorEtiqueta(tabla, "Año Correspondiente al Modelo"),
      placa: buscarValorPorEtiqueta(tabla, "Placa"),
      numero_serial: buscarValorPorEtiqueta(tabla, "Número de Serial"),
      tipo_servicio: tipoServicio || null,
      descripcion_bien: descripcionBien,
    },
  };
}

function extraerDatosDetalleReal(placaConsultada) {
  enviarEstado(
    "EXTRAYENDO_DATOS",
    "Extrayendo datos reales por ID de tabla (gvDeudores / gvAcreedores / gvBienesSerial)..."
  );

  const deudor = extraerDeudorGaranteReal();
  const acreedor = extraerAcreedorReal();
  const vehiculo = extraerVehiculoReal();

  const camposFaltantes = [];

  if (!deudor.encontrada) {
    camposFaltantes.push("deudor_garante (tabla no encontrada)");
  } else {
    if (!deudor.valor.nombre) camposFaltantes.push("deudor_garante.nombre");
    if (!deudor.valor.tipo_identificacion) camposFaltantes.push("deudor_garante.tipo_identificacion");
    if (!deudor.valor.numero_identificacion) camposFaltantes.push("deudor_garante.numero_identificacion");
    if (!deudor.valor.ciudad) camposFaltantes.push("deudor_garante.ciudad");
  }

  if (!acreedor.encontrada || !acreedor.valor.razon_social) {
    camposFaltantes.push("acreedor.razon_social");
  }

  if (!vehiculo.encontrada) {
    camposFaltantes.push("vehiculo (tabla no encontrada)");
  } else {
    if (!vehiculo.valor.tipo_bien) camposFaltantes.push("vehiculo.tipo_bien");
    if (!vehiculo.valor.marca) camposFaltantes.push("vehiculo.marca");
    if (!vehiculo.valor.fabricante) camposFaltantes.push("vehiculo.fabricante");
    if (!vehiculo.valor.modelo) camposFaltantes.push("vehiculo.modelo");
    if (!vehiculo.valor.placa) camposFaltantes.push("vehiculo.placa");
    if (!vehiculo.valor.numero_serial) camposFaltantes.push("vehiculo.numero_serial");
    if (!vehiculo.valor.descripcion_bien) camposFaltantes.push("vehiculo.descripcion_bien");
  }

  // Comparacion normalizada SOLO para mayusculas/espacios; el valor
  // almacenado en el JSON conserva el texto tal cual se extrajo (regla 16).
  const placaExtraidaCruda = vehiculo.valor ? vehiculo.valor.placa : null;
  const placaCoincide =
    !!placaExtraidaCruda && limpiarTexto(placaExtraidaCruda).toUpperCase() === limpiarTexto(placaConsultada).toUpperCase();

  if (placaExtraidaCruda && !placaCoincide) {
    camposFaltantes.push(`placa_coincide (consultada="${placaConsultada}", extraida="${placaExtraidaCruda}")`);
  }

  const estado = camposFaltantes.length === 0 && placaCoincide ? "OK" : "EXTRACCION_INCOMPLETA";

  NEXORA_LOG(`EXTRACCION_REAL estado=${estado}`);
  if (vehiculo.tipoServicioOrigen) NEXORA_LOG(`tipo_servicio obtenido via: ${vehiculo.tipoServicioOrigen}`);
  if (camposFaltantes.length > 0) NEXORA_LOG(`Campos/validaciones faltantes: ${JSON.stringify(camposFaltantes)}`);

  const resultado = {
    fuente: "RGM",
    placa_consultada: placaConsultada,
    estado,
    deudor_garante: deudor.valor,
    acreedor: acreedor.valor,
    vehiculo: vehiculo.valor,
  };
  if (camposFaltantes.length > 0) resultado.campos_faltantes = camposFaltantes;

  if (estado === "OK") {
    enviarEstado("EXTRACCION_COMPLETADA", "Los tres bloques se extrajeron correctamente y la placa coincide.");
  } else {
    enviarEstado("EXTRACCION_INCOMPLETA", `Faltan: ${camposFaltantes.join(", ")}`);
  }
  enviarResultadoFinal(resultado);
}

// =========================================================================
// EXTRACTOR (iteracion ANTERIOR, generico por titulo/columna) — NO se usa
// ni se corrige en esta iteracion. Se deja intacto tal como pediste.
// =========================================================================
//
// Misma filosofia que la deteccion de la tabla de resultados: localizacion
// por CONTENIDO (titulo real de seccion + nombres reales de columna que
// reportaste), nunca por clase/id/posicion inventados. Si algo no se
// encuentra, queda null + diagnostico, nunca un valor fabricado.
// =========================================================================

const TITULOS_SECCION = {
  deudoresGarantes: "Deudores y Garantes",
  acreedores: "Acreedores Garantizados",
  vehiculo: "Bienes Garantizados (Por serial)",
};

const COLUMNAS_DEUDOR_GARANTE = {
  nombre: ["nombre"],
  tipo_identificacion: ["tipo de identificacion", "tipo identificacion", "tipo doc"],
  numero_identificacion: ["numero de identificacion", "identificacion", "documento"],
  ciudad: ["ciudad"],
};

const COLUMNAS_ACREEDOR = {
  nombre: ["razon social", "nombre", "acreedor"],
};

const COLUMNAS_VEHICULO = {
  tipo_bien: ["tipo de bien", "tipo bien"],
  marca: ["marca"],
  fabricante: ["fabricante"],
  anio_modelo: ["ano/modelo", "ano modelo", "modelo", "ano"],
  placa: ["placa"],
  numero_serie: ["numero de serie", "serie", "serial"],
  tipo_servicio: ["tipo de servicio", "servicio"],
  descripcion: ["descripcion del bien", "descripcion"],
};

function limpiarTexto(s) {
  return (s || "").replace(/\s+/g, " ").trim();
}

/**
 * Busca el elemento cuyo texto coincida (exacto o por inclusion cercana,
 * ambos normalizados) con el titulo real de seccion reportado.
 */
function encontrarEncabezadoSeccion(tituloBuscado) {
  const normalizado = normalizarTexto(tituloBuscado);
  const candidatos = document.querySelectorAll("h1,h2,h3,h4,h5,h6,caption,legend,strong,b,th,td,div,span,p");
  let mejorParcial = null;

  for (const el of candidatos) {
    const textoPropio = normalizarTexto(
      Array.from(el.childNodes)
        .filter((n) => n.nodeType === Node.TEXT_NODE)
        .map((n) => n.textContent)
        .join(" ")
    );
    const textoCompleto = normalizarTexto(el.textContent);

    if (textoPropio === normalizado || textoCompleto === normalizado) return el;
    if (!mejorParcial && textoCompleto.includes(normalizado) && textoCompleto.length < normalizado.length + 40) {
      mejorParcial = el;
    }
  }
  return mejorParcial;
}

/**
 * A partir del titulo de seccion, busca entre las tablas que le siguen en
 * el documento la que mas coincidencias de columna tenga con lo esperado
 * (misma tecnica de puntaje que encontrarTablaResultados). Evita cruzar
 * ciegamente a la tabla de otra seccion mas lejana.
 */
function encontrarTablaDeSeccion(elementoTitulo, mapaColumnas) {
  if (!elementoTitulo) return null;
  const nombresEsperados = Object.values(mapaColumnas).flat();

  const puntuarTabla = (tabla) => {
    const encabezados = Array.from(tabla.querySelectorAll("th, tr:first-child td")).map((c) =>
      normalizarTexto(c.textContent)
    );
    let puntaje = 0;
    for (const nombre of nombresEsperados) {
      if (encabezados.some((t) => t.includes(normalizarTexto(nombre)))) puntaje++;
    }
    return puntaje;
  };

  const tablaPadre = elementoTitulo.closest("table");
  if (tablaPadre && puntuarTabla(tablaPadre) > 0) return tablaPadre;

  const siguientes = Array.from(document.querySelectorAll("table")).filter(
    (tabla) => elementoTitulo.compareDocumentPosition(tabla) & Node.DOCUMENT_POSITION_FOLLOWING
  );

  let mejor = null;
  let mejorPuntaje = 0;
  for (let i = 0; i < Math.min(siguientes.length, 6); i++) {
    const puntaje = puntuarTabla(siguientes[i]);
    if (puntaje > mejorPuntaje) {
      mejorPuntaje = puntaje;
      mejor = siguientes[i];
    }
  }
  return mejor || siguientes[0] || tablaPadre || null;
}

function indiceColumnaPorNombres(tabla, nombresPosibles) {
  const encabezados = Array.from(tabla.querySelectorAll("th, tr:first-child td"));
  const textos = encabezados.map((c) => normalizarTexto(c.textContent));
  for (const nombre of nombresPosibles) {
    const idx = textos.findIndex((t) => t.includes(normalizarTexto(nombre)));
    if (idx !== -1) return idx;
  }
  return -1;
}

/**
 * Extrae uno o varios registros (una fila = un registro) segun un mapa de
 * {campoSalida: [nombresDeColumnaPosibles]}. Si una columna no se
 * encuentra, ese campo queda null en TODOS los registros: nunca se inventa.
 */
function extraerRegistrosDeTabla(tabla, mapaColumnas) {
  const indices = {};
  for (const [campo, nombres] of Object.entries(mapaColumnas)) {
    indices[campo] = indiceColumnaPorNombres(tabla, nombres);
  }

  const registros = filasDeDatos(tabla)
    .map((fila) => {
      const celdas = fila.children;
      const registro = {};
      let algunValor = false;
      for (const [campo, idx] of Object.entries(indices)) {
        const valor = idx !== -1 && celdas[idx] ? limpiarTexto(celdas[idx].textContent) : null;
        registro[campo] = valor || null;
        if (valor) algunValor = true;
      }
      return { registro, algunValor };
    })
    .filter((r) => r.algunValor)
    .map((r) => r.registro);

  return { registros, indices };
}

/**
 * Extrae una seccion completa (titulo -> tabla -> registros) y devuelve
 * tambien un diagnostico ACOTADO (no vuelca toda la pagina) cuando algo
 * falta.
 */
function extraerSeccion(nombreSeccion, tituloBuscado, mapaColumnas) {
  const elementoTitulo = encontrarEncabezadoSeccion(tituloBuscado);
  if (!elementoTitulo) {
    return {
      registros: [],
      encontrada: false,
      diagnostico: { seccion: nombreSeccion, motivo: "TITULO_SECCION_NO_ENCONTRADO", tituloBuscado },
    };
  }

  const tabla = encontrarTablaDeSeccion(elementoTitulo, mapaColumnas);
  if (!tabla) {
    return {
      registros: [],
      encontrada: false,
      diagnostico: {
        seccion: nombreSeccion,
        motivo: "TABLA_NO_ENCONTRADA_TRAS_TITULO",
        tituloBuscado,
        contextoHTML: elementoTitulo.outerHTML.slice(0, 1000),
      },
    };
  }

  const { registros, indices } = extraerRegistrosDeTabla(tabla, mapaColumnas);
  const columnasNoEncontradas = Object.entries(indices)
    .filter(([, idx]) => idx === -1)
    .map(([campo]) => campo);

  return {
    registros,
    encontrada: true,
    diagnostico:
      columnasNoEncontradas.length > 0 || registros.length === 0
        ? {
            seccion: nombreSeccion,
            motivo: registros.length === 0 ? "TABLA_SIN_FILAS_DE_DATOS" : "COLUMNAS_NO_ENCONTRADAS",
            columnasNoEncontradas,
            encabezadosTabla: Array.from(tabla.querySelectorAll("th, tr:first-child td")).map((c) =>
              limpiarTexto(c.textContent)
            ),
            tablaHTML: tabla.outerHTML.slice(0, 2000),
          }
        : null,
  };
}

function extraerYValidar(placa) {
  enviarEstado(
    "EXTRAYENDO_DATOS",
    "Localizando Deudores y Garantes / Acreedores Garantizados / Bienes Garantizados..."
  );

  const seccionDeudores = extraerSeccion(
    "deudores_garantes",
    TITULOS_SECCION.deudoresGarantes,
    COLUMNAS_DEUDOR_GARANTE
  );
  const seccionAcreedores = extraerSeccion("acreedores", TITULOS_SECCION.acreedores, COLUMNAS_ACREEDOR);
  const seccionVehiculo = extraerSeccion("vehiculo", TITULOS_SECCION.vehiculo, COLUMNAS_VEHICULO);

  const vehiculoRegistro = seccionVehiculo.registros[0] || null;
  const placaExtraida =
    vehiculoRegistro && vehiculoRegistro.placa ? vehiculoRegistro.placa.toUpperCase().trim() : null;
  const placaEncontrada = !!placaExtraida;
  const placaCoincide = placaEncontrada && placaExtraida === placa.toUpperCase().trim();

  const diagnosticos = [seccionDeudores.diagnostico, seccionAcreedores.diagnostico, seccionVehiculo.diagnostico].filter(
    Boolean
  );

  const extraccion = {
    deudores_garantes: seccionDeudores.encontrada && seccionDeudores.registros.length > 0,
    acreedores: seccionAcreedores.encontrada && seccionAcreedores.registros.length > 0,
    vehiculo: seccionVehiculo.encontrada && seccionVehiculo.registros.length > 0,
    placa_encontrada: placaEncontrada,
    placa_coincide: placaCoincide,
  };

  NEXORA_LOG(`extraccion: ${JSON.stringify(extraccion)}`);

  if (diagnosticos.length > 0) {
    NEXORA_LOG(
      `Diagnosticos de extraccion (secciones incompletas): ${JSON.stringify(
        diagnosticos.map((d) => ({ seccion: d.seccion, motivo: d.motivo }))
      )}`
    );
    chrome.storage.local.set({
      ultimoSnapshotDesconocido: {
        placa,
        motivo: "EXTRACCION_PARCIAL",
        url: location.href,
        titulo: document.title,
        diagnosticos,
        timestamp: new Date().toISOString(),
      },
    });
  }

  const resultadoBase = {
    fuente: "RGM",
    placa_consultada: placa,
    validacion_placa: placaCoincide ? "OK" : "ERROR",
    deudores_garantes: seccionDeudores.registros,
    acreedores: seccionAcreedores.registros,
    vehiculo: {
      tipo_bien: vehiculoRegistro ? vehiculoRegistro.tipo_bien : null,
      marca: vehiculoRegistro ? vehiculoRegistro.marca : null,
      fabricante: vehiculoRegistro ? vehiculoRegistro.fabricante : null,
      anio_modelo: vehiculoRegistro ? vehiculoRegistro.anio_modelo : null,
      placa: vehiculoRegistro ? vehiculoRegistro.placa : null,
      numero_serie: vehiculoRegistro ? vehiculoRegistro.numero_serie : null,
      tipo_servicio: vehiculoRegistro ? vehiculoRegistro.tipo_servicio : null,
      descripcion: vehiculoRegistro ? vehiculoRegistro.descripcion : null,
    },
    extraccion,
    url_detalle: location.href,
    titulo_pagina: document.title,
  };

  // La placa NUNCA se valida contra la URL de consulta: se valida contra el
  // campo real extraido de la seccion Bienes Garantizados.
  if (placaEncontrada && !placaCoincide) {
    enviarEstado(
      "PLACA_NO_COINCIDE",
      `Placa consultada "${placa}" no coincide con la placa extraida "${placaExtraida}". No se continua como resultado valido.`
    );
    enviarResultadoFinal({ ...resultadoBase, estado: "PLACA_NO_COINCIDE" });
    return;
  }

  const completa =
    extraccion.deudores_garantes && extraccion.acreedores && extraccion.vehiculo && extraccion.placa_encontrada;

  if (completa) {
    enviarEstado("EXTRACCION_COMPLETADA", "Las tres secciones se extrajeron y la placa coincide.");
    enviarResultadoFinal({ ...resultadoBase, estado: "EXTRACCION_COMPLETADA" });
  } else {
    enviarEstado(
      "EXTRACCION_INCOMPLETA",
      "Al menos una seccion/campo no se pudo extraer. Revisa 'extraccion' en el JSON y el snapshot de diagnostico."
    );
    enviarResultadoFinal({ ...resultadoBase, estado: "EXTRACCION_INCOMPLETA" });
  }
}

function capturarSnapshotDesconocido(placa, motivo, extra) {
  enviarEstado("ESTRUCTURA_NO_RECONOCIDA", `Motivo: ${motivo}. Se guardo un snapshot de diagnostico.`);
  chrome.storage.local.set({
    ultimoSnapshotDesconocido: {
      placa,
      motivo,
      extra: extra || null,
      url: location.href,
      titulo: document.title,
      timestamp: new Date().toISOString(),
    },
    nexoraEsperandoDetalle: null,
  });
  enviarResultadoFinal({
    fuente: "RGM",
    placa_consultada: placa,
    estado: "ESTRUCTURA_NO_RECONOCIDA",
    motivo,
    url: location.href,
    _nota: "Revisa 'ultimoSnapshotDesconocido' en chrome.storage.local para disenar los selectores reales.",
  });
}

// =========================================================================
// FALLBACK: formulario tradicional del home (v0.1 original, no usado por
// defecto; se conserva por si el flujo directo por URL deja de funcionar).
// =========================================================================

const SELECTORES_FORMULARIO = {
  formularioNoOficial: "#formularioConsultaNoOficial",
  radioNumeroDeBien: "#numeroDeBien",
  inputNumeroBien: "#opcion-2",
  botonConsultar: "#enviaConsultaNoOficial",
};

function detectarFormulario() {
  const form = document.querySelector(SELECTORES_FORMULARIO.formularioNoOficial);
  if (form) {
    enviarEstado("FORMULARIO_DETECTADO", "Formulario de consulta no oficial encontrado (flujo de respaldo).");
    return form;
  }
  return null;
}

function completarYEnviarConsultaFormulario(placa) {
  const radio = document.querySelector(SELECTORES_FORMULARIO.radioNumeroDeBien);
  const input = document.querySelector(SELECTORES_FORMULARIO.inputNumeroBien);
  const boton = document.querySelector(SELECTORES_FORMULARIO.botonConsultar);
  if (!radio || !input || !boton) {
    enviarEstado("ERROR", "No se encontraron todos los controles esperados del formulario (flujo de respaldo).");
    return;
  }
  radio.click();
  setTimeout(() => {
    input.value = placa.toUpperCase().trim();
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    enviarEstado("CONSULTANDO", `(Respaldo) Enviando consulta para la placa ${placa}...`);
    boton.click();
  }, 300);
}

// =========================================================================
// PUNTO DE ENTRADA
// =========================================================================

chrome.runtime.onMessage.addListener((mensaje, sender, sendResponse) => {
  if (mensaje.tipo === "ANALIZAR_PAGINA_RGM") {
    NEXORA_LOG(`Connector iniciado. Fuente: RGM (flujo directo por URL). Placa: ${mensaje.placa}`);
    manejarAnalizarPaginaRGM(mensaje.placa);
    sendResponse({ ok: true });
    return true;
  }

  if (mensaje.tipo === "EJECUTAR_CONSULTA_RGM") {
    const form = detectarFormulario();
    if (!form) {
      enviarEstado("ERROR", "No se detecto el formulario de consulta en esta pagina (flujo de respaldo).");
      sendResponse({ ok: false });
      return true;
    }
    completarYEnviarConsultaFormulario(mensaje.placa);
    sendResponse({ ok: true });
  }
  return true;
});

// Al cargar CUALQUIER pagina del dominio, comprobamos si veniamos de hacer
// clic en el boton de detalle (navegacion completa a una pagina nueva). Si
// es asi, esta carga ES la pagina de detalle que esperabamos.
(function comprobarLlegadaTrasNavegacionCompleta() {
  chrome.storage.local.get("nexoraEsperandoDetalle", (datos) => {
    const espera = datos.nexoraEsperandoDetalle;
    if (!espera) return;
    const antiguedadMs = Date.now() - (espera.timestamp || 0);
    if (antiguedadMs > 30000) {
      // Muy viejo: probablemente una espera anterior que nunca se limpio.
      chrome.storage.local.set({ nexoraEsperandoDetalle: null });
      return;
    }
    NEXORA_LOG("Pagina cargada tras clic en boton de detalle (navegacion completa detectada).");
    evaluarLlegadaDetalle(espera.placa);
  });
})();

NEXORA_LOG("Content script cargado en " + location.href);
