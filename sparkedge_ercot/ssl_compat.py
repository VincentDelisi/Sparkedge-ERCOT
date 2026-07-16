"""Optional, CAISO-scoped TLS relaxation.

Background
----------
gridstatus fetches CAISO data over two different paths:

* OASIS (``oasis.caiso.com``) via ``requests`` -> LMPs and gas prices.
* The public "outlook" endpoints (``www.caiso.com/outlook/...``) which are read
  straight into pandas via ``urllib``/``pandas.read_csv`` -> load & fuel mix.

On some networks (corporate proxies, HTTPS-inspecting antivirus such as
Kaspersky/ESET, or certain VPNs) the TLS chain for these hosts is re-signed by a
local certificate the Python trust store doesn't recognise, producing::

    ssl.SSLCertVerificationError: CERTIFICATE_VERIFY_FAILED
    (self-signed certificate in certificate chain)

The proper fix is to install the intercepting proxy's root CA into the trust
store. But that's often not possible on a locked-down machine, and the CAISO
data is public anyway -- so we offer an *opt-in* relaxation that disables TLS
verification **for CAISO hosts only**, leaving every other HTTPS request fully
verified.

This module is a no-op unless ``Settings.insecure_ssl_caiso`` is True
(env ``SPARKEDGE_INSECURE_SSL=1``). It is idempotent.
"""

from __future__ import annotations

import logging
import ssl
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Hosts we are willing to skip verification for. Kept deliberately narrow.
# ERCOT data is served from ercot.com; caiso.com is retained harmlessly.
_CAISO_HOSTS = ("ercot.com", "caiso.com")

_installed = False


def _is_caiso(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return any(host == h or host.endswith("." + h) for h in _CAISO_HOSTS)


def install(enabled: bool) -> None:
    """Install the CAISO-scoped SSL relaxation if ``enabled``.

    Patches both transport layers gridstatus uses:
      1. ``requests.Session.request`` -> forces verify=False for CAISO URLs.
      2. ``ssl._create_default_https_context`` -> unverified context, which is
         what ``urllib``/``pandas.read_csv`` use for the outlook CSV endpoints.

    We cannot cleanly scope (2) per-host (urllib builds the context globally),
    so we only touch it when the user has explicitly opted in. (1) stays
    host-scoped regardless.
    """
    global _installed
    if not enabled or _installed:
        return

    # --- 1. requests: host-scoped verify=False ---------------------------- #
    try:
        import requests

        _orig_request = requests.Session.request

        def _patched_request(self, method, url, *args, **kwargs):  # type: ignore
            if _is_caiso(url):
                kwargs.setdefault("verify", False)
            return _orig_request(self, method, url, *args, **kwargs)

        requests.Session.request = _patched_request  # type: ignore[assignment]

        # Silence the noisy InsecureRequestWarning that urllib3 emits.
        try:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:  # pragma: no cover
            pass
    except Exception as exc:  # pragma: no cover
        log.warning("could not patch requests for CAISO SSL: %s", exc)

    # --- 2. urllib / pandas.read_csv: unverified default context ---------- #
    # The outlook load/fuel-mix CSVs are opened with urllib, which honours the
    # module-level default HTTPS context.
    try:
        ssl._create_default_https_context = ssl._create_unverified_context  # type: ignore[attr-defined]
    except Exception as exc:  # pragma: no cover
        log.warning("could not relax default SSL context: %s", exc)

    _installed = True
    log.warning(
        "TLS verification RELAXED for CAISO hosts (SPARKEDGE_INSECURE_SSL). "
        "This is scoped to public CAISO data; all other HTTPS stays verified."
    )
