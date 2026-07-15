"""Auto-login por token firmado (HS256) para las webs contables.

La web (austral-contab-web) genera enlaces:
    /round/autologin?token=<payload_b64>.<sig_b64>&redirect=/web%23id=...

payload JSON: {"db": ..., "login": ..., "exp": epoch, "aud": "round-autologin"}
Firma: HMAC-SHA256 con el secreto compartido de /etc/odoo17.conf:

    [round_autologin]
    secret = ...

El módulo se carga server-wide (server_wide_modules) para servir la ruta
sin base de datos seleccionada. Si el token es inválido/caducado se cae
al login normal de Odoo.
"""
import base64
import hashlib
import hmac
import json
import logging
import time

from odoo import SUPERUSER_ID, api, http
from odoo.http import request
from odoo.modules.registry import Registry
from odoo.tools import config

_logger = logging.getLogger(__name__)


def _b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class RoundAutologin(http.Controller):

    @http.route("/round/autologin", type="http", auth="none", csrf=False)
    def autologin(self, token=None, redirect="/web", **kw):
        # redirect solo relativo (request.redirect(local=True) lo refuerza)
        if not redirect.startswith("/"):
            redirect = "/web"
        secret = (config.misc.get("round_autologin") or {}).get("secret")
        if not secret or not token or token.count(".") != 1:
            return request.redirect("/web/login")
        try:
            payload_b64, sig_b64 = token.split(".")
            expected = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest()
            given = _b64d(sig_b64)
            if not hmac.compare_digest(expected, given):
                raise ValueError("firma inválida")
            payload = json.loads(_b64d(payload_b64))
            if payload.get("aud") != "round-autologin":
                raise ValueError("aud")
            if int(payload.get("exp", 0)) < time.time():
                raise ValueError("caducado")
            db, login = payload["db"], payload["login"]
        except Exception as e:
            _logger.warning("round_autologin: token rechazado (%s)", e)
            return request.redirect("/web/login")

        if db not in http.db_list(force=True):
            return request.redirect("/web/login")

        # sesión ya iniciada en esa BD → directo
        if request.session.db == db and request.session.uid:
            return request.redirect(redirect)

        registry = Registry(db)
        with registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            user = env["res.users"].sudo().search(
                [("login", "=", login), ("active", "=", True)], limit=1)
            if not user:
                _logger.warning("round_autologin: login %s no existe en %s", login, db)
                return request.redirect("/web/login")
            request.session.pre_login = user.login
            request.session.pre_uid = user.id
            env_user = env(user=user.id)
            request.session.finalize(env_user)
            # Rotación manual: la ruta no tiene BD asociada, así que la rotación
            # post-dispatch NO recalcularía session_token sobre el sid nuevo
            # (env vacío) y la sesión nacería inválida. Rotamos aquí con el env
            # correcto y desactivamos la rotación automática.
            http.root.session_store.rotate(request.session, env_user)
            request.session.should_rotate = False
            _logger.info("round_autologin: sesión abierta db=%s login=%s", db, login)

        return request.redirect(redirect)
