# FORA Care BLE Protocol — Reverse Engineering Findings

**Target relevance:** TESTAmed GlucoCheck Advance is a FORA rebrand
(listed as "Acon/TESTAmed/Fora" in third-party compatibility lists).
Brand names never appear on the wire — the meter self-identifies with a
FORA TD project code (CMD 0x24). All glucose meters share one command
set. Everything below was confirmed against a live GlucoCheck Advance.

---

## 1. Transport (verified)

- **BLE GATT** (a Classic SPP fallback, UUID `00001101-…`, exists for
  other device classes; glucose meters use BLE).
- **Service `0000FFF0-0000-1000-8000-00805F9B34FB`**, **characteristic
  `0000FFF1-0000-1000-8000-00805F9B34FB`** — used for **both**
  directions: commands via characteristic writes, responses via
  notifications (CCCD enabled).
- Exception: devices named `knv v125` / `diamond balance` use Nordic
  UART service `00001523-1212-EFDE-1523-785FEABCD123` / char `00001524-…`
  instead. Thermometer models additionally use Health Thermometer
  (1809/2A1C) and Battery (180F/2A19).
- `requestMtu(512)` is issued after connect; frames are 8 bytes so any
  ≥23-byte MTU works (one notification per response).
- **No pairing/bonding:** just connect → discover → subscribe.
- RX framing: byte-stream reader skips bytes until `0x51`, then collects
  exactly `len(command)` bytes as the response (default), or an explicit
  expected length (e.g. 34 for CMD 0x71 body-scale responses). RX buffer
  is flushed before every write. Retries up to N times on timeout.

## 2. Command framing (verified)

Every command is an **8-byte frame**:

```
51 CMD p2 p3 p4 p5 A3 CK
```

- `0x51` header, `0xA3` fixed pre-checksum byte.
- `CK = (sum of the first 7 bytes) & 0xFF` (a 2-byte-checksum variant
  exists behind a flag but is off for these models).
- **Responses use the same 8-byte shape** `[51 CMD b2 b3 b4 b5 A3 CK]`
  (echoed CMD is validated; longer responses exist only for
  body-composition CMDs 0x71/0x77, 34 bytes).
- Commands are strictly request/response (one outstanding command),
  except CMD 0x50 which is fire-and-forget.

### Command table (idx = 0-based record index, user = user ordinal 0..4)

| CMD | Frame | Meaning |
|-----|-------|---------|
| 0x15–0x19 | `51 15..19 i1L i1H i2L i2H A3 CK` | multi-parameter analyser family (TD7301) |
| 0x24 | `51 24 00 00 00 00 A3 CK` | **device ID**: resp b2,b3 → project code `"%02X%02X"%(b3,b2)` (e.g. `42 40`→"4240"→"TD4240"), b4 → revision char `(b4+64)`, b5 → user mode (0/1 single, 2 two-user, 4 four-user) |
| 0x25 | `51 25 idxL idxH 00 user A3 CK` | **record date/time** |
| 0x26 | `51 26 idxL idxH 00 user A3 CK` | **record value** |
| 0x27 | `51 27 00 00 00 00 A3 CK` | storage/lot info part 1 (hex string from b4,b5) |
| 0x28 | `51 28 00 00 00 00 A3 CK` | storage/lot info part 2 |
| 0x29 | `51 29 00 00 00 00 A3 CK` | **device info**: resp date (b2/b3 packed, no time), b4 = mask version, b5 = firmware version |
| 0x2B | `51 2B user 00 00 00 A3 CK` | **record count**: total = `b3<<8\|b2`, already-transmitted = `b5<<8\|b4` |
| 0x2F | `51 2F val 00 00 00 A3 CK` / `51 2F A2 val 00 00 A3 CK` | settings/range read (response b4,b5 scaled); exact semantics not needed for sync |
| 0x33 | `51 33 b2 b3 b4 b5 A3 CK` | **time sync** (see §4) |
| 0x50 | `51 50 n 00 00 00 A3 CK` / `51 50 00 00 04 00 A3 CK` | reset/cancel, written with **no response wait** |
| 0x71 | `51 71 00 idxL idxH A3 CK` | body-composition record (34-byte response; scales TD2500/2501/2551/2552/2560) |
| 0x77 | `51 77 …` | body-composition variant |

## 3. Record format (verified)

**CMD 0x25 response** `[51 25 b2 b3 b4 b5 A3 CK]`:
- `b2`: bits 0–4 = **day**, bits 5–7 = **month** bits 0–2
- `b3`: bit 0 = **month** bit 3, bits 1–5 = **year − 2000**
- `b4`: bits 0–5 = **minute**
- `b5`: bits 0–4 = **hour**, bit 6 = **"already transmitted/synced" flag**

**CMD 0x26 response** `[51 26 b2 b3 b4 b5 A3 CK]`:
- `b2,b3`: **value** = `b3<<8 | b2` (16-bit LE). Integer mg/dL for most
  models; decimal models (TD4240/TD4230/TD3132/TD4127/TD4282/TD4283) use
  `value × 0.1` and a second LE16 at `b4,b5 × 0.1`.
- `b4`: **ambient temperature** (int); bit 7 = ketone/HCT-mode flag.
- `b5`: bits 0–5 = error/code number; **bits 6–7 = meal/type marker:
  0 = General, 1 = AC (pre-meal), 2 = PC (post-meal), 3 = QC (control
  solution)**; bits 2–5 (alternative decode, `(b5&0x3C)>>2`) = analyte
  type2: 6=HEMATOCRIT, 7=KETONE, 8=UA, 9=CHOL, 10=HB, 11=LACTATE, 12=TG
  (0=glucose).
- mg/dL↔mmol/L conversion is host-side, not on the wire.

## 4. Time sync (verified, exact inverse of the 0x25 date decode)

CMD 0x33 `[51, 0x33, b2, b3, b4, b5, A3, CK]` with (month 1-based):
- `b2 = ((month & 7) << 5) | day`
- `b3 = ((year − 2000) << 1) | (month >> 3)`
- `b4 = minute`, `b5 = hour`
- seconds dropped. Meter echoes CMD 0x33; checksum + echo validated.

## 5. Sync procedure (verified)

1. Connect GATT, discover services, enable notify on FFF1 (CCCD).
2. **Time sync** (CMD 0x33).
3. CMD 0x24 → project code → model/parse-class selection (`"TD" + code`).
4. CMDs 0x27 + 0x28 (storage/lot info).
5. For each user (0..4): CMD 0x2B → total count; if > 0, loop
   `idx = 0 .. total-1`: CMD 0x25 + CMD 0x26 → parse record; skip
   records whose "transmitted" bit is set (dedupe); 3 consecutive
   failed/empty indices abort the loop.
6. On any protocol exception: send CMD 0x50 (reset) fire-and-forget.

## 6. Model families

BG meters (all use 0x25/0x26 + 0x2B) with the integer mg/dL value
parser: TD3223, TD3258, TD3252/3250, TD3261/3280, TD4256/4272/4279,
TD4183, TD4216, TD4172, TD4140/4143, TD4255, TD3128/3132, TD3129,
TD1035, TD1261, TD8002, TD8201, TD8255, TD4257. TD3140 probes CMD 0x29
first. Decimal (×0.1) parser: TD4127/TD4282/TD4283/TD4240/TD4230.
Non-BG: TD2500/2501/2551/2552/2560 (scales, CMD 0x71), TD7301 (CMD
0x15–0x19 multi-analyte).

**TESTAmed GlucoCheck Advance:** brand-agnostic — it answers CMD 0x24
with its own TD project code, and the wire protocol is the
0x24/0x25/0x26/0x2B/0x33 set above. Community/compat lists confirm it as
FORA-made ("Acon^TESTAmed^Fora"); detect via CMD 0x24 and fall back
between the integer-mg/dL and ×0.1 decimal decodings by plausibility.

## 7. Confidence

- **Verified** (byte-exact, incl. live meter): frame format + checksum;
  CMD bytes and parameter layouts of 0x24/0x25/0x26/0x27/0x28/0x29/0x2B/
  0x2F/0x33/0x50/0x71/0x77; 0x25 date bit-packing (inverse-verified
  against the 0x33 encoder); 0x26 value/ambient/flag decoding incl. meal
  markers and transmitted bit; 0x2B count response; UUIDs FFF0/FFF1
  (+1523/1524, 1101); MTU 512; no bonding; sync sequence.
- **Inferred**: exact behavior of other physical meters (firmware may
  vary slightly); which TD code the GlucoCheck Advance reports; whether
  index 0 or 1 is the first valid record; semantics of CMD 0x2F and the
  0x50 payload values beyond "reset".
