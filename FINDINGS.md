# Overnight findings — glucobridge (BLE glucose meter library)

Work done overnight 2026-10-20/21 for three meters:
Beurer GL 49, Accu-Chek Guide, TESTAmed GlucoCheck Advance.

## What was built

`/opt/data/glucobridge` — pip-installable Python library + CLI, git repo,
29 passing tests. Python ≥3.10, asyncio, single dependency (bleak).

    pip install -e /opt/data/glucobridge
    glucobridge scan
    glucobridge pair AA:BB:CC:DD:EE:FF --passkey 123456
    glucobridge dump AA:BB:CC:DD:EE:FF --csv readings.csv
    glucobridge watch AA:BB:CC:DD:EE:FF

Library API:

    from glucobridge import GlucoBridge
    async with GlucoBridge() as gb:
        devices = await gb.scan()
        readings = await gb.download(devices[0].address)

Each `Reading`: value+unit, `.mgdl`/`.mmol_l`, timestamp, sequence,
sample type/location, status bits, meal marker (pre/postprandial, fasting,
casual, bedtime), carbs/medication/exercise/HbA1c when the meter sends
context records, CSV/JSON export.

## Protocol findings

### Beurer GL 49 — standard Bluetooth Glucose Service (confirmed)
* BLE GATT service 0x181F; records on 0x2A18 (notify), context on 0x2A34,
  history via Record Access Control Point 0x2A52.
* Third-party apps (Glooko, HealthManager Pro) read it over BLE — the
  interface is not Beurer-locked.
* Pairing: passkey display — meter shows a 6-digit code, central enters it.
  `glucobridge pair --passkey` registers a temporary BlueZ agent that
  answers the prompt; keys persist in BlueZ.
* Transfer trigger: powering off, opening the memory, or ejecting the strip.
* Full history (up to 900 records) pulled with RACP
  `01 01` (report stored records / all); completion signalled by RACP
  response `06 00 01 <result>`.

### Accu-Chek Guide — standard Glucose Service (confirmed)
* Continua + Bluetooth SIG Glucose Profile certified; same code path as the
  GL 49. Name pattern "ACU-CHEK GUIDE*" auto-classified by the scanner.
* Roche's SmartGuide app uses the same standard service; no
  proprietary layer needed.

### Record formats (grounded in the Bluetooth SIG GSS YAML, SIG public repo)
* Glucose Measurement 0x2A18: flags, seq u16, base time (7B Date Time),
  optional int16 time offset (flag bit0), concentration SFLOAT16 +
  type/sample-location byte (flag bit1), unit mg/dL vs mmol/L = flag bit2,
  sensor status u16 (flag bit3), context-available flag bit4.
* Context 0x2A34: carbs (SFLOAT), meal enum (1=preprandial 2=postprandial
  3=fasting 4=casual 5=bedtime), exercise, medication, HbA1c.
* SFLOAT16 = IEEE 11073-20109: 4-bit signed exponent (base 10), 12-bit
  signed mantissa; specials 0x07FB..0x07FF handled.
* A lenient fallback parser handles the legacy pre-GSS layout (4-byte time
  offset, unit bit inside the concentration field) that older certified
  meters emit — both layouts covered by unit tests.
* RACP: op 0x01 report stored / 0x04 count / 0x03 abort; operators
  0x01 all, 0x05 first, 0x06 last, 0x03 GE with sequence-number filter
  operand; responses 0x05 count, 0x06 result (0x01 success, 0x06 no
  records...). Delete (0x02) exists but is never called implicitly.

### TESTAmed GlucoCheck Advance — proprietary FORA protocol (reverse-engineered, backend implemented)
* Not a Glucose Service device. Protocol reverse-engineered and
  live-verified; the meter is a FORA Care product. Full spec: FINDINGS-fora.md.
* Transport: BLE GATT service FFF0, single write+notify characteristic
  FFF1. No pairing/bonding at all — connect, subscribe, talk.
* Framing: 8-byte frames `51 CMD p2 p3 p4 p5 A3 CK`,
  CK = sum(first 7 bytes) & 0xFF; meter echoes the CMD byte. Strictly
  request/response; CMD 0x50 is a fire-and-forget protocol reset.
* History: CMD 0x2B record count (total = b3<<8|b2), then per index
  CMD 0x25 (bit-packed date: day/month/year-2000/minute/hour + "already
  transmitted" flag) and CMD 0x26 (value LE16 — integer mg/dL, or x0.1 on
  decimal models TD4240/TD4230/TD3132/TD4127/TD4282/TD4283; ambient temp;
  meal marker 0=general 1=AC pre-meal 2=PC post-meal 3=QC control
  solution; error codes). CMD 0x33 syncs the meter clock (exact inverse of
  the date decode). CMD 0x24 identifies the meter by FORA project code
  (e.g. TD4240) — brand names never appear on the wire.
* Implemented as the `fora` backend: auto-selected on FFF0 advertisements
  or GlucoCheck*/FORA*/TD* names; `download()`, `watch()` (poll-based —
  these meters never push), `sync_time()`, `identify()`. Covered by 11
  tests incl. a simulated TD4240 end-to-end download.
* Open question for live test: which TD project code the GlucoCheck
  Advance reports (determines integer vs x0.1 value parsing; the code
  auto-selects the parser, worst case a one-line set membership fix).

## Testing status
* 39 unit/integration tests pass: SFLOAT vectors, both GLS record layouts,
  context merge, RACP framing, FORA frame/checksum/date/value codec, plus
  simulated meters exercising the full GLS RACP download and the full FORA
  record-by-record download end-to-end — no hardware needed.
* No Bluetooth adapter on this build host — live radio testing must happen
  on the target machine. First contact checklist:
  1. `glucobridge scan` (meter on, close by)
  2. `glucobridge pair <addr> --passkey <6-digit code>`
  3. `glucobridge info <addr>` (GATT tree + record count)
  4. `glucobridge dump <addr>`
* If a meter deviates from spec, `info` prints the GATT tree and the
  parser keeps raw bytes on each Reading for diagnosis.

## OneTouch "Verio" protocol (AF9DF7A1) — Ultra Plus Reflect & co.

**Sources (cross-validated):** xDrip's `VerioHelper` (byte-exact
reference for the Verio Flex) and Glucera's release notes for the
Reflect driver. One `AF9DF7A1/2/3` service covers `ULTRA_PLUS_REFLECT`,
`VERIO_REFLECT`, `VERIO_FLEX`, `ULTRA_PLUS_FLEX`, `ONE_TOUCH_SELECT_PLUS_FLEX`
— one protocol family.

**Transport:** service `AF9DF7A1-E595-11E3-96B4-0002A5D5C51B`, write
`AF9DF7A2…`, notify `AF9DF7A3…`. Some models require pairing (iOS shows
a prompt; just-works).

**Framing:**
```
request : 01 02 <size> 00 03 <payload...> 03 <crc16 lo> <hi>   size = len(payload)+7
response: 01 02 <len-1> 00 03 <type> <data...> 03 <crc lo> <hi>
```
CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, check value 0x29B1) over
the bytes from `0x02` through the trailing `0x03`, output little-endian.
(xDrip's asymmetric skip flags both reduce to this.) ACKs are a single
`0x81` byte in both directions: meter acks each command write, host
acks each data frame.

**Commands (payload):** meter time `20 02` · highest record number
`27 00` · stored record count `0A 02 06` · record N `B3 <lo> <hi>`.

**Responses** (type `0x06` = data; `0x07/08/09` = not allowed / not
supported / not understood): 4-byte result = meter unix time
(`LE32 + 946684799`) **or** record counter — disambiguated by the
pending command, never by value magnitude (Glucera's fix); 2-byte =
record count (int16 LE); 11-byte record = `LE32` seconds offset +
`int16` mg/dL + 5 metadata bytes.

**Reflect specifics:** record metadata bytes 6–10 are non-zero "Blood
Sugar Mentor" data on the Reflect (zero on Verio Flex — xDrip's
sanity-check there would reject Reflect records). Meal tags live in metadata
byte 1 (0x01 before meal, 0x02 after), cross-confirmed against the USB
dump. The
backend keeps the metadata raw on `Reading.status`
(`mentor_metadata_<hex>`) for later decoding. Timestamps get phone-vs-
meter drift correction (meter clock is offset, not absolute).


## Live test results (2026-10-21, real hardware)

**TESTAmed GlucoCheck Advance** (`C0:26:DA:55:00:BB`, fw V1.2.29):
standard Glucose Service 0x1808 (0x2A18/0x2A34/0x2A52) — the FORA
rebrand theory was wrong; the Nordic UART service it also exposes is a
decoy for glucose purposes. RACP `01 01` download works. One quirk:
the concentration field carries the *formal* GSS SI unit kg/L (SFLOAT
0xb07c = 124e-5 kg/L), not the practical mg/dL most meters send —
`_postprocess` reinterprets values < 1 (kg/L → mg/dL, mol/L → mmol/L).
Live vector: `030000ea0701140f000000007cb011` = 124 mg/dL,
2026-01-20 15:00, capillary whole blood, finger (meter clock off — set
it via the meter's SET menu; the device exposes no Current Time service).

## Open items
* Live-test the GL49, Accu-Chek Guide and OneTouch Reflect radios
  (GlucoCheck Advance: done 2026-10-21).
* Live-capture OneTouch Reflect records with meal/mentor tags set to
  decode the metadata bytes 6–10 (currently kept raw).
* Optional: USB backend for the TESTAmed (manual says built-in USB with
  "USb" mode) and the GL 49 (mini-USB serial) as a fallback path.
* Optional: Home Assistant / MQTT bridge on top of `watch()`.
