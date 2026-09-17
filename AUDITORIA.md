# AUDITORÍA DEL ECOSISTEMA — 17/09/2026

> Foto fechada, no un documento a mantener. Para el estado actual, mira el `CLAUDE.md` de
> cada repo y `RECOVERY.md`.

## Por qué se hizo

El 16/09/2026 se perdió el ordenador de trabajo con todas las claves SSH y los repos
locales. Al reconstruir el acceso quedó claro que nadie tenía una visión completa del
sistema: 7 repositorios, 4 webs de contabilidad, 3 instancias de Odoo, 6 pipelines y una
docena de servicios. Esta auditoría recorrió todo contrastando **el código y el servidor**,
no la documentación.

Regla de trabajo: cada afirmación sale de algo verificado —`res_company`, la tabla
`empresa`, los `.env`, `systemctl`, `nginx -T`, `git log`— y lo que no se pudo verificar se
dejó marcado como hueco en vez de rellenarlo a ojo.

---

## El mapa que faltaba

### Empresas, bases de datos y Odoo

| Empresa | Base de datos | company_id | Odoo |
|---|---|---|---|
| Wiemspro SL | `wiems_v18_prod` | 1 | 18 Enterprise (alojado) |
| Wiemspro CORP | `wiems_v18_prod` | 3 | 18 Enterprise |
| Bio Sensors Suit Group SL | `wiems_v18_prod` | 9 | 18 Enterprise |
| International Austral Sport SA | `wiems_v18_prod` | 12 | 18 Enterprise |
| Medical Cables SL | `medical_v18` | 1 | 18 Community (alojado) |
| CARARJFAM2019, SL | `cararjfam` | 1 | 17 (en el VPS) |
| Best Training Rincón de la Victoria SL | `round_facturacion` | 3 | 17 (en el VPS) |

**Los `company_id` no son globales.** La company 1 es Wiemspro SL, CARARJFAM2019 o Medical
Cables según la base de datos. Cualquier regla que diga «company N» sin nombrar la BD es
ambigua, y en contabilidad ambiguo es peligroso.

`cararjfam_test` es la BD de migración (1 CARARJFAM, 2 BT, 3 AUSTRAL en migración,
4 INTERNATIONAL AUSTRAL SPORT). Sus ids **no valen en producción**; conserva el histórico
de Austral hasta el 30/06/2026 y no se escribe más en ella.

### Webs

| Dominio | Raíz estática | Backend | Servicio |
|---|---|---|---|
| contab.wiemspro.com | `/var/www/contab` | :5001 | `wiemspro-contab` |
| contab.medicalcables.eu | `/var/www/medicalcables` | :5002 | `medicalcables-contab` |
| **pruebas-ca.medicalcables.eu** | `/var/www/austral-contab` | :5003 | `austral-contab` ← **la de Austral** |
| **austral.carajfam.com** | `/var/www/carajfam` | :5004 | `carajfam-contab` ← **la de CARARJFAM/BT** |
| gestionnoofit.wiemspro.com | `/var/www/gestionnoofit` | :8096 | `gestionnoofit_api` |
| round.wiemspro.com | `/var/www/round` | :8095 | `round_config_api` |

Trampa de nombres: **`austral.carajfam.com` no es la web de Austral.**

### De dónde sale el código de cada web

`wiemspro-contab` es la plataforma común. Su `backend/app` y su `frontend/src` se copian
cada 4 horas a Medical y a CARARJFAM/BT (`aplicar_desde_pc.sh`), que reaplican encima sus
personalizaciones (`fixups.py` y `personalizar_carajfam.py`). El frontend de Austral
también sale de `wiemspro-contab`, pero se compila a mano.

Consecuencia: **en los repos de Medical y CARARJFAM, `backend/app` es un derivado, no una
fuente.** Un cambio propio de esas webs solo sobrevive si está en su script de
personalización.

---

## Hallazgos

### 🔴 Código en producción sin repositorio ni copia de seguridad

Lo más grave, y era sistémico.

| Proyecto | Ficheros | Situación |
|---|---|---|
| `round_config_api` | 210 `.py` | Servicio activo. Sin repo |
| `gn_scripts` | 142 `.py` | Sin repo |
| `round_mcp` | 16 `.py` | Servicio activo. Sin repo |
| `austral-contab` | 14 `.py` | Servicio activo. Sin repo |
| `automation_austral_e18` | 19 `.py` | En producción parcial. Sin repo ni backup |
| `automation_bt`, `automation_cf` | 30 `.py` | Port a medias. Sin repo ni backup |
| `personalizar_carajfam.py` | 1 | Se ejecuta en cada sync. Sin repo |

**Resuelto**: cuatro repositorios privados nuevos, los tres pipelines y el script
rescatados a `odoo-deploy` y a `medicalcables-contab`.

### 🔴 2.595 líneas sin versionar en gestionnoofit

Entre el 10 y el 13/09 se parcheó `/opt/gestionnoofit_api` en caliente sin devolver los
cambios al repo. Seis ficheros existían solo en el servidor —incluido
`app/comunicaciones.py` (517 líneas) y el cron que lo alimenta cada 10 minutos— y nueve
más tenían 1.044 líneas de diferencia.

Entre esos cambios había un **arreglo de control de acceso**: un `partner_id` vacío
saltaba la comprobación de permisos entera en vez de negarla, de modo que un distribuidor
podía ver documentos ajenos. Estaba corregido en producción y en ningún sitio más.

**Resuelto**: rescatado (`3ad490b`). `contab estado` vigila ahora este proyecto y el
respaldo diario del PC se trae el código del VPS.

### 🟠 Dos documentos del mismo repo se contradicen

`MAPA_EMPRESAS.md` se declara «fuente de verdad» y dice que la contabilidad de AUSTRAL
está en `cararjfam_test` company 4. `RECOVERY.md §56` dice, en mayúsculas, que ya no está
en este servidor: migró a `wiems_v18_prod` company 12 en agosto.

Manda `RECOVERY.md`. `MAPA_EMPRESAS.md` lleva sin actualizarse desde el **12/06/2026** y le
faltan dos instancias enteras de Odoo 18. **Pendiente de reescribir.**

Esa desactualización tuvo consecuencias: la regla «esta app es solo AUSTRAL company 4»
sobrevivió meses en el `CLAUDE.md` de `wiemspro-contab` apuntando a un registro que hoy
tiene `activa = 0`.

### 🟠 Documentación que describe otro proyecto

`wiemspro-contab` y `medicalcables-contab` se crearon copiando `austral-contab-web`. Sus
`README.md`, `ROADMAP.md`, `docs/ARCHITECTURE.md`, `docs/API.md` y `docs/DEV_VPS.md`
**tienen un solo commit: el del scaffold**, y nunca se actualizaron.

El `README` de `wiemspro-contab` decía *«esqueleto inicial, sin lógica funcional todavía»*
con 428 commits en producción detrás.

**Resuelto a medias**: los `CLAUDE.md` —que es lo que Claude lee automáticamente— están
reescritos con datos verificados. Los otros cinco documentos de cada repo siguen como
estaban, señalados como no fiables en un apartado «Estado de la documentación».

### 🟡 Un servicio encendido que nadie alcanzaba

`austral-contab-web` (:5000) llevaba meses activo **sin ningún dominio apuntando a él**.
Era la web original de Austral sobre Odoo 17, sustituida en agosto.

Además, en `/etc/nginx/sites-enabled/` había un `austral.carajfam.com.bak_audit` del
03/07 que declaraba el mismo `server_name` que el bueno y apuntaba a ese servicio. nginx
lo ignoraba por conflicto, pero si alguien hubiera tocado el fichero real, el `.bak`
habría tomado el relevo sirviendo una web retirada con `cararjfam` y `bt` activas.

**Resuelto**: servicio detenido y deshabilitado, `.bak_audit` retirado (copia en
`/root/nginx-retirados/`), nginx recargado sin avisos.

### 🟡 Otros

- **`SoloCarlos`** (company 11) fue una prueba y sigue con `activa = 1` en la tabla
  `empresa`, que es el campo que controla el acceso a la web. Sin usuarios ni pipeline.
- **`wellhub_integration`** es un addon de Odoo con **0 ficheros**.
- **`maqueta_tienda`** no entra en el backup diario: su dueño es `odoo18` y el script solo
  respalda las BD del usuario `odoo`. Es una maqueta, así que probablemente esté bien.
- **`webround-seguridad`** es un repositorio público **vacío**, sin un solo commit.
- Las 15 ramas `feat/` de `austral-contab-web` están **todas fusionadas**: son punteros de
  mayo y junio, no trabajo pendiente.

---

## Qué se corrigió

| Acción | Dónde |
|---|---|
| 4 repositorios privados creados | `round_config_api`, `round_mcp`, `gn_scripts`, `austral-contab` |
| 3 pipelines + 2 addons de Odoo rescatados | `odoo-deploy` (`cae2524`) |
| 2.595 líneas rescatadas | `gestionnoofit` (`3ad490b`) |
| Scripts de sync de CARARJFAM rescatados | `medicalcables-contab` (`32cbcfb`) |
| Copias de `automation` y `automation_austral` refrescadas | eran del 06/06 |
| `contab estado` pasa de vigilar 5 piezas a **10** | `wiemspro-contab/tools/contab.sh` |
| Respaldo diario ampliado: chats ↔ servidor **y** código del VPS | `respaldo-chats.ps1` |
| 7 `CLAUDE.md` escritos o reescritos | uno por repo |
| Servicio retirado apagado y vhost fantasma eliminado | VPS |

---

## Las decisiones que necesitaban una persona

Seis preguntas que el código no podía responder, resueltas el mismo 17/09/2026:

| Pregunta | Decisión | Estado |
|---|---|---|
| `MAPA_EMPRESAS.md`, ¿reescribir o retirar? | Reescribir | ✅ Hecho, con las 7 empresas verificadas |
| `SoloCarlos`, ¿`activa = 0`? | Sí | ✅ Hecho, 0 usuarios afectados, copia previa en `/root` |
| `automation_bt` y `automation_cf`, ¿se retoman? | Sí | 📌 Decisión anotada; la migración queda pendiente de ejecutar |
| Los 5 ficheros obsoletos de Medical, ¿refrescar? | **No**: conservarlos por si aparece algo que solo estaba en ellos | ✅ Documentados uno a uno |
| `webround-seguridad` y `wellhub_integration`, ¿borrar? | Sí | ✅ Addon retirado · ⏳ el repo necesita permiso `delete_repo` |
| Los 5 documentos heredados, ¿qué hacer? | Borrar 4, reescribir el README | ✅ Hecho en los 3 repos afectados |

### Sobre los pipelines nuevos — corrección

Durante la auditoría di por sentado que `automation_bt` y `automation_cf` eran un port
abandonado, porque no tienen crons ni logs. **Era falso.** `cron_cola_vps.py` lee
`empresa.pipeline_dir` y ejecuta `<pipeline_dir>/webproc.py`: las subidas por la web las
procesan **ya** los pipelines nuevos, mientras la cola de Drive sigue en los viejos.
CARARJFAM y BT corren las dos generaciones a la vez, cada una para una vía de entrada.

El paso pendiente de la migración es mover también el flujo de Drive, **apagando los crons
antiguos en la misma operación**.

### Lo único que queda abierto

- **Borrar el repo `webround-seguridad`** (público y vacío). Necesita ampliar el token:
  `gh auth refresh -h github.com -s delete_repo`, o hacerlo desde la web de GitHub.
- **Ejecutar la migración** de la cola de Drive a `automation_cf` y `automation_bt`.

---

## La lección que se repite

Tres de los hallazgos graves tienen la misma forma: **algo se hizo bien en el servidor y
nunca volvió al repositorio**. La regla estaba escrita —el `CLAUDE.md` de `gestionnoofit`
dice «❌ Editar directamente en VPS sin pasar por git», y `TRABAJO_CONCURRENTE.md` avisa de
que los parches en caliente desaparecen— pero **nada la comprobaba**.

Lo que se ha puesto no es una regla más, es una comprobación: `contab estado` ahora compara
las diez piezas y enseña las desviaciones. Una regla que nadie verifica es una intención;
una comprobación es una red.
