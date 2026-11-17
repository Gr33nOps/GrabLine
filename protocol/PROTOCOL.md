# GrabLine phone-to-PC protocol (version 6)

Status: normative for `app/remote/` (desktop) and the Android app. Fixtures in `protocol/fixtures/` of the desktop repository are part of this document: where prose and a fixture disagree, fix both in one change.

Words: MUST, MUST NOT, SHOULD and MAY are used as in RFC 2119. "PC" is the GrabLine desktop app. "Phone" is GrabLine for Android.

## 1. Overview

| Item | Value |
| --- | --- |
| Roles | The PC listens; the phone always connects out. The phone never listens. |
| Transport | One TLS WebSocket per connection: `wss://<address>:<port>/remote` |
| Encoding | JSON objects in WebSocket text frames, UTF-8. Binary frames only PC to phone, only for `fetch` (section 13). |
| Frame limit | 1,048,576 bytes per frame, both directions (same number as `app/native_host/protocol.py`) |
| Protocol version | `PROTOCOL_VERSION = 6`, the same constant the browser extension's `ping` reports |
| PC identity | A self-signed certificate; the phone pins its public key from the QR code |
| Phone identity | An ECDSA P-256 key pair in Android Keystore; the PC stores only the public key |
| Default port | 47821 (the user can change it in Settings, Phone) |

Connection life cycle:

```
phone                                   PC
  |---- TCP + TLS (pin checked) -------->|
  |<--- hello {nonce, pcId, pcName} -----|
  |---- pair {...}  (first time only) -->|   user presses Allow
  |<--- paired {deviceId, serverProof} --|
  |---- auth {deviceId, signature} ----->|
  |<--- authOk --------------------------|
  |==== requests / replies / events =====|
```

## 2. Transport and framing

| Rule | Detail |
| --- | --- |
| TLS | TLS 1.2 or newer (1.3 preferred). The PC is `QWebSocketServer` in `SecureMode`; the phone is an OkHttp `WebSocket`. |
| Handshake path | `GET /remote`. Any other path: HTTP 404. |
| `Origin` header | A handshake that carries an `Origin` header MUST be refused (HTTP 403). Browsers always send one; the phone never does. |
| Compression | `permessage-deflate` MUST NOT be negotiated. |
| Text frame | Exactly one JSON object. Not an object, invalid JSON, or invalid UTF-8: close `4400`. |
| Phone binary frames | Not allowed. Close `4400`. |
| Size | The PC sets the incoming message limit to 1,048,576 bytes; a larger frame closes that connection with `1009` and no error reply (the frame is never parsed). The listener keeps running (QA-09). Before `authOk` the limit is 4,096 bytes. The PC never sends a frame over the limit; a reply that would exceed it is paged instead. |
| Nesting | JSON nesting deeper than 8 levels: `badRequest`. |
| Keep-alive | The phone sends WebSocket pings every 20 s (`pingInterval`). The PC closes a connection that has sent nothing for 60 s (`1001`). |
| Connections | At most 2 authenticated connections per device (the second is meant for `fetch`). A third closes the oldest with `4409`. |

### Envelope

| Kind | Fields | Notes |
| --- | --- | --- |
| Request (phone to PC) | `id`, `type`, plus the message's own fields | `id` is a string, 1 to 64 characters of `[A-Za-z0-9_-]`, unique among the connection's unanswered requests |
| Reply (PC to phone) | `id` (echoed), `type`, `protocolVersion`, plus fields | Exactly one reply per request. Replies MAY arrive out of order. |
| Error reply | `id` (echoed, absent if the request had no usable id), `type: "error"`, `protocolVersion`, `code`, `message` | Section 16 |
| Event (PC to phone) | `type`, `protocolVersion`, no `id` | `hello`, `pairPending`, `event`, `fetchEnd` |
| Notification (phone to PC) | `type`, no `id`, no reply | Only `fetchAck` |

The phone MUST time out a request that gets no reply: 15 s by default, 45 s for `analyze` and `fileHash`, 90 s for `pair`.

### Types and strictness

| Type name | Meaning |
| --- | --- |
| string | JSON string. Limits are in UTF-16 code units after trimming. No NUL characters. |
| int | JSON number with no fraction, in the range -(2^53 - 1) to 2^53 - 1. JSON `true`/`false` are NOT ints (Python: test `type(v) is int`, not `isinstance`). |
| bool | JSON `true` or `false` only |
| time | int, Unix seconds, UTC |
| bytes | string holding base64url without padding (RFC 4648 section 5). Standard base64 or padding is rejected. |
| url | string, at most 8,192 characters, scheme `http` or `https` with a host, or `magnet:` with `xt=` in the query (the rule in `host.py` `_valid_url`) |

| Situation | Result |
| --- | --- |
| Unknown field | Ignored, except the path-bearing names below |
| Field of the wrong type, out of range, or failing its pattern | `badRequest`. Nothing is truncated or coerced. |
| Required field missing | `badRequest` |
| List longer than its limit | `tooLarge` |
| Unknown `type` | `unsupported` |
| Any field named `path`, `folder`, `dir`, `destDir`, `dest_dir`, `filename`, `file`, `saveAs` in a phone request | `forbidden`. This is the one exception to "unknown fields are ignored": a client that tries to name a location is refused, not silently served (QA-08, QA-40). This check runs before the other field checks. |
| `cookie`, `referer`, `userAgent`, `fallbackUrls`, `onlyIfRunning`, `source` | Ignored and never stored. Phone handoffs always have empty headers and source `phone`. |

The phone applies the same rules to what it receives: unknown fields, unknown event names and unknown error codes are ignored or mapped to a generic case; a wrong type in a known field makes the phone drop that message and log it, never crash.

### Close codes

| Code | Sent by | Meaning | Phone behaviour |
| --- | --- | --- | --- |
| 1000 | either | Normal close (after `unpair`, or the phone leaving) | None |
| 1001 | PC | Listener turning off, app quitting, or idle timeout | Reconnect with back-off |
| 1009 | PC | Frame over the size limit | Report a bug; do not resend |
| 4400 | PC | Malformed frame | Report a bug; do not resend |
| 4401 | PC | Authentication failed (unknown device, revoked device, bad signature: indistinguishable) | Treat the pairing as removed (section 7) |
| 4403 | PC | This authenticated device was removed on the PC, or pairing was denied | "This PC no longer accepts this phone. Pair again to reconnect" / "PC declined" |
| 4408 | PC | Not authenticated within 10 s | Reconnect once |
| 4409 | PC | Replaced by a newer connection from the same device | None |
| 4426 | PC | Phone protocol too old | "Update GrabLine on this phone" |
| 4429 | PC | Flooding (section 9) | Reconnect after 30 s |

## 3. Listener rules

| Rule | Detail | QA |
| --- | --- | --- |
| Off by default | No socket exists until the user opens "Pair a phone". | QA-11 |
| On while needed | On only while at least one device is paired, or a pairing QR is showing. | QA-11 |
| Off again | Turns off (all sockets closed) within 1 s of the last device being removed, and when a QR expires or is closed with no device paired. | QA-11 |
| Bind addresses | One listening socket per local address that is private or link-local: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.0.0/16`, `fc00::/7`, `fe80::/10`. Never `0.0.0.0`, `::`, or a public address. | QA-11 |
| Peer check | Every accepted connection whose remote address is outside those ranges is closed before the TLS handshake. | QA-11 |
| Address changes | The PC re-reads its interface list on every network-change signal and at least every 5 s, opening and closing sockets to match. | QA-12 |
| No UPnP | The listener never asks the router for a port mapping. | QA-11 |
| Loopback test mode | Setting `remote_loopback_test`, off by default, not shown in the UI. When on, the listener also binds `127.0.0.1` and accepts loopback peers, and the QR also lists `10.0.2.2` (the Android emulator's name for the host). | - |
| User switch | "Listener on or off" in Settings. Off closes every connection with `1001`; pairings are kept. | - |

Tailscale: its IPv6 addresses (`fd7a:115c:a1e0::/48`) are inside `fc00::/7`, so they are bound and listed. Its IPv4 range (`100.64.0.0/10`) is not private address space and is not bound.

## 4. Certificate and pinning

### PC certificate

| Item | Value |
| --- | --- |
| Key | ECDSA P-256, generated with Python `cryptography` the first time the listener is enabled |
| Certificate | Self-signed X.509 v3, subject and issuer `CN=GrabLine <pcId>`, serial random, valid from 1 day before creation for 30 years, no SubjectAltName |
| Storage | `remote_cert.pem` and `remote_key.pem` in the GrabLine data directory; the key file is readable by the current user only (0600, or an owner-only ACL on Windows) |
| Rotation | Only when the user chooses "Reset phone connections", or when the data directory is lost. Either one breaks every pairing (QA-07). |

### The pin

**The pin is the SHA-256 hash of the certificate's SubjectPublicKeyInfo, DER-encoded, written as base64url without padding (43 characters).**

Why the public key and not the whole certificate: the key is what proves the PC's identity. The PC may re-issue the certificate around the same key (a nearing expiry date, a changed subject) without breaking pairings. A reinstall creates a new key, so the pin changes and the phone refuses it, which is what QA-07 needs. Java gives the exact bytes as `certificate.publicKey.encoded`; Python as `public_key().public_bytes(DER, SubjectPublicKeyInfo)`.

### Phone trust rules

OkHttp's default trust manager rejects self-signed certificates, so each paired PC gets its own `OkHttpClient` built with a custom `X509TrustManager` and `HostnameVerifier`. Nothing else in the app uses them.

| Check | Rule |
| --- | --- |
| `checkServerTrusted(chain, authType)` | Take `chain[0]` only. Compute `SHA-256(chain[0].publicKey.encoded)`. Compare with the stored pin using `MessageDigest.isEqual`. Mismatch, empty chain or any exception: throw `CertificateException`. |
| System trust store | Not consulted. A certificate from a public CA with a different key is refused. |
| Validity dates | Not checked. A wrong phone clock must not lock the user out; the pin is the whole trust decision. |
| `getAcceptedIssuers()` | Empty array |
| Hostname verification | The verifier returns `true` without looking at the name. The PC is reached by IP addresses that change, and the certificate has no names. Identity is the pinned key, checked on every handshake, so this does not weaken anything. |
| Mismatch | The phone does not send anything, drops the connection, and shows "Your PC's identity changed. If you reinstalled GrabLine, pair again". It MUST NOT offer "connect anyway". |
| Redirects, proxies | The client follows no redirects and uses `Proxy.NO_PROXY`. |

During manual pairing only (section 5.4) the trust manager records the presented key instead of comparing; the key becomes the pin only if the pairing proofs verify.

## 5. Pairing

### 5.1 Pairing session (PC)

| Item | Value |
| --- | --- |
| Start | The user opens Devices, Pair a phone. The listener turns on. |
| Lifetime | 120 s from the moment the QR is shown. One session at a time; opening a new one cancels the old. |
| Code | 12 characters from the Crockford base32 alphabet `0123456789ABCDEFGHJKMNPQRSTVWXYZ`, chosen with `secrets.choice`: 60 bits. Shown as `XXXX-XXXX-XXXX`. |
| Single use | The session ends the moment one correct proof arrives (before the Allow prompt). A second correct proof gets `pairingExpired`. |
| Wrong proofs | Counted per session, across all connections. The fifth wrong proof locks the session: that reply and all later `pair` requests get `pairingLocked`, the QR is replaced by the warning "Too many wrong codes. Show a new code to try again", and a new session needs a new user action (QA-03). |
| Storage | The code lives in memory only and is never logged. |

### 5.2 QR code content

```
grabline-pair:?v=1&a=<addresses>&p=<port>&f=<pin>&c=<code>&i=<pcId>&n=<pcName>&x=<expiry>
```

| Parameter | Format | Notes |
| --- | --- | --- |
| `v` | `1` | Format version of this URI. Unknown value: "Update GrabLine on this phone". |
| `a` | Comma-separated list, 1 to 8 entries. IPv4 dotted, IPv6 in percent-encoded brackets (`%5B...%5D`), no zone id | Every private and link-local address the listener is bound to, IPv4 first. Link-local IPv6 is listed last; the phone tries it on its active Wi-Fi interface and MAY skip it. |
| `p` | int 1024 to 65535 | Same port for every address |
| `f` | 43 characters, base64url | The pin (section 4) |
| `c` | 12 characters, Crockford alphabet, no dashes | The one-time code |
| `i` | 22 characters, base64url | `pcId`: 16 random bytes created once per PC data directory |
| `n` | Percent-encoded UTF-8, at most 64 characters decoded | The PC's display name (host name by default) |
| `x` | time | When the session expires |

Parameters appear in this order; the phone parses by name and ignores unknown ones. Any missing or malformed parameter: "This isn't a GrabLine pairing code". QR error correction level M. Fixture: `qr.uri.txt`.

### 5.3 Proofs

The code is never sent. Both sides prove they know it, bound to this connection, this PC key and this phone key.

```
K            = the 12 code characters as ASCII bytes
pubHash      = SHA-256(device public key, SPKI DER)            (32 bytes)
pin          = the 32 raw bytes of the pin
nonce        = the 32 raw bytes of hello.nonce
codeProof    = HMAC-SHA256(K, "grabline-pair-v1"   || 0x00 || nonce || pin || pubHash)
serverProof  = HMAC-SHA256(K, "grabline-paired-v1" || 0x00 || nonce || pin || pubHash || deviceId as ASCII)
```

The phone uses the pin from the QR (or the key it saw, in manual mode); the PC uses its own. A machine in the middle has a different key, so its relayed proof fails on the PC and it cannot make a `serverProof` for the phone. It can only guess the code offline from a captured proof, and the code is dead within 120 s (plus at most 60 s of prompt). Both comparisons are constant-time (`hmac.compare_digest`, `MessageDigest.isEqual`). Test vectors: `vectors.json`.

### 5.4 Manual entry (camera denied)

The PC shows, under the QR: the first listed address with the port (`192.168.1.20:47821`) and the code (`7K3M-9QX2-RTBW`). The phone asks for both.

| Step | Rule |
| --- | --- |
| Address field | `host:port`; IPv6 in brackets. Port optional, default 47821. |
| Code field | Normalise before use: remove spaces and dashes, upper-case, then `O` to `0`, `I` and `L` to `1`. After that it MUST be 12 characters of the alphabet, else "That code doesn't look right". |
| Trust | The phone connects accepting any certificate, records its SPKI hash as the candidate pin, and uses it in `codeProof`. |
| Commit | The candidate becomes the pin only after `serverProof` verifies. If it does not, the phone stores nothing and shows "That code didn't match". |

The proofs give manual entry the same protection as the QR without typing a 43-character fingerprint. Fixture: `qr.manual.txt`.

### 5.5 Flow

```
phone                                              PC
  | scan QR / type address + code                   | session active, QR showing
  | generate P-256 key in AndroidKeyStore           |
  |---- TLS (pin from QR) ------------------------->|
  |<--- hello ---------------------------------------|
  |---- pair {deviceName, publicKey, codeProof} --->| check session, lock, proof
  |<--- pairPending {timeoutSeconds: 60} -----------| "Allow Pixel 8 to control GrabLine?"
  |                                                  | user: Allow
  |<--- paired {deviceId, pcId, pcName, serverProof}| device row stored
  | verify serverProof; store pin, deviceId, pcId   |
  |---- auth ... (section 6) ---------------------->|
```

| Outcome | PC | Phone | QA |
| --- | --- | --- | --- |
| Allow | Stores `{deviceId, name, publicKey, created, lastSeen}`, replies `paired`, closes the pairing dialog, lists the device | Verifies `serverProof`, stores the pairing, sends `auth` on the same connection, lists the PC as online | QA-01 |
| Deny | Stores nothing, replies error `pairingDenied`, closes `4403` | Deletes the Keystore key it generated, stores nothing, shows "PC declined" | QA-04 |
| No answer in 60 s | Treated as Deny, but the reply is `pairingExpired` | Deletes the key, shows "Code expired" | - |
| Session over, or none | `pairingExpired`; nothing stored | Deletes the key, shows "Code expired. Show a new code on your PC" | QA-02 |
| Wrong proof | `badCode` with `attemptsLeft`; fifth time `pairingLocked` | "That code didn't match" / "Too many wrong codes. Show a new code on your PC" | QA-03 |
| Phone leaves during the prompt | Prompt closes, nothing stored | - | - |

Expiry is decided by the PC's clock. If the phone cannot connect and its own clock is past `x`, it shows "Code expired" instead of "PC not found" (the listener may already be off).

A phone that pairs again with a PC it already knows (`pcId` matches) replaces its old pairing; the PC shows the old device row until the user removes it or the phone sends `unpair` first (the phone SHOULD, if the old pairing still authenticates).

### 5.6 Device key (phone)

| Item | Value |
| --- | --- |
| Algorithm | EC, curve `secp256r1`, purpose `SIGN`, digest `SHA-256` |
| Store | `AndroidKeyStore`, non-exportable; StrongBox when the device has it, else TEE |
| User authentication | Not required (the app must reconnect in the background to deliver the outbox) |
| Alias | `grabline.pc.<pcId>`; `grabline.pairing` while a pairing is in progress |
| Lifetime | Deleted on Deny, on failure, on `unpair`, on "Forget this PC", and when the PC reports the pairing removed |
| Backup | The alias and pin are excluded from Android backup; a restored phone must pair again |

## 6. Authentication handshake

Every connection starts with `hello` from the PC. The phone answers with `auth` (or `pair` then `auth`).

```
transcript = "grabline-auth-v1" || 0x00 || nonce (32 bytes) || pin (32 bytes) || deviceId (22 ASCII bytes)
signature  = ECDSA-P256-SHA256(device private key, transcript), ASN.1 DER, sent as base64url
```

| Property | How |
| --- | --- |
| Fresh | The nonce is 32 bytes from `secrets.token_bytes`, new for every connection, valid for one `auth` attempt |
| Bound to this PC | The pin is the key the phone verified in this TLS handshake; the PC verifies with its own key hash. A signature made for another server never verifies here. |
| Domain separated | The label and fixed lengths mean the key can never be tricked into signing anything else that parses as an auth transcript |
| No secret on the PC | The PC stores public keys only; reading its database gives no ability to act as a phone |

PC checks, in this order: `deviceId` is known and not removed; the stored key verifies the signature; `protocolVersion` in `auth` is at least the PC's `minClientVersion`. The first two failures give the single reply `{type: "error", code: "unauthorized", message: "not authorized"}` followed by close `4401`, with the same timing path for both (the PC verifies against a fixed dummy key when the device is unknown). The version failure gives `versionTooOld` and close `4426`.

"Every request carries the device key": after `authOk`, every frame on the connection is covered by the TLS session that the signature was bound to, so each request is attributable to exactly one device key without repeating a token. A bearer token in each message would be weaker: it could be copied from the PC's database or the phone's memory. Revocation ends the session itself (section 8).

### Before `authOk`

| Limit | Value |
| --- | --- |
| Accepted types | `pair`, `auth`. Anything else: `unauthorized`, close `4401`. No state is read or written (QA-05). |
| Time to authenticate | 10 s from the WebSocket handshake; `4408`. A connection waiting on the Allow prompt gets the prompt's 60 s instead. |
| Frame size | 4,096 bytes |
| Messages | At most 4 per connection |
| `auth` attempts | 1 per connection |
| Unauthenticated connections | 8 in total, 3 per remote address; extra ones are closed at accept |
| Repeated failures | 10 failed `auth` or `pair` from one address in 60 s: that address is refused for 60 s |

## 7. Authorization rules

| Rule | Detail | QA |
| --- | --- | --- |
| One level | An authenticated device may use every message in this document. There are no roles. | - |
| Job scope | A device sees and controls every job on the PC, whatever its source, as the PC's own window does. | QA-16 |
| Locations | A destination is chosen only by `folderId` or `queueId` from the `queues` reply. The PC resolves ids to paths itself, at claim time. An id it did not issue is `notFound`. | QA-08 |
| Files | `control`, `fetch` and `fileHash` take a `jobId` (int). The PC reads the path from its own database. `fetch` serves only jobs with status `completed`. | QA-40 |
| Headers | Phone handoffs carry no cookies, referer or user agent. | - |
| Unattended | A phone handoff never opens a modal dialog on the PC. It uses the unattended add path with the given quality, or `best`. | QA-13 |
| Strings from the PC | `name`, `error` and folder `name` MUST NOT contain a filesystem path. URLs have any `user:password@` part removed. | - |
| Pairing removed | If `auth` fails with `unauthorized` on a connection whose pin matched, the phone knows the real PC refused its key: it deletes the pairing and key and shows "This PC no longer accepts this phone. Pair again to reconnect". | QA-06 |

## 8. Revocation and unpairing

| Action | PC | Phone | QA |
| --- | --- | --- | --- |
| Remove a device on the PC | In one step on the listener thread: mark the device removed in memory, delete its row, close each of its connections with `4403`, drop its queued replies and streams. A request already being handled gets no reply. If it was the last device, the listener turns off. | On `4403`: delete pairing and key, show "This PC no longer accepts this phone. Pair again to reconnect". A later connection attempt gets `unauthorized`/`4401` (or no listener) and ends the same way. | QA-06, QA-11 |
| `unpair` from the phone | Deletes the device, replies `ok`, closes `1000` | Deletes pairing and key whether or not the reply arrives. If the PC is unreachable, the phone forgets locally and the PC keeps a dead row until the user removes it. | - |
| Reset phone connections (PC) | Removes all devices, deletes the certificate and key, turns the listener off | Next connect: pin mismatch (QA-07 message) | QA-07 |

Within 1 s (QA-06): removal is applied on the listener thread before the Settings click handler returns, and every request handler checks the in-memory "removed" flag before it touches the database.

## 9. Rate limits

All limits are enforced on the listener thread before any database or UI work, so a flood cannot stall the window (QA-10). The listener MUST NOT run on the UI thread.

| Limit | Value | When exceeded |
| --- | --- | --- |
| Requests per device (all its connections together) | Token bucket: 20 tokens per second, burst 40. Each request costs 1; `analyze`, `fileHash` and `links` cost 5. | Error `rateLimited` with `retryAfterMs`; the request has no effect |
| `analyze` in flight per device | 2 | `rateLimited` |
| `fetch` streams | 1 per connection, 2 per device | `conflict` |
| `fetchAck` notifications | 50 per second per connection, not charged to the bucket | Extra ones ignored |
| Flood | More than 200 rate-limited requests in any 10 s | Close `4429`; the device is refused for 30 s |
| Unanswered requests per connection | 32 | `rateLimited` |
| Events per connection | At most 20 per second after coalescing (section 15) | Coalesced, never dropped silently for state changes |
| `phone.command` events per device | At most 10 per second (section 20) | The PC does not send it and shows "busy" to its user |

Phone on `rateLimited`: wait `retryAfterMs` (at least 250 ms), retry up to 3 times, then show "Your PC is busy. Try again in a moment". User-facing actions are never retried in a tight loop.

## 10. Versioning

| Rule | Detail |
| --- | --- |
| One number | `PROTOCOL_VERSION` (6) is shared by the Native Messaging host and the remote listener. Version-5 extensions keep working: nothing they send or read changed. |
| Carried in | `hello`, every reply, every event; and `protocolVersion` in the phone's `pair` and `auth` |
| Bump when | A message type, a required field, or a meaning changes. Adding an optional field, an event name or a `features` entry does not need a bump. |
| Phone constant | `MIN_PC_PROTOCOL = 6`. If `hello.protocolVersion` (or any reply's) is lower or missing, the phone sends nothing more, closes `1000`, and shows "Update GrabLine on your PC" (QA-17). It MUST tolerate every other field being absent. Fixtures: `hello.server.old.json`, `pong.v5.json`. |
| PC constant | `minClientVersion = 6`, sent in `hello`. A lower `protocolVersion` in `pair` or `auth`: `versionTooOld`, close `4426`; the phone shows "Update GrabLine on this phone". The phone SHOULD check `hello.minClientVersion` itself and not send at all. |
| Features | `authOk.features` lists optional message groups this PC serves. A missing group, or an `unsupported` reply: the phone hides or blocks only that feature with "Update GrabLine on your PC to use this". `phoneJobs` (section 20) is such a group: a PC that lists it accepts `phoneJobs` and `phoneCommandResult` and sends `phone.command`; an older PC does not list it and the phone sends none of them. |
| Newer PC | A phone meeting `protocolVersion` above its own carries on; it ignores what it does not know. |

## 11. Finding the PC

mDNS is not used. The phone keeps, per PC, the address list from the QR, replaced by `authOk.addresses` after each successful login, plus the last address that worked.

| Step | Rule |
| --- | --- |
| 1 | Try the last address that worked (connect timeout 2 s). |
| 2 | Try the other known addresses in parallel (2 s). |
| 3 | If the phone is on Wi-Fi or Ethernet and has an IPv4 address in a prefix of /22 or longer: try every other host in that prefix on the port, 32 at a time, 400 ms connect timeout. Only a host that presents the pinned key counts. At most one sweep per 60 s, and only while the app is in the foreground or the outbox is not empty. |
| 4 | "PC not found" with Retry. Links wait in the outbox ("Will send when your PC is back"). |

Step 3 is what meets QA-12 (new DHCP lease, found within 10 s) without a discovery service. A PC on another network is reached by typing its address in "Edit address" (for example a Tailscale IPv6 address).

## 12. Messages

"Phase" is the product phase in which the message ships. All requests need `authOk` first, except `pair` and `auth`. Every reply also has `id`, `type` and `protocolVersion`; every request has `id` and `type`; they are not repeated in the tables. "Opt" marks optional fields.

### Summary

| Request | Reply `type` | Phase | Cost | Errors beyond the common ones |
| --- | --- | --- | --- | --- |
| (none) | `hello` (PC speaks first) | 1 | - | - |
| `pair` | `pairPending` event, then `paired` | 1 | - | `pairingExpired`, `pairingLocked`, `badCode`, `pairingDenied`, `versionTooOld` |
| `auth` | `authOk` | 1 | - | `unauthorized`, `versionTooOld` |
| `ping` | `pong` | 1 | 1 | - |
| `download` | `queued` | 1 | 1 | `notFound` (queue, folder), `forbidden` |
| `status` | `status` | 1 | 1 | `tooLarge` |
| `recent` | `recent` | 1 | 1 | - |
| `focus` | `ok` | 1 | 1 | - |
| `links` | `queued` | 1 | 5 | `tooLarge`, `notFound`, `forbidden` |
| `jobs` | `jobs` | 1 | 1 | - |
| `control` | `ok` | 1 | 1 | `notFound`, `conflict` |
| `global` | `global` | 1 | 1 | - |
| `queues` | `queues` | 1 | 1 | - |
| `analyze` | `analysis` | 1 | 5 | `failed` |
| `subscribe` / `unsubscribe` | `subscribed` / `ok` | 1 | 1 | - |
| `unpair` | `ok` | 1 | 1 | - |
| `files` | `files` | 4 | 1 | - |
| `fetch` | `fetchStart`, binary frames, `fetchEnd` event | 4 | 1 | `notFound`, `conflict`, `forbidden` |
| `fetchCancel` | `ok` | 4 | 1 | `notFound` |
| `fetchAck` (notification) | none | 4 | 0 | - |
| `fileHash` | `fileHash` | 4 | 5 | `notFound`, `conflict` |
| `outbox` | `outbox` | 4 | 1 | - |
| `outboxAck` | `ok` | 4 | 1 | `tooLarge` |
| `phoneJobs` | `ok` | `phoneJobs` feature | 3 | - |
| `phoneCommandResult` | `ok` | `phoneJobs` feature | 1 | - |

Common errors for every request: `badRequest`, `rateLimited`, `unsupported`, `internal`, and `unauthorized` before `authOk`. The extension's `gallery` message is not served to phones (`unsupported`).

### `hello` (PC to phone, event)

| Field | Type | Notes |
| --- | --- | --- |
| `protocolVersion` | int | 6 |
| `minClientVersion` | int | Lowest phone protocol this PC accepts |
| `pcId` | string, 22 | Stable id of this PC |
| `pcName` | string, 1 to 64 | Display name |
| `nonce` | bytes, 32 raw | Fresh per connection |

`hello` is sent to unauthenticated peers on the local network, so it carries nothing else: no app version, no pairing state, no device count.

### `pair`

| Request field | Type | Notes |
| --- | --- | --- |
| `protocolVersion` | int | The phone's version |
| `deviceName` | string, 1 to 64 | Shown in the Allow prompt and the device list; control characters removed by the PC before display |
| `publicKey` | bytes | P-256 SubjectPublicKeyInfo DER, exactly 91 bytes, uncompressed point on the curve; anything else `badRequest` |
| `codeProof` | bytes, 32 raw | Section 5.3 |
| `appVersion` | string, at most 32, opt | For the device list only |

| `pairPending` field | Type | Notes |
| --- | --- | --- |
| `timeoutSeconds` | int | 60. The phone shows "Confirm on your PC". |

| `paired` field | Type | Notes |
| --- | --- | --- |
| `deviceId` | string, 22 | 16 random bytes, base64url, created by the PC |
| `pcId`, `pcName` | string | As in `hello` |
| `serverProof` | bytes, 32 raw | Section 5.3. The phone MUST verify it before storing anything. |

Check order on the PC: schema; session exists and not expired (`pairingExpired`); not locked (`pairingLocked`); proof (`badCode` with `attemptsLeft` int, or `pairingLocked` on the fifth); version. At most 8 devices may be paired; a ninth `pair` gets `conflict` before the prompt.

### `auth`

| Request field | Type | Notes |
| --- | --- | --- |
| `protocolVersion` | int | The phone's version |
| `deviceId` | string, 22 | From `paired` |
| `signature` | bytes, at most 72 raw | Section 6 |
| `appVersion` | string, at most 32, opt | Stored with `lastSeen` |

| `authOk` field | Type | Notes |
| --- | --- | --- |
| `pcId`, `pcName` | string | The phone updates the name it shows |
| `appVersion` | string | Desktop app version |
| `appRunning` | bool | Always `true` (the listener lives inside the app) |
| `serverTime` | time | For clock-skew display only |
| `addresses` | list of string, at most 8 | Current bound addresses, QR notation without percent-encoding |
| `port` | int | Current port |
| `features` | list of string | Subset of `jobs`, `control`, `global`, `queues`, `analyze`, `events`, `files`, `fetch`, `outbox`, `phoneJobs` |

### `ping`

Request: no fields. Reply `pong`: `appRunning` (bool), `pcId`, `pcName`, `appVersion` (strings), `serverTime` (time). The first two fields of the reply are exactly the extension's v5 `pong`.

### `download`

| Request field | Type | Notes |
| --- | --- | --- |
| `url` | url | Required |
| `requestId` | string, 16 to 64 of `[A-Za-z0-9_-]` | Required. Created by the phone when the user shares the link, stored in its outbox, and reused for every retry of the same share. |
| `quality` | string, opt | One of `best`, `2160p`, `1440p`, `1080p`, `720p`, `480p`, `360p`, `mp3`, `m4a`, `flac` (lower case; the set in `host.py`). Default `best`. Any other value: `badRequest`. |
| `queueId` | int, opt | An id from `queues`. Absent or `null`: the default queue. |
| `folderId` | string, opt, `^[A-Za-z0-9_-]{1,32}$` | An id from `queues`. Absent or `null`: the PC's default folder. |
| `title` | string, at most 512, opt | Shown until the PC resolves a real name |
| `pageUrl` | url, opt | The page the link was shared from, if the phone knows it |

| `queued` field | Type | Notes |
| --- | --- | --- |
| `handoffId` | int | Row in the handoffs table |
| `handoffUrl` | string | The URL stored (after the same video-page rewrite the extension gets) |
| `previousJobId` | int | Newest earlier job for this URL, or 0 |
| `appRunning` | bool | `true` |
| `requestId` | string | Echo |
| `duplicate` | bool | `true` if nothing new was queued |
| `jobId` | int or null | The job, if one already exists for this handoff; else `null`. The job is announced by `job.added` with the same `requestId`. |

PC handling: insert into `handoffs` with `source = "phone"`, `headers = {}`, `quality`, and the new `options` column holding `{"requestId", "deviceId", "queueId", "folderId", "title", "unattended": true}`. The app claims it on its next poll through the unattended add path; an id that no longer resolves at claim time falls back to the default folder or queue.

Exactly once and deduplication:

| Case | Rule | QA |
| --- | --- | --- |
| Same `requestId` from the same device again | The PC keeps `(deviceId, requestId) -> handoffId` in a `remote_requests` table for 7 days (and at most 10,000 rows per device, oldest dropped). A repeat returns the first `handoffId` with `duplicate: true` and queues nothing. | QA-14 |
| Phone outbox | An entry is deleted only after a `queued` reply for its `requestId`. Any other outcome (no reply, connection lost, `rateLimited`, `internal`) keeps it for a retry with the same `requestId`. `badRequest`, `forbidden`, `notFound` and `unsupported` remove it and tell the user. | QA-14 |
| Same link twice in a second | Same device, same `url`, `quality`, `queueId` and `folderId`, different `requestId`, within 5 s of the first: the first `handoffId` is returned with `duplicate: true`. The phone also drops an identical share made within 2 s before it reaches the outbox. | QA-15 |
| Entries older than 7 days in the phone outbox | The phone asks before sending them ("Still send 3 old links?"), since the PC may no longer remember the `requestId`. | QA-14 |

### `status`

Request: `urls` (list of url, at most 50). Reply `status`: `jobs`, one entry per valid URL in request order, `{url, id, status, downloaded, total, name}` or `{url, status: "pending"}` when the PC has no job for it; `appRunning`. Same shape as the extension's reply.

### `recent`

Request: `limit` (int 1 to 20, opt, default 5). Reply `recent`: `jobs` (list of job objects, newest first), `appRunning`.

### `focus`

Request: `target` (string at most 32, opt; `settings` opens Settings). Reply `ok`: `appRunning`. Raises the PC window; opens no dialog.

### `links`

| Request field | Type | Notes |
| --- | --- | --- |
| `urls` | list of url, 1 to 300 | Duplicates removed, order kept. More than 300: `tooLarge`. No valid entry: `badRequest`. |
| `requestId` | string | Required; same rules and 7-day memory as `download` |
| `quality`, `queueId`, `folderId` | as in `download`, opt | Apply to every link |
| `pageUrl`, `pageTitle` | url / string at most 512, opt | - |

Reply `queued`: `handoffId`, `count` (links accepted), `appRunning`, `requestId`, `duplicate`. One handoff with `source = "phone-links"`; the app adds each link as its own unattended job. The PC shows no picker (section 18).

### `jobs`

| Request field | Type | Notes |
| --- | --- | --- |
| `limit` | int 1 to 100, opt | Default 50 |
| `cursor` | string at most 64, opt | `nextCursor` from the previous page. Malformed: `badRequest`. |
| `filter` | string, opt | `all` (default), `active` (queued, downloading, paused), `finished` (completed), `failed` (failed, cancelled) |

Reply `jobs`: `jobs` (list of job objects, newest id first), `nextCursor` (string or null), `total` (int, jobs matching the filter). The cursor is opaque to the phone (the PC encodes the last id), so pages stay stable while jobs are added.

### `control`

| Request field | Type | Notes |
| --- | --- | --- |
| `jobId` | int, at least 1 | - |
| `action` | string | `pause`, `resume`, `cancel`, `retry`, `remove` |

| Action | Allowed from | Result status | Already there |
| --- | --- | --- | --- |
| `pause` | queued, downloading | paused | paused: `ok`, no change |
| `resume` | paused | queued or downloading | queued, downloading: `ok` |
| `cancel` | queued, downloading, paused | cancelled | cancelled: `ok` |
| `retry` | failed, cancelled | queued | queued, downloading: `ok` |
| `remove` | any | job gone | unknown id: `notFound` |

Any other combination: `conflict`. Reply `ok`: `job` (the job object after the action, or `null` after `remove`). `remove` behaves exactly as Remove in the desktop window: the list entry goes, a finished file stays on disk, an unfinished job is stopped first. The change is also sent as an event to every subscribed connection, including the sender's (QA-16).

### `global`

| Request field | Type | Notes |
| --- | --- | --- |
| `action` | string | `get`, `pauseAll`, `resumeAll`, `setSpeedLimit`, `clearSpeedLimit` |
| `bytesPerSecond` | int 1,024 to 10,000,000,000 | Required for `setSpeedLimit`, ignored otherwise |

Reply `global`: `state`, the global state object:

| Field | Type | Notes |
| --- | --- | --- |
| `paused` | bool | Pause-all is on |
| `speedLimit` | int or null | Bytes per second; null is no limit |
| `speed` | int | Current total, bytes per second |
| `active` | int | Jobs downloading |
| `queued` | int | Jobs waiting |
| `todayBytes` | int, opt | Phase 4 dashboard figure |

### `queues`

Request: no fields. Reply `queues`:

| Field | Type | Notes |
| --- | --- | --- |
| `queues` | list of `{id: int, name: string, paused: bool}` | Named queues in display order. The default queue is not listed; it is `queueId` absent. |
| `folders` | list of `{id: string, name: string, isDefault: bool}` | The default download folder and the favourite folders that exist. At most 50. |

`folders[].id` is `"f"` plus base64url of the first 10 bytes of `HMAC-SHA256(folderSecret, absolute normalised path)`, where `folderSecret` is 32 random bytes kept in the PC's settings. It is stable across restarts, says nothing about the path, and cannot be forged for a folder the PC did not list. `name` is the last path component (with its parent's name added when two folders share one); never a full path.

### `analyze`

Request: `url` (url; `magnet:` is `badRequest`). Reply `analysis`:

| Field | Type | Notes |
| --- | --- | --- |
| `url` | string | Echo |
| `title` | string or null | - |
| `duration` | int or null | Seconds |
| `isPlaylist` | bool | - |
| `count` | int | Items (1 unless a playlist) |
| `qualities` | list, at most 20, best first | `{label, kind: "video" or "audio", height: int or null, ext: string, sizeBytes: int or null}`; `label` is from the `download.quality` set |

The PC answers within 30 s or replies `failed` with `reason`: `noMedia` ("This is a web page, not a file"), `drm` ("... protects its media with DRM. GrabLine can't download it"), `siteError`, `timeout`. Nothing is queued by `analyze`.

### `subscribe`, `unsubscribe`

Request `subscribe`: `topics` (list of `jobs`, `global`, `outbox`, `phone`; opt, default `["jobs", "global"]`; unknown names ignored). Reply `subscribed`: `topics` (those now on), `state` (global state object). Subscribing again replaces the topic set. `unsubscribe` has no fields and replies `ok`. Events are described in section 15.

### `unpair`

Request: no fields. Reply `ok`, then the PC deletes the device and closes `1000` (section 8).

## 13. Files, fetch and outbox (Phase 4)

### `files`

Request: `limit` (int 1 to 100, opt, default 50), `cursor` (opt). Reply `files`: `files` (list of `{jobId: int, name: string, size: int, finishedAt: time, mime: string or null}`), `nextCursor`. Listed: jobs with status `completed` whose output is one regular file that still exists. Multi-file outputs (torrent folders) are not listed in version 6.

### `fetch`

| Request field | Type | Notes |
| --- | --- | --- |
| `jobId` | int | A job from `files` |
| `offset` | int, at least 0, opt | First byte wanted; default 0 (this is the Range support) |
| `length` | int, at least 1, opt | Bytes wanted; default to the end of the file |
| `etag` | string at most 64, opt | The `etag` from an earlier `fetchStart`. If the file no longer matches: `conflict`; the phone starts again from 0. |

| `fetchStart` field | Type | Notes |
| --- | --- | --- |
| `streamId` | int, 1 to 2^32 - 1 | Unique on this connection |
| `jobId`, `name`, `size` | int, string, int | `size` is the whole file |
| `offset`, `length` | int | The range that will be sent |
| `etag` | string | Opaque validator made from size and modification time |
| `chunkSize` | int | 262,144 |
| `windowBytes` | int | 8,388,608 |

Errors: `notFound` (no such job, or its file is gone), `conflict` (job not `completed`, file changed, `offset` past the end, or a stream is already open), `forbidden` (a path field). The PC opens the file from the job's stored path, refuses anything that is not a regular file, and holds it open for the stream.

Binary frame (PC to phone), all integers big-endian:

| Bytes | Field | Value |
| --- | --- | --- |
| 0 to 3 | magic | ASCII `GLF1` |
| 4 to 7 | `streamId` | uint32 |
| 8 to 15 | `offset` | uint64: position in the file of the first payload byte |
| 16 onward | payload | 1 to 262,144 bytes (256 KiB). The largest frame is 262,160 bytes. |

Fixture: `fetch.frame.hex.txt`.

| Rule | Detail |
| --- | --- |
| Order | Frames of a stream arrive in order with no gaps. The phone checks `offset` equals what it expects; if not, it cancels and resumes. |
| Flow control | The PC sends no byte at or beyond `acked + windowBytes`, where `acked` starts at the stream's `offset`. The phone sends `fetchAck {streamId, received}` (`received` = absolute offset of the next byte it needs, counted only after the bytes are written to disk) at least every 2 MiB. On the PC, a chunk is also not queued while the socket's `bytesToWrite()` is above 1 MiB. |
| Interleaving | JSON frames may be sent between chunks. The phone SHOULD run `fetch` on its second connection so events are not delayed. |
| End | Event `fetchEnd {streamId, status, nextOffset}` with `status` `complete`, `cancelled` or `error` (and `code` for `error`). `nextOffset` is where a resume would start. |
| Cancel | `fetchCancel {streamId}`: reply `ok`, then `fetchEnd` with `cancelled`. Closing the connection also ends the stream. |
| Resume | After a drop the phone reconnects and sends `fetch` with `offset` = bytes safely on disk and the `etag` (QA-39). |
| Integrity | When the copy is complete the phone sends `fileHash {jobId}` and compares. Reply `fileHash`: `jobId`, `size`, `etag`, `sha256` (64 lower-case hex). The PC hashes off the UI thread and caches by path, size and modification time. A mismatch deletes the copy and says so. |
| Stall | No ack for 60 s: `fetchEnd` with `error`, `code: "failed"`. |

### `outbox`, `outboxAck`

These carry links the PC user queued "for my phone". (The phone's own outbox of links waiting for the PC is a local store, not a message.)

`outbox` request: no fields. Reply `outbox`: `items` (at most 100, oldest first), each `{itemId: int, url: string, title: string or null, quality: string or null, createdAt: time}`; only items addressed to this device or to "any phone".

`outboxAck` request: `itemIds` (list of int, 1 to 100). Reply `ok`: `removed` (int). The phone acks only after the item is saved in its own database; the PC deletes on ack. An item acked twice or unknown is skipped, not an error. An unacked item is offered again, so the phone ignores an `itemId` it already holds (per `pcId`).

## 14. Job object

Used by `recent`, `jobs`, `control` and events. Field names follow the extension's `status` reply where they overlap.

| Field | Type | Source on the desktop | Notes |
| --- | --- | --- | --- |
| `id` | int | `Job.id` | - |
| `url` | string | `Job.url` | `user:password@` removed |
| `name` | string | `Job.title or Job.filename` | Never a path |
| `status` | string | `JobStatus` in `app/core/models.py` | `queued`, `downloading`, `paused`, `completed`, `failed`, `cancelled`. An unknown value is shown as "Unknown" by the phone, with no actions. |
| `kind` | string | `JobKind` | `direct`, `smart`, `hls`, `torrent`, `cloud` |
| `downloaded` | int | `db.stored_progress(job)` or the live counter | Bytes |
| `total` | int or null | `Job.total_size` | null when unknown |
| `speed` | int | Manager's live figure | Bytes per second; 0 unless `downloading` |
| `eta` | int or null | Manager's live figure | Seconds; null when unknown or not downloading |
| `queueId` | int or null | `Job.queue_id` | null is the default queue |
| `createdAt` | time | `jobs.created_at` | - |
| `finishedAt` | time or null | `jobs.updated_at` when status is `completed`, `failed` or `cancelled` | null otherwise |
| `error` | string or null, at most 512 | `Job.error` | Plain words, paths removed; null unless `failed` |
| `requestId` | string or null | handoff `options.requestId`, copied into `Job.options` | Set only on jobs this device created; null for everyone else's |
| `fetchable` | bool | - | `true` when `fetch` would accept the job (Phase 4; `false` before) |

## 15. Events

Sent only on connections that subscribed. Shape: `{type: "event", protocolVersion, event, seq, ts, ...}`. `seq` is an int that starts at 1 per connection and rises by 1; a gap tells the phone to reload `jobs`. `ts` is the PC's time.

| `event` | Topic | Payload | Sent when | Throttle |
| --- | --- | --- | --- | --- |
| `job.added` | jobs | `job` | A job is created, from any source | None |
| `job.updated` | jobs | `job` | Status, name, total or queue changed (pause, resume, cancel, retry, start) | Coalesced to the latest per job, at most 5 per second per job |
| `job.progress` | jobs | `jobId`, `status`, `downloaded`, `total`, `speed`, `eta` | Bytes moved | At most 1 per second per job |
| `job.finished` | jobs | `job` | Status became `completed` | None |
| `job.failed` | jobs | `job` (with `error`) | Status became `failed` | None |
| `job.removed` | jobs | `jobId` | The job left the list | None |
| `global.updated` | global | `state` | Pause-all, speed limit, or counts changed | At most 1 per second |
| `outbox.added` | outbox | `count` | Phase 4: the PC queued a link for this phone | At most 1 per second |
| `phone.command` | phone | `commandId`, `action`, `jobId` | Section 20: the PC user asked this phone to act on one of its downloads | Never coalesced; at most 10 per second per device; only on the device's newest connection that subscribed to `phone` |

| Rule | Detail |
| --- | --- |
| Snapshot | `subscribe` first, then page through `jobs`. Apply events by job id; the newest wins. An event for an unknown job id that is not `job.removed` is treated as `job.added` when it has a `job`, else ignored until the next reload. |
| Latency | A state change reaches subscribers within 1 s (QA-16); a new job from `download` within 2 s (QA-13). |
| Overload | Above 20 events per second on a connection, progress events are dropped first (the next one carries current numbers), then `job.updated` is coalesced. State-changing events are never dropped; if the PC cannot keep that promise it skips a `seq` so the phone reloads. |
| Reconnect | Events are not replayed. After reconnecting the phone subscribes and reloads. |
| Notifications | The phone raises "finished" and "failed" notifications from `job.finished` and `job.failed` only while connected; it does not poll in the background. |

## 16. Errors

Shape: `{id, type: "error", protocolVersion, code, message, ...}`. `message` is short English for logs and MUST NOT contain job details, paths, device names or secrets. The phone never shows `message`; it shows its own translated text chosen by `code`. An unknown `code` is handled as `internal`.

| `code` | Meaning | Extra fields | Phone shows |
| --- | --- | --- | --- |
| `badRequest` | Missing field, wrong type, bad pattern, malformed cursor | - | "Something went wrong sending that. Update GrabLine on this phone if it keeps happening" |
| `unauthorized` | Not authenticated, unknown or removed device, bad signature (never says which) | - | "This PC no longer accepts this phone. Pair again to reconnect" |
| `forbidden` | The request named a path or location | - | "Your PC doesn't allow that" |
| `notFound` | Job, queue, folder, stream or file does not exist (any more) | - | "That's no longer on your PC" (the phone refreshes the list) |
| `conflict` | Not possible in the current state | - | "That can't be done right now" (the phone refreshes the item) |
| `tooLarge` | A list over its limit, or a frame over 1 MB (then with close `1009`) | - | "That's too much to send at once" |
| `rateLimited` | Section 9 | `retryAfterMs` int | After retries: "Your PC is busy. Try again in a moment" |
| `unsupported` | Unknown `type`, or a feature this PC does not serve | - | "Update GrabLine on your PC to use this" |
| `versionTooOld` | The phone's protocol is below `minClientVersion` | `minClientVersion` int | "Update GrabLine on this phone" |
| `pairingExpired` | No active pairing session (expired, used, closed, or prompt timed out) | - | "Code expired. Show a new code on your PC" |
| `pairingLocked` | Five wrong proofs | - | "Too many wrong codes. Show a new code on your PC" |
| `pairingDenied` | The user pressed Deny | - | "PC declined" |
| `badCode` | Wrong proof | `attemptsLeft` int | "That code didn't match. Check it and try again" |
| `failed` | The PC tried and could not (`analyze`, `fileHash`, a stream) | `reason`: `noMedia`, `drm`, `siteError`, `timeout`, `io` | By reason; default "Your PC couldn't do that. Try again" |
| `internal` | Unexpected error on the PC; details stay in the PC's log | - | "Your PC hit a problem. Try again" |

Conditions that are not error replies:

| Condition | How the phone knows | Phone shows |
| --- | --- | --- |
| PC too old | `protocolVersion` below 6 or missing in `hello` or a reply | "Update GrabLine on your PC" |
| Identity changed | Pin mismatch in the TLS handshake | "Your PC's identity changed. If you reinstalled GrabLine, pair again" |
| PC unreachable | Section 11 ends without a connection | "PC not found" / "Will send when your PC is back" |
| Removed while connected | Close `4403` | "This PC no longer accepts this phone. Pair again to reconnect" |

## 17. Security rules, traced

| SPEC threat row | Rules here | Sections | QA | Fixtures |
| --- | --- | --- | --- | --- |
| Someone on the same Wi-Fi sends commands | Listener off until pairing; only `pair`/`auth` before login; ECDSA login bound to this PC; one generic `unauthorized` with no detail and no state read; pre-auth limits | 3, 6 | QA-05, QA-11 | `invalid/preauth.*.json`, `invalid/auth.*.json` |
| A fake PC impersonates yours | SPKI pin from the QR; custom trust manager; no bypass; proofs bind pairing to the PC key even in manual entry | 4, 5.3, 5.4 | QA-07 | `qr.uri.txt`, `vectors.json` |
| Pairing code guessed or reused | 60-bit code, never sent; single use; 120 s; 5 wrong proofs lock the session; Allow prompt; Deny stores nothing | 5 | QA-01 to QA-04 | `pair.*.json`, `invalid/pair.*.json` |
| A stolen phone keeps access | Removal closes live connections at once and deletes the public key; key is non-exportable in Keystore; PC holds no phone secret | 5.6, 8 | QA-06 | `error.unauthorized.json` |
| The phone writes anywhere on the PC | Destinations only by PC-issued `folderId`/`queueId`; job ids only in `control`/`fetch`; path-bearing fields `forbidden`; no path in any reply | 2, 7, 12, 13 | QA-08, QA-40 | `invalid/download.folder*.json`, `invalid/download.pathField.json`, `invalid/fetch.*.json`, `invalid/control.pathAsJobId.json` |
| Oversized or malformed messages | 1 MB frames (4 KB before login); strict types; unknown fields ignored; unknown type `unsupported`; per-device token bucket; flood close | 2, 9 | QA-09, QA-10 | `invalid/oversize.marker.json`, `invalid/flood.marker.json`, `invalid/download.wrongTypes.json`, `invalid/unknownType.json` |
| The listener exposed to the internet | Per-address binds on private and link-local ranges; peer address check; no UPnP; `Origin` refused; off after the last unpairing | 3 | QA-11 | - |
| Downloaded files are dangerous | Unchanged: phone handoffs go through the desktop's normal pipeline and advisory checks; nothing is auto-opened; `fetch` only copies bytes | 7, 13 | - | - |

Implementation rules that apply everywhere: constant-time comparison for proofs and pins; no code, nonce, proof, signature, key or URL query string in logs; fail closed on any exception in the auth or pairing path; the listener and its handlers never run on the UI thread; the device list and certificate key are stored in the data directory with user-only access.

Other QA items: QA-12 section 11; QA-13 and QA-16 section 15; QA-14 and QA-15 `download`; QA-17 section 10; QA-38 uses `download` then a local cancel on the phone (the phone removes its partial file only after `queued`); QA-39 `fetch` resume and `fileHash`.

## 18. Deviations from SPEC.md

| SPEC says | This protocol | Why |
| --- | --- | --- |
| HTTPS for requests plus one WebSocket for events | One TLS WebSocket carries requests, replies, events and file bytes | One server class on the PC (`QWebSocketServer`), one pinned client on the phone, one authentication per connection, and revocation is "close the socket". No second listener to secure. |
| The phone finds the PC with mDNS `_grabline._tcp` | No mDNS. The QR lists every address; the phone remembers them, refreshes them from `authOk`, and sweeps its own subnet as a last step | No `zeroconf` dependency, no multicast announcement that tells the whole network a PC runs GrabLine, and mDNS is unreliable on many home routers anyway. Cost: the sweep (section 11) and an "Edit address" field. |
| QR holds the TLS certificate fingerprint (SHA-256) | SHA-256 of the certificate's public key (SPKI) | Lets the certificate be re-issued around the same key without breaking pairings; a reinstall still changes it. |
| The phone "sends the code" | The phone sends an HMAC proof of the code and checks one from the PC | The code never crosses the wire, wrong guesses reveal nothing, and manual entry (no camera) is as safe as the QR. |
| The PC "issues a long-lived device key" stored in Keystore | The phone generates a non-exportable key pair in Keystore; the PC stores the public key and issues a `deviceId` | A key the PC issued would have to be importable and would also sit on the PC. This way no secret that can act as the phone exists outside the phone's secure hardware. |
| "Every request carries the device key" | The connection is authenticated once by a signature bound to the TLS server key; every request on it belongs to that device | Equivalent guarantee, no bearer secret to steal or log, and removal takes effect by closing the connection. |
| "The same JSON messages the browser extension sends" | Same `type` names and fields for `ping`, `download`, `status`, `recent`, `focus`, `links`, plus an `id` on requests and `id`/`code` on replies; `cookie`, `referer`, `userAgent`, `fallbackUrls`, `onlyIfRunning`, `source` are ignored; `gallery` is not served | A multiplexed socket needs request ids; the phone must never carry browser sessions; `onlyIfRunning` is meaningless when the listener lives in the running app. The handler code is still shared. |
| `links`: "hand off many links for the picker" | Each link becomes an unattended job; no picker opens on the PC | Phone handoffs must never open a modal dialog on an unattended PC. The phone does the picking before it sends. |
| `files`/`fetch`: "stream one to the phone with Range support" (implies HTTP) | `fetch` with `offset`/`length`, binary WebSocket frames with a 16-byte header, 256 KiB chunks, ack-based window | Follows from the single-socket decision. Offsets give the same resume ability as Range. |
| `events` listed as one message | `subscribe`/`unsubscribe` requests plus `event` frames with an `event` name | Lets a second (fetch) connection stay quiet, and gives a clean snapshot-then-updates order. |
| Pair-denied as a message (`pairDenied`) | Error reply with `code: "pairingDenied"` | One error path for every refusal; the phone's error table already maps it to "PC declined". |
| Unknown fields ignored | Ignored, except path-bearing names, which are `forbidden` | QA-08 and QA-40 expect such requests to be rejected, not served with the path dropped. |
| Bound to private and link-local addresses; reachable through Tailscale | Bound to RFC 1918, `169.254/16`, `fc00::/7`, `fe80::/10` only | Tailscale's IPv4 range (`100.64.0.0/10`) is not private space, so it is not bound; Tailscale works over its IPv6 addresses, which are. |
| Messages in the SPEC table | Added: `pair`, `auth`, `subscribe`, `unsubscribe`, `unpair`, `fetchCancel`, `fetchAck`, `fileHash`, `outboxAck`, and `global` action `get` | Needed to make pairing, login, flow control, verification and acknowledgement explicit. |

## 19. Open points

| Point | State |
| --- | --- |
| Subnet sweep on large or client-isolated networks | Will not find the PC; the user must use "Edit address". Measure QA-12 on real routers. |
| Link-local IPv6 from Android | Needs the Wi-Fi interface's scope id; allowed to be skipped. |
| Multi-file outputs in `files`/`fetch` | Not in version 6. |
| `finishedAt` | Derived from `jobs.updated_at`; a real `finished_at` column would be more exact. |
| `speed`/`eta` in the job object | Need a read accessor on the download manager; not stored in the database. |

## 20. The phone's downloads on the PC (feature `phoneJobs`)

Both apps act as remotes: the phone already sees and controls the PC's downloads; with this feature the PC
sees the phone's downloads and can pause, resume, cancel, retry or remove them, or have one downloaded on the
PC instead. The phone still connects out and the PC still never connects to the phone: the phone reports its
downloads with a request, and the PC sends commands as events on the phone's own connection.

This is an optional message group, so `PROTOCOL_VERSION` stays 6 (section 10). A PC that serves it lists
`"phoneJobs"` in `authOk.features`. A phone that does not see it there sends nothing from this section.

### Phone setting

The phone has a setting "Let paired PCs see and control downloads on this phone", on by default. While it is
off the phone sends `phoneJobs` with `shared: false` and an empty `jobs` list once per connection, and answers
every `phone.command` with `result: "refused"` without touching anything.

### `phoneJobs` (phone to PC)

Request fields:

| Field | Type | Notes |
| --- | --- | --- |
| `shared` | bool | `false` when the phone setting is off; then `jobs` MUST be empty |
| `jobs` | list, at most 100 | The phone's downloads, newest first (section 20 phone-job object) |

Reply `ok`. Cost 3 (the phone reports at most once a second; the PC does not rely on it). The phone sends a full list right after `authOk` (when the feature is listed) and again
whenever its downloads change, at most once per second per connection (coalesced: the newest list wins). Only
one connection per phone needs to send it (the first, "control" connection).

The PC keeps only the newest list per device, in memory (never in its database), with the time it arrived.
When the device has no open connection the PC shows the last list greyed with "Last seen ...". Removing the
device drops its list.

Validation on the PC (any failure: `badRequest`, the stored list is unchanged): `jobs` is a list of at most
100 objects; each object has the required fields with the right types and lengths below; `id` values are
unique. Unknown fields are ignored. The PC shows every string as plain text (no rich text, no links that open
by themselves).

Phone-job object:

| Field | Type | Notes |
| --- | --- | --- |
| `id` | int, at least 1 | The phone's own job id |
| `name` | string, 1 to 256 | File or video name; never a path |
| `url` | string, at most 2048 | Source link with any `user:password@` removed; empty for a copy from a PC |
| `status` | string | `queued`, `downloading`, `paused`, `waiting`, `completed`, `failed`, `cancelled`. Anything else the PC shows as "Unknown" with no actions |
| `kind` | string | `direct`, `video`, `torrent`, `cloud`, `transfer` (a copy from a PC). Unknown: shown as `direct` |
| `downloaded` | int, at least 0 | Bytes |
| `total` | int or null | null when unknown |
| `speed` | int, at least 0 | Bytes per second; 0 unless `downloading` |
| `eta` | int or null | Seconds |
| `createdAt` | time | |
| `error` | string or null, at most 512 | Plain words; null unless `failed` |

### `phone.command` (event, PC to phone)

Sent only on a connection of that device that subscribed to the new topic `phone`
(`subscribe {topics: [..., "phone"]}`), at most one connection per device (the newest). Payload:

| Field | Type | Notes |
| --- | --- | --- |
| `commandId` | int, 1 to 2^31 - 1 | Unique per PC run; the PC matches the result to it |
| `action` | string | `pause`, `resume`, `cancel`, `retry`, `remove`, `moveToPc` |
| `jobId` | int | A phone job id from the newest `phoneJobs` list |

The PC sends a command only from its own window (a user action), at most 10 per second per device. A command
the phone has not answered in 15 s is shown as "The phone did not answer".

The phone checks `jobId` against its own database and that the action fits the job's state, then does what
its own Downloads screen does for that action. `moveToPc` does what the phone's "Send to PC" does, with the
commanding PC as the target: the link is sent to that PC with `download` (unattended, quality `best` or the
job's quality), and only after the PC replied `queued` the phone cancels its own job and removes its partial
file; a finished phone file is kept. A `transfer` job (a copy from a PC) cannot be moved: `refused`.

### `phoneCommandResult` (phone to PC)

Request fields: `commandId` (int), `result` (string: `ok`, `notFound`, `refused`, `failed`), `message`
(string or null, at most 256, plain words for the PC to show). Reply `ok`. Cost 1. An unknown or already
answered `commandId` is ignored (still `ok`). The phone sends a fresh `phoneJobs` list after a command changed
something.

### Security rules for this section

| Rule | Detail |
| --- | --- |
| Same trust | Only an authenticated device sends `phoneJobs` and `phoneCommandResult`; the PC accepts them only for that device. Commands go only to that device's connections. |
| No new files or paths | Nothing here reads or writes a file on the PC, opens a path, or starts a download on the PC except `moveToPc`, which reaches the PC through the normal `download` request and its rules (section 7). |
| Bounded | List size, string lengths, rate (the device's token bucket) and the 1 MiB frame limit apply. A device's list is replaced, never merged, so it cannot grow memory. |
| Phone in control | The phone setting turns the whole feature off; the phone refuses commands for jobs it does not have or states that do not fit. |
| Display | The PC shows phone strings as plain text, replaces bidirectional control characters (U+061C, U+200E, U+200F, U+202A-202E, U+2066-2069) in names, links, errors and command messages with U+FFFD, and never logs URLs (as for jobs today). |

Fixtures (protocol/fixtures): `phoneJobs.request.json`, `phoneJobs.request.notShared.json`,
`event.phone.command.json`, `phoneCommandResult.request.json`, and `authOk` with `phoneJobs` in `features`.
