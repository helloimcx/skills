#!/usr/bin/env python3
"""Resident worker agent: WS command channel for this machine.

Connects to the CP server (URL from the machine-global worker config, token
from CODE_LOOP_CP_TOKEN or the credentials file), receives pushed commands —
e.g. a dashboard review click — and executes each entirely on this machine
(registration guard -> claim ttl=300 -> worktree -> bind -> spawn the adopting
reviewer session), then acks the ledger over HTTP. Reconnects with backoff;
offline periods lose nothing (commands queue on the ledger until reconnect
catch-up or TTL expiry).
"""
import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
if str(SERVICE) not in sys.path:
    sys.path.insert(0, str(SERVICE))

from codeloop.agent import main

if __name__ == '__main__':
    sys.exit(main())
