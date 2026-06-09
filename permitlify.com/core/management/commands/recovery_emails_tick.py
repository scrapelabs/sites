"""Dispatch any due recovery emails. Designed to be called from cron
every ~15 minutes.

Reads pending rows from ``recovery_queue`` whose ``fire_at <= now()``,
renders each one with the admin-configured template snapshot stored on
the row (so live template edits never corrupt already-queued sends),
and dispatches via ``core.email_service.send_email``. Rows are marked
``sent`` / ``failed`` / ``skipped`` so they never re-fire.

Skips rules:
  * The user has already converted (paid) since the row was queued.
  * The user's account no longer exists.
  * The user has opted out of marketing email (``user.data.marketing_unsub = True``).
"""
from __future__ import annotations

import logging
import time
from django.core.management.base import BaseCommand

log = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Dispatch due rows from the recovery_queue table."

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=200,
                            help='Max rows to process per tick (default 200).')
        parser.add_argument('--dry-run', action='store_true',
                            help='Log what would be sent without dispatching.')

    def handle(self, *args, **opts):
        from core import db
        from core.email_service import send_email

        limit = int(opts.get('limit') or 200)
        dry   = bool(opts.get('dry_run'))

        due = db.recovery_due_rows(limit=limit)
        if not due:
            self.stdout.write("recovery_emails_tick: nothing due.")
            return

        sent = skipped = failed = 0
        t0 = time.time()
        for row in due:
            rid  = row['id']
            uid  = row['user_id']
            step = row['step']
            try:
                user = db.get_user_by_id(uid) or {}
                if not user:
                    db.recovery_mark(rid, 'skipped', note='user missing')
                    skipped += 1
                    continue

                # get_user_by_id returns a flattened doc (JSONB merged in
                # at top level), so look for the unsubscribe flag directly
                # on the dict — not under a nested .data key.
                if user.get('subscription_active') or user.get('marketing_unsub'):
                    db.recovery_mark(rid, 'skipped',
                                     note='paid or unsubscribed')
                    skipped += 1
                    continue

                tpl   = row.get('template') or {}
                subj  = tpl.get('subject', '')
                body  = tpl.get('body', '')
                if not subj or not body:
                    db.recovery_mark(rid, 'skipped', note='empty template')
                    skipped += 1
                    continue

                # Variable substitution — keep tiny and predictable so
                # admins don't accidentally template-inject themselves.
                first_name = (user.get('name') or '').split(' ', 1)[0] or 'there'
                ctx = {
                    'name':       first_name,
                    'email':      user.get('email', ''),
                    'trial_link': row.get('trial_link')
                                  or 'https://permitlify.com/signup/',
                }
                for k, v in ctx.items():
                    subj = subj.replace('{{' + k + '}}', str(v))
                    body = body.replace('{{' + k + '}}', str(v))

                if dry:
                    self.stdout.write(
                        f"DRY id={rid} → {user.get('email')} step={step} "
                        f"subj={subj!r}")
                    continue

                from core.views import _render_recovery_html
                ok, err = send_email(
                    svc_key='recovery',
                    to=user.get('email', ''),
                    subject=subj,
                    body_text='',
                    body_html=_render_recovery_html(subj, body),
                )
                if ok:
                    db.recovery_mark(rid, 'sent')
                    sent += 1
                else:
                    db.recovery_mark(rid, 'failed', note=str(err)[:240])
                    failed += 1
            except Exception as exc:  # noqa: BLE001
                log.exception("recovery_emails_tick row %s failed", rid)
                try:
                    db.recovery_mark(rid, 'failed', note=str(exc)[:240])
                except Exception:
                    pass
                failed += 1

        self.stdout.write(
            f"recovery_emails_tick: {sent} sent · {skipped} skipped · "
            f"{failed} failed · in {time.time()-t0:.2f}s "
            f"({'DRY' if dry else 'LIVE'})")
