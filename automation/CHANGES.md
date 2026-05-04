# Cambios v2 — extraction_notes y multi-empresa

Fecha: 2026-04-25
Aplica a las dos tareas programadas de Cowork: `cararjfam` y `best-training`.

## Por qué

En la versión v1 (umbral de confianza 0.80) varias facturas válidas iban a *Revisión* porque Cowork era honesto con sus dudas (ej. "IVA no impreso → confidence 0.6"). Esto generaba trabajo innecesario porque, al quedar todas las facturas en **borrador** en Odoo, el humano ya es la red de seguridad final.

Conclusión: **dejamos pasar todo lo extraíble** y obligamos a Cowork a **anotar** cualquier duda, que se muestra al revisor en Odoo en el campo "Notas".

## Qué cambia en el VPS (ya desplegado)

1. **`process_invoice.py`** — `MIN_CONFIDENCE` baja de **0.80 → 0.0**. La confianza se vuelve informativa, no decide.
2. **`process_invoice.py`** — lee un nuevo campo `extraction_notes` del JSON. Lo escribe en:
   - `narration` de la factura → visible en la pestaña "Otra información" → "Notas".
   - chatter (mensaje en el log de la factura, siempre visible en la parte inferior del formulario).
3. **`process_invoice.py`** + **`server.py`** + **`poller.py`** — multi-empresa: la SA atiende varias `Cola_VPS` y enruta a `company_id` correcto en Odoo.
4. **`companies.py`** (NUEVO) — registro central de empresas con CIF + folder IDs Drive + odoo_company_id.

## Qué cambia para Cowork (los 2 prompts)

### 1. Decisión: siempre la más probable
> "Toma siempre la decisión MÁS PROBABLE. No abandones la extracción porque algo no esté 100% claro."

Antes Cowork tenía la "salida fácil" de bajar la confianza y mandar a Revisión. Ahora extrae siempre que el documento sea reconocible como factura.

### 2. Campo nuevo `extraction_notes`
Toda duda se vuelca a este campo en lenguaje natural. Ejemplos:
- "IVA 21% no impreso, asumido (servicio profesional)."
- "Número de factura ilegible parcialmente: podría ser 226 o 228."
- "Total impreso dice 50.01; he tomado 50.00 que cuadra con base+IVA."
- "Factura simplificada (ticket); proveedor extraído del nombre comercial."

El revisor humano ve esto al abrir la factura en Odoo, decide y valida.

### 3. Validación cliente más estricta
- subtotal + iva ≈ total (margen 0.05 €). Si falla → NO subir, error.
- suma(líneas) ≈ subtotal. Si falla → NO subir.
- VAT con formato válido. Si falla → NO subir.

Estos son *blockers* duros, no van a Revisión: directamente registran como error.

### 4. Confianza informativa
La confianza ya no decide nada. El VPS acepta cualquier valor (0.0 a 1.0). Cowork la sigue calculando para reporting.

## Files

- [cowork_prompt.txt](cowork_prompt.txt) — CARARJFAM2019 (carpetas: `1RZjKO1G...`, `1dIQ0IKG...`, etc.)
- [cowork_prompt_besttraining.txt](cowork_prompt_besttraining.txt) — Best Training (carpetas: `1d12YefA...`, `13vIwkLL...`, etc.)
- [CHANGES.md](CHANGES.md) — este documento.

## Cómo aplicar los nuevos prompts en Cowork

1. Abre tu tarea programada `facturas-pendientes-drive` (CARARJFAM).
2. Sustituye TODO el prompt por el contenido de `cowork_prompt.txt`.
3. Guarda.
4. Repite con la tarea de Best Training, usando `cowork_prompt_besttraining.txt`.
5. Para validar: ejecuta cada tarea con "Run now" sobre carpetas vacías. Debe responder "PDFs vistos: 0" y subir un CSV solo cabecera.

## Cómo se ve el resultado en Odoo

Al abrir cualquier factura procesada (Compras → Facturas → click):

```
Pestaña "Otra información"
  Notas (narration):
    ⚠ Observaciones extracción automática:
    IVA 21% no impreso, asumido (servicio profesional).

    Confianza: 0.60

    Origen: 20260425-184540-123456-067D2600000427.pdf
```

Y en el chatter inferior:
```
Factura procesada automaticamente. Confianza: 0.60.
Observaciones:
IVA 21% no impreso, asumido (servicio profesional).
Origen: 20260425-184540-123456-067D2600000427.pdf
```
