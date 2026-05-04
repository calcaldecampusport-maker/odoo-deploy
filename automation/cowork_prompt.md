# Cowork Scheduled Task — Procesar facturas (vía Drive bus)

## Por qué este prompt es distinto al primero

El sandbox de Cowork bloquea HTTP outbound y `web_fetch` no soporta POST. Por eso usamos **Drive como bus**: Cowork escribe un JSON en una subcarpeta especial y el VPS lo recoge cada 5 minutos.

## Configuración de la tarea programada en Cowork

- **Frecuencia**: cada 1 hora
- **Cron**: `0 * * * *`
- **Timezone**: Europe/Madrid
- **Conector**: Google Drive (con la cuenta de las carpetas)

## Prompt (copia y pega TODO el bloque)

```
Procesa las facturas de proveedores pendientes en Google Drive.

CONTEXTO
- Empresa: CARARJFAM2019,SL (CIF B93653392), España.
- Carpeta "Pendientes" (id 1RZjKO1GqJuPURl6WTsl2R9egwm7cyYFQ): contiene PDFs a procesar.
- Subcarpeta "Cola_VPS" (id 1dIQ0IKGGk-3oJc9129pmA5IDepVzp71-): donde dejarás un JSON por cada PDF que extraigas. El VPS lee de aquí cada 5 min y tú no tienes que llamar a ninguna URL.

PASOS

1. Lista archivos PDF en "Pendientes" con search_files:
   query: parents in '1RZjKO1GqJuPURl6WTsl2R9egwm7cyYFQ' and mimeType='application/pdf' and trashed=false

2. Lista archivos JSON ya existentes en "Cola_VPS" con search_files:
   query: parents in '1dIQ0IKGGk-3oJc9129pmA5IDepVzp71-' and trashed=false

3. Construye el conjunto de drive_file_id ya en cola (extraer del nombre del JSON: si el JSON se llama "{ID}.json", saca "{ID}").

4. Para cada PDF en Pendientes cuyo id NO esté en el conjunto de la cola:

   a. Lee el PDF con read_file_content (fileId del PDF).

   b. Extrae con cuidado:
      - supplier_name: razón social del emisor
      - supplier_vat: NIF/CIF español. Quita el prefijo "ES" si aparece. Formato: letra+8 dígitos (CIF, ej. B12345678) o 8 dígitos+letra (NIF persona física)
      - invoice_ref: número de factura del proveedor
      - invoice_date: YYYY-MM-DD
      - due_date: YYYY-MM-DD si aparece, si no omite el campo
      - subtotal: base imponible total
      - tax_total: total IVA
      - total: total a pagar
      - lines: array. Una entrada por cada tipo distinto de IVA en la factura:
          { "description": "<qué es>", "amount": <base imponible de esa línea>, "tax_rate": 21|10|4|0 }

   c. Calcula extraction_confidence (0..1) basándote en si pudiste leer todos los campos sin dudar:
      - 1.0: todo claro
      - 0.7-0.9: alguna duda menor
      - <0.7: campos ambiguos -> el VPS enviará a Revisión automáticamente

   d. Valida tú mismo:
      - subtotal + tax_total ≈ total (margen 0.02 €)
      - suma(line.amount) ≈ subtotal
      - VAT con formato válido
      Si falla algo, baja confidence a 0.3.

   e. Construye el objeto JSON con esta estructura exacta:
      {
        "drive_file_id": "<el fileId del PDF>",
        "supplier_name": "...",
        "supplier_vat": "...",
        "invoice_ref": "...",
        "invoice_date": "YYYY-MM-DD",
        "due_date": "YYYY-MM-DD",
        "subtotal": 100.00,
        "tax_total": 21.00,
        "total": 121.00,
        "lines": [{"description": "...", "amount": 100.00, "tax_rate": 21}],
        "extraction_confidence": 0.95
      }

   f. Sube ese JSON a Cola_VPS usando create_file con:
      - title: "{drive_file_id_del_PDF}.json"   (nombre exacto = el ID del PDF + ".json")
      - mimeType: "application/json"
      - parentId: "1dIQ0IKGGk-3oJc9129pmA5IDepVzp71-"
      - content: base64 del string JSON
      - disableConversionToGoogleType: true

5. Al terminar, devuelve un resumen breve:
   - PDFs vistos en Pendientes: N
   - PDFs ya en cola del VPS (saltados): X
   - JSONs creados en Cola_VPS: Y
   - Errores de extracción: Z (con motivos por archivo)

REGLAS DURAS
- NUNCA inventes datos. Si dudas, baja extraction_confidence; el VPS lo mandará a Revisión.
- NUNCA muevas/borres archivos en Drive: el VPS lo hace por ti.
- NUNCA crees dos JSONs para el mismo drive_file_id (por eso el paso 2 + 3, dedupe por id).
- El nombre del JSON DEBE ser exactamente "{drive_file_id_del_PDF}.json" para que el dedupe funcione en próximos ciclos.
```

## Cómo va a funcionar visto en conjunto

```
PDF en Pendientes
    │ (cada 1h, Cowork)
    ▼
Cowork extrae → escribe JSON en Cola_VPS (Drive)
    │ (cada 5min, cron VPS)
    ▼
poller.py lee JSON → POST localhost:8080 → ORM crea factura → SA mueve PDF
    │
    ▼
PDF a Contabilizados o Revisión (Drive)  +  JSON eliminado de Cola_VPS
```

## Tras pegar el prompt

1. Activa la tarea.
2. Pulsa "Run now" con la carpeta vacía → respuesta esperada: "PDFs vistos: 0".
3. Sube 1 PDF real a Pendientes.
4. Pulsa "Run now" otra vez → debería escribir un JSON en Cola_VPS.
5. Espera ≤ 5 min.
6. Mira en Drive: el PDF debería estar en Contabilizados, y Cola_VPS de nuevo vacía.
7. Mira en Odoo: la factura en estado borrador, con el PDF adjuntado.
