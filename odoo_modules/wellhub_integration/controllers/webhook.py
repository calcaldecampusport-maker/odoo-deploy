import hashlib
import hmac
import json
import logging
from datetime import datetime

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class WellhubWebhookController(http.Controller):
    """Endpoint público para recibir webhooks de Wellhub.

    URL: https://erp.carajfam.com/api/wellhub/checkin
    Método: POST
    Headers:
      - X-Gympass-Signature: HMAC-SHA1(body, secret) en hex
      - Content-Type: application/json
    """

    @http.route(
        "/api/wellhub/checkin",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        save_session=False,
    )
    def checkin(self, **kwargs):
        body_bytes = request.httprequest.get_data() or b""
        signature_header = request.httprequest.headers.get("X-Gympass-Signature", "")

        # Parsear payload
        try:
            payload = json.loads(body_bytes.decode("utf-8"))
        except Exception as e:
            _logger.warning("Wellhub webhook: invalid JSON: %s", e)
            return request.make_response(
                json.dumps({"error": "invalid_json"}),
                headers=[("Content-Type", "application/json")],
                status=400,
            )

        # Identificar empresa: por location_id o por company_id en payload
        location_id = (
            payload.get("location_id")
            or payload.get("location", {}).get("id")
            or payload.get("gym", {}).get("id")
        )

        # Buscar config (cross-company porque webhook entra sin auth)
        env_sudo = request.env(su=True)
        cfg = env_sudo["wellhub.config"].search([
            ("active", "=", True),
            ("location_id", "=", str(location_id) if location_id else False),
        ], limit=1)
        # Fallback: si no hay location match, usar la única config activa
        if not cfg:
            cfg = env_sudo["wellhub.config"].search([("active", "=", True)], limit=1)
        if not cfg:
            _logger.warning("Wellhub webhook: no wellhub.config activa para location_id=%s", location_id)
            return request.make_response(
                json.dumps({"error": "no_config"}),
                headers=[("Content-Type", "application/json")],
                status=400,
            )

        # Validar firma HMAC-SHA1
        signature_valid = False
        if cfg.webhook_secret:
            digest = hmac.new(
                cfg.webhook_secret.encode("utf-8"),
                body_bytes,
                hashlib.sha1,
            ).hexdigest()
            signature_valid = hmac.compare_digest(digest, signature_header)
            if not signature_valid:
                _logger.warning(
                    "Wellhub webhook: firma INVÁLIDA. expected=%s got=%s",
                    digest, signature_header,
                )

        # Extraer campos del payload
        user_token = (
            payload.get("user", {}).get("unique_token")
            or payload.get("unique_token")
            or payload.get("user_id")
            or ""
        )
        event_id = (
            payload.get("event_id")
            or payload.get("id")
            or payload.get("checkin_id")
            or f"{user_token}-{payload.get('checkin_at', '')}"
        )
        checkin_at_raw = (
            payload.get("checkin_at")
            or payload.get("timestamp")
            or payload.get("created_at")
        )
        try:
            checkin_at = (
                datetime.fromisoformat(checkin_at_raw.replace("Z", "+00:00"))
                if checkin_at_raw else datetime.utcnow()
            )
        except Exception:
            checkin_at = datetime.utcnow()

        # Idempotencia por event_id
        Checkin = env_sudo["wellhub.checkin"]
        existing = Checkin.search([
            ("company_id", "=", cfg.company_id.id),
            ("event_id", "=", event_id),
        ], limit=1)
        if existing:
            _logger.info("Wellhub webhook: event_id=%s ya existe (idempotente)", event_id)
            return request.make_response(
                json.dumps({"status": "duplicate", "checkin_id": existing.id}),
                headers=[("Content-Type", "application/json")],
                status=200,
            )

        # Solo crear el check-in si la firma es válida (o si no hay secret configurado en modo desarrollo)
        if cfg.webhook_secret and not signature_valid:
            # Aún así registramos con flag para auditoría
            ck = Checkin.create({
                "company_id": cfg.company_id.id,
                "checkin_at": checkin_at,
                "user_unique_token": user_token or "INVALID-SIGNATURE",
                "location_id": str(location_id) if location_id else False,
                "event_id": event_id,
                "raw_payload": json.dumps(payload, ensure_ascii=False)[:5000],
                "signature_valid": False,
            })
            return request.make_response(
                json.dumps({"error": "invalid_signature", "logged_id": ck.id}),
                headers=[("Content-Type", "application/json")],
                status=401,
            )

        # Crear check-in y entrada puntual asociada
        ck = Checkin.create({
            "company_id": cfg.company_id.id,
            "checkin_at": checkin_at,
            "user_unique_token": user_token,
            "location_id": str(location_id) if location_id else False,
            "event_id": event_id,
            "raw_payload": json.dumps(payload, ensure_ascii=False)[:5000],
            "signature_valid": True,
        })
        ep = env_sudo["entrada.puntual"].create({
            "company_id": cfg.company_id.id,
            "fecha": checkin_at,
            "fuente": "wellhub",
            "usuario_uuid": user_token,
            "precio": cfg.precio_unitario_default or 0,
            "wellhub_checkin_id": ck.id,
            "notes": "Check-in automático via webhook",
        })
        ck.write({"entrada_puntual_id": ep.id})

        # Actualizar contadores en config
        cfg.write({
            "last_checkin_at": checkin_at,
            "total_checkins": cfg.total_checkins + 1,
        })

        return request.make_response(
            json.dumps({"status": "ok", "checkin_id": ck.id, "entrada_id": ep.id}),
            headers=[("Content-Type", "application/json")],
            status=200,
        )
