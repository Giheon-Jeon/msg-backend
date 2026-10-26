"""Render the two authenticated preview paths into an existing KOTH Caddy route."""
import base64
import hashlib
import ipaddress
import re
from urllib.parse import urlsplit
from uuid import UUID


def render_preview_gateway(original, challenge_id, origin, upstream, bootstrap):
    if str(UUID(challenge_id)) != challenge_id:
        raise ValueError("canonical challenge UUID required")
    parsed = urlsplit(origin)
    if (parsed.scheme != "https" or parsed.netloc != parsed.hostname or parsed.path
            or not re.fullmatch(r"[a-z0-9.-]+", parsed.hostname or "")):
        raise ValueError("canonical HTTPS origin required")
    host, port = upstream.rsplit(":", 1)
    ipaddress.ip_address(host)
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("valid private upstream required")
    if "msg-koth-admin-preview-v1" in original:
        raise ValueError("preview already installed; inspect before replacing")
    script = bootstrap.split("<script>", 1)[1].split("</script>", 1)[0]
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    entry = f'''
  # msg-koth-admin-preview-v1 entry begin
  @preview_entry_get {{
   path /__operator/preview
   method GET
  }}
  handle @preview_entry_get {{
   header Cache-Control "no-store, private"
   header Content-Security-Policy "default-src 'none'; script-src 'sha256-{digest}'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
   root * /etc/msgctf-koth
   rewrite * /admin-preview.html
   file_server
  }}
  @preview_entry_post {{
   path /__operator/preview
   method POST
  }}
  handle @preview_entry_post {{
   request_body {{
    max_size 4KB
   }}
   reverse_proxy https://msgctf.kr {{
    rewrite /api/v1/koth/preview_sessions/authorize
    header_up X-KOTH-Preview-Challenge {challenge_id}
    header_up X-KOTH-Preview-Origin {origin}
    header_up -Cookie
    header_up -Authorization
    transport http {{
     dial_timeout 2s
     response_header_timeout 3s
    }}
   }}
  }}
  # msg-koth-admin-preview-v1 entry end
'''
    session = f'''
  # msg-koth-admin-preview-v1 session begin
  @preview_session expression `{{http.request.cookie.__Host-msg_koth_preview}} != ""`
  handle @preview_session {{
   forward_auth https://msgctf.kr {{
    uri /api/v1/koth/preview_sessions/authorize
    header_up X-KOTH-Preview-Challenge {challenge_id}
    header_up X-KOTH-Preview-Origin {origin}
    header_up Cookie "__Host-msg_koth_preview={{http.request.cookie.__Host-msg_koth_preview}}"
    header_up -Authorization
    transport http {{
     dial_timeout 2s
     response_header_timeout 3s
    }}
   }}
   reverse_proxy {upstream} {{
    transport http {{
     response_header_timeout 160s
    }}
   }}
  }}
  # msg-koth-admin-preview-v1 session end
'''
    # Keep internal-path denial, source-IP checks and the release window in place.
    original, count = re.subn(r"(?m)^(\s*)@internal path ", lambda m:entry + m.group(0), original, count=1)
    if count != 1:
        raise ValueError("existing internal-path boundary not found")
    original, count = re.subn(r'(?m)^(\s*)respond "?KOTH_NOT_OPEN"? 403', lambda m:session + m.group(0), original, count=1)
    if count != 1:
        raise ValueError("existing release boundary not found")
    # Never expose the administrator's grant to a deliberately vulnerable game.
    original, count = re.subn(r"(reverse_proxy " + re.escape(upstream) + r"\s*\{)",
        lambda m:m.group(1) + '\n header_up Cookie "(^|; *)__Host-msg_koth_preview=[^;]*" ""', original)
    if count < 2:
        raise ValueError("game proxy boundaries not found")
    return original
