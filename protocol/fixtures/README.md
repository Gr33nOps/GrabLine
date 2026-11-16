# Protocol fixtures (version 6)

Canonical examples for `../PROTOCOL.md`. The desktop tests (pytest) and the Android tests (JUnit) load the same files, so both sides agree on every message shape. Change a fixture only together with `PROTOCOL.md`, and copy the folder to the Android repository in the same change.

## File format

Every `.json` file except `manifest.json` and `vectors.json` is one object:

| Field | Required | Meaning |
| --- | --- | --- |
| `direction` | Yes | `phone-to-pc` or `pc-to-phone` |
| `description` | Yes | What the case shows |
| `state` | Yes | `preauth` (before `authOk`) or `authed` |
| `message` | Yes | The exact JSON object sent in one WebSocket text frame (`null` only in `invalid/notJson.marker.json`) |
| `expectError` | Invalid cases | The `code` of the error reply the PC must send |
| `expectClose` | Some | The WebSocket close code the PC must use after replying (or instead of replying, for 1009 and 4400) |
| `precondition` | Some | State the test must set up first |
| `expectIgnored`, `expectReplyType` | Some | Fields the receiver must ignore, and the reply type it must still give |
| `generate` | `*.marker.json` | How the test builds a message too big or too repetitive to store (see below) |
| `raw` | `notJson.marker.json` | The literal frame text to send |

## Names

`<type>.request[.<variant>].json`, `<type>.reply[.<variant>].json`, `event.<event name>.json`, `error.<code>.json`. `invalid/` holds requests the PC must refuse.

Text files: `qr.uri.txt` (the pairing QR content), `qr.manual.txt` (manual entry: address line, code line), `fetch.frame.hex.txt` (one binary fetch frame as hex).

## Marker files

| File | Build |
| --- | --- |
| `invalid/oversize.marker.json` | Replace `message[padField]` with `padChar` repeated until the compact UTF-8 JSON is `totalBytes` long |
| `invalid/links.tooMany.marker.json` | Fill `message[listField]` with `count` strings from `template`, `{n}` = 1..count |
| `invalid/flood.marker.json` | Send `message` (fresh `id` each time) `repeatPerSecond` times a second for `seconds` |
| `invalid/notJson.marker.json` | Send `raw` as the text frame |

## Sample values

All values are fixed so tests are repeatable.

| Value | Where from |
| --- | --- |
| Times | `1794700000` and offsets from it |
| Nonce | Bytes `00` to `1f` |
| Pin | SHA-256 of the ASCII text `grabline-test-pc-spki` (stands in for a real certificate key hash) |
| Pairing code | `7K3M9QX2RTBW` |
| Device key | A TEST-ONLY P-256 key derived from the public text `grabline-test-device-key-1`. Never use it outside tests. |
| Proofs and signature | Really computed, so `pair.request.json`, `pair.reply.paired.json` and `auth.request.json` verify against each other. Inputs and results are in `vectors.json`. |

`manifest.json` lists every file with its direction, state, message type and expected error. A test should fail if a file exists that the manifest does not list (other than `README.md` and `manifest.json` themselves), or the other way round.

For cases closed with 1009 or 4400 the PC sends no error reply; `expectError` there names the error class only.
