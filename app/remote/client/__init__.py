"""The client side of PC-to-PC linking (PROTOCOL.md section 21).

GrabLine on this PC connects OUT to GrabLine on another PC and is just another
paired device of that PC, the way the Android app is. Nothing in this package
ever listens: it only calls ``connect``. It lives in ``app/remote/`` because
that is the one place allowed to touch sockets (``scripts/check_no_listen.py``).

* ``transport``  one blocking TLS WebSocket connection with the pin check.
* ``pairing``    manual (typed code) pairing with trust on first use.
* ``keys``       the per-link device key and where it is stored.
* ``session``    the thread that keeps one link connected (back-off, addresses).
* ``local``      this PC's own downloads as section 20 reports, and the host's
  commands on them.
* ``service``    ``LinkService``: the GUI-facing API and Qt signals.
"""
