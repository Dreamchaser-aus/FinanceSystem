"""Per-platform colours and badges, so an admin can tell which platform a row belongs to.

Provides template helpers (no schema change, nothing written):
  pf_color(platform_id) -> hex colour, stable per platform (position of its id among all platform ids)
  pf_chip(platform_id)  -> small coloured chip with the platform name (HTML-escaped)
  pf_all                -> True when an admin is viewing "All Platforms"; chips are shown only then
  pf_active_color       -> colour of the single platform currently selected (None for All / anonymous)

Registered as a nested blueprint of auto_closing_stage7 (like store_balance.py), so app.py is untouched.
Any failure falls back to harmless no-ops so pages never break because of a badge.
"""
from flask import Blueprint, current_app, g, session
from flask_login import current_user
from markupsafe import Markup, escape

bp = Blueprint('platform_badges', __name__)

# Distinct hues; red/green are avoided because they already mean debit/credit in the tables.
PALETTE = ['#2563eb', '#0d9488', '#d97706', '#7c3aed', '#db2777',
           '#4f46e5', '#ea580c', '#0891b2', '#65a30d', '#9333ea']


def _shade(hex_color, factor):
    r, gr, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return '#%02x%02x%02x' % (int(r * factor), int(gr * factor), int(b * factor))


def _platform_map():
    """{platform_id: (name, colour)}, loaded once per request."""
    cached = getattr(g, '_pf_map', None)
    if cached is None:
        from app import Platform
        rows = Platform.query.with_entities(Platform.id, Platform.name).order_by(Platform.id).all()
        cached = {pid: (name, PALETTE[i % len(PALETTE)]) for i, (pid, name) in enumerate(rows)}
        g._pf_map = cached
    return cached


@bp.app_context_processor
def inject_platform_badges():
    noop = {'pf_chip': lambda pid: '', 'pf_color': lambda pid: '#64748b', 'pf_all': False, 'pf_active_color': None}
    try:
        if not current_user.is_authenticated:
            return noop
        pmap = _platform_map()

        def pf_color(pid):
            return pmap.get(pid, ('', '#64748b'))[1]

        def pf_chip(pid):
            name, color = pmap.get(pid, ('Unknown platform', '#64748b'))
            return Markup(
                '<span class="pf-chip" style="--pf:%s;--pf-bg:%s1f;--pf-bd:%s55;--pf-fg:%s" title="Platform: %s">'
                '<i aria-hidden="true"></i>%s</span>' % (color, color, color, _shade(color, .62), escape(name), escape(name)))

        chosen = session.get('platform_id', 'all' if current_user.role == 'admin' else '')
        is_all = current_user.role == 'admin' and chosen == 'all'
        active = pmap.get(chosen)
        return {'pf_chip': pf_chip, 'pf_color': pf_color, 'pf_all': is_all,
                'pf_active_color': active[1] if active else None}
    except Exception:
        current_app.logger.exception('Platform badges failed')
        return noop
