"""Tests for the payment-success-email dedup mechanism.

Background
----------
Whop's checkout fires payment activation through TWO concurrent paths:

  * ``ls_success`` — synchronous redirect, runs the moment the user is
    bounced back from Whop with ``?membership_id=…``.
  * ``ls_webhook`` (``action == 'membership.went_valid'``) — async POST
    from Whop's servers. Can land before, after, or *instead of* the
    redirect (closed browser, off-platform upgrade from the Whop
    dashboard, plan switch from billing portal, ``ls_success``
    membership-verification failure).

Before this fix, ONLY the redirect path called
``_fire_payment_success_email``. The webhook path activated the
subscription in the DB but never sent the receipt — so any user who
closed their browser before the redirect ran (or any off-platform
upgrade) silently activated without ever getting a "Payment received"
email.

Fix
---
Both paths now route through ``_maybe_fire_payment_success_email``,
which dedupes on a (user_id, membership_id, plan) tuple stamped on
the user JSONB:

  * First call → fires email + stamps user.
  * Second call with same (mem_id, plan) → silent no-op (race winner
    already sent it).
  * Plan change on same mem_id → fires fresh receipt (real upgrade).
  * Routine renewal (same mem_id, same plan, fired by Whop on every
    billing cycle) → no spam.

These tests pin every branch of that decision logic. They run with
``SimpleTestCase`` (no DB), mock ``get_user_by_id`` / ``update_user``
/ ``_fire_payment_success_email`` directly on the views module, so no
threads spawn and no real email transport is touched.
"""
from unittest.mock import patch
from django.test import SimpleTestCase, RequestFactory


class PaymentEmailDedupTests(SimpleTestCase):
    """Pin the (mem_id, plan) dedup contract on
    ``views._maybe_fire_payment_success_email``."""

    def _call(self, user_record, *, mem_id, plan, user_id=999):
        """Drive the helper with a fully-stubbed user record. Returns
        ``(sent_bool, mock_fire_email, mock_update_user)`` so each test
        can assert on dispatch + stamp behaviour."""
        from core import views

        request = RequestFactory().get('/')

        with patch.object(views, 'get_user_by_id',
                          return_value=user_record) as mock_get, \
             patch.object(views, 'update_user') as mock_update, \
             patch.object(views, '_fire_payment_success_email') as mock_fire:
            sent = views._maybe_fire_payment_success_email(
                user_id=user_id,
                mem_id=mem_id,
                plan=plan,
                mem_data={'id': mem_id},
                request=request,
            )
        # Every call path that does anything reads the user once.
        if user_id:
            mock_get.assert_called_once_with(user_id)
        return sent, mock_fire, mock_update

    # ── Happy paths ──────────────────────────────────────────────────

    def test_first_activation_sends_and_stamps(self):
        """No prior stamps on the user → email fires + user gets stamped
        with both keys so the matching webhook (or redirect) no-ops."""
        user = {'id': 999, 'email': 'x@y.com'}
        sent, fire, upd = self._call(user, mem_id='mem_abc', plan='pro')

        self.assertTrue(sent)
        fire.assert_called_once()
        upd.assert_called_once()
        kw = upd.call_args.kwargs
        self.assertEqual(kw['payment_email_last_membership'], 'mem_abc')
        self.assertEqual(kw['payment_email_last_plan'], 'pro')

    def test_duplicate_redirect_webhook_race_is_suppressed(self):
        """Same (mem_id, plan) already stamped → no email, no DB write.
        This is the race-winner-already-sent case the dedup exists for."""
        user = {
            'id': 999, 'email': 'x@y.com',
            'payment_email_last_membership': 'mem_abc',
            'payment_email_last_plan':       'pro',
        }
        sent, fire, upd = self._call(user, mem_id='mem_abc', plan='pro')

        self.assertFalse(sent)
        fire.assert_not_called()
        upd.assert_not_called()

    # ── Real plan / membership change still re-sends ─────────────────

    def test_plan_change_on_same_membership_re_sends(self):
        """Pro → Agency on the same mem_id → fresh receipt fires
        (genuine upgrade, customer should get the new amount + plan
        label in their inbox)."""
        user = {
            'id': 999, 'email': 'x@y.com',
            'payment_email_last_membership': 'mem_abc',
            'payment_email_last_plan':       'pro',
        }
        sent, fire, upd = self._call(user, mem_id='mem_abc', plan='agency')

        self.assertTrue(sent)
        fire.assert_called_once()
        kw = upd.call_args.kwargs
        self.assertEqual(kw['payment_email_last_membership'], 'mem_abc')
        self.assertEqual(kw['payment_email_last_plan'], 'agency')

    def test_new_membership_id_re_sends(self):
        """Whop renewal/upgrade flows that issue a NEW mem_id → fresh
        receipt fires. Some Whop product configurations rotate the
        membership_id on every renewal — we still want to receipt
        those, so the stamp is per-mem AND per-plan, not just per-plan."""
        user = {
            'id': 999, 'email': 'x@y.com',
            'payment_email_last_membership': 'mem_abc',
            'payment_email_last_plan':       'pro',
        }
        sent, fire, upd = self._call(user, mem_id='mem_xyz', plan='pro')

        self.assertTrue(sent)
        fire.assert_called_once()
        kw = upd.call_args.kwargs
        self.assertEqual(kw['payment_email_last_membership'], 'mem_xyz')
        self.assertEqual(kw['payment_email_last_plan'], 'pro')

    # ── Defensive paths ──────────────────────────────────────────────

    def test_missing_user_id_is_silent_noop(self):
        """``uid`` resolution can fail in the webhook (no metadata user_id,
        no email match). Helper must return False, never touch the DB,
        never raise — so the webhook returns 200 instead of looping."""
        from core import views

        request = RequestFactory().get('/')
        with patch.object(views, 'get_user_by_id') as mock_get, \
             patch.object(views, 'update_user') as mock_update, \
             patch.object(views, '_fire_payment_success_email') as mock_fire:
            sent = views._maybe_fire_payment_success_email(
                user_id=None,
                mem_id='mem_abc',
                plan='pro',
                mem_data={},
                request=request,
            )

        self.assertFalse(sent)
        mock_get.assert_not_called()
        mock_update.assert_not_called()
        mock_fire.assert_not_called()

    def test_empty_membership_id_falls_through_to_send(self):
        """Comment in source: if we couldn't identify the membership at
        all (empty mem_id), better one extra email than miss the
        activation entirely. Pin that fall-through so a future
        refactor that adds an early-return on empty mem doesn't
        silently regress activation receipts."""
        user = {
            'id': 999, 'email': 'x@y.com',
            'payment_email_last_membership': '',
            'payment_email_last_plan':       '',
        }
        sent, fire, _upd = self._call(user, mem_id='', plan='starter')

        self.assertTrue(sent)
        fire.assert_called_once()
