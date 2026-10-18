"""Serve-side operator data plane (review MAJOR-3): the loopback endpoint that
lets a session process ask the serve process to run an L1 act it cannot run
itself. The session has no executor — collect_refresh / command_resend ride
this process's dispatcher and refresh closure — so wired acts are forwarded
here; local-only verbs (no_op / sweep_expired) still run in-session against
the store.

Fencing is two-layered: the machine token gates the HTTP transport (the
Handler only reaches us after auth), and face.act re-checks the live
execution token + terminal state on the machine-global operator store.
Unwired verbs fail honestly with OPERATOR_VERB_NOT_WIRED. Kept out of
cp/server.py to hold that module under the single-file line redline; the
dependency stays `cp -> operator` via the lazy import in the route.

External API: serve_act(handler) — the Handler calls it with itself when
POST /api/v1/operator/act arrives; the envelope shape matches every other
server verb (envelope_ok / envelope_fail from codeloop.cp.protocol).
"""
from ..cp.protocol import envelope_fail, envelope_ok


def serve_act(handler):
    """Run one operator L1 act requested over the data plane. `handler` is the
    CP server Handler (provides _body / _send)."""
    from ..operator import face as op_face
    from ..operator import store as op_store
    body = handler._body()
    execution = str(body.get('execution') or '')
    token = body.get('token')
    verb = str(body.get('verb') or '')
    if (not execution or isinstance(token, bool)
            or not isinstance(token, int) or not verb):
        handler._send(200, envelope_fail('OPERATOR_ACT_INVALID',
                                         'execution/token/verb required'))
        return
    store = op_store.open_store()
    try:
        receipt = op_face.act(store, execution, token, verb,
                              target=str(body.get('target') or ''),
                              detail=str(body.get('detail') or ''))
    except ValueError as exc:
        handler._send(200, envelope_fail(type(exc).__name__, str(exc)))
        return
    finally:
        store.db.close()
    handler._send(200, envelope_ok({'verb': verb, 'receipt': receipt}))