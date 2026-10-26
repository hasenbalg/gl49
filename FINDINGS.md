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
* BLE GATT service 0x1808; records on 0x2A18 (notify), context on 0x2A34,
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

**OneTouch Ultra Plus Reflect** (`EA:7B:85:08:A1:D9`, fw SI_04.01.03):
advertises the standard Glucose Service UUID as a **decoy** (no 2A18 in
the real GATT) next to its AF9DF7A1 service — name-based routing wins.
Pairs with passkey entry (meter displays the code). The record-counter
command `27 00` answers with **2 bytes** (Flex: 4); records are the same
11-byte layout as the Flex. 8/8 records downloaded with correct
timestamps; 0 mg/dL entries correspond to failed attempts (meter `Er`
codes). Meal/mentor metadata bytes still kept raw pending tagged
samples.

**BLE watermark bug (live, 2026-10-21):** the `27 00` record counter
under-reports memory — readings taken while Bluetooth was off do not
bump it (counter said 8 while USB showed 10 slots; the two newest
readings were invisible to BLE download). Fixed by probing record
numbers past the watermark until the meter rejects two in a row.

**BLE meal tag CRACKED (live, 2026-10-21):** metadata[1] (record byte
7) is the meal flag — 0x01 before / 0x02 after / 0 none — confirmed by
cross-checking a USB `after` reading (USB meal_flag 2) against its BLE
record (`mentor_metadata_0002000000`). Now surfaced as `Reading.meal`.

**OneTouch Ultra Plus Reflect over USB** (2766:0010, live-verified
2026-10-21): enumerates as USB mass storage with a doorbell FAT volume
(label LIFESCAN, one startup.htm); data path is the documented Verio
2015 SCSI register protocol (READ/WRITE(10) to LBA 3/4/5, same
LifeScan shared binary packet + CRC-16/CCITT-FALSE as BLE, different
framing). Quirks found live:
* the medium is only present while the FAT volume is MOUNTED —
  unmounting ejects it (SCSI NOT READY / MEDIUM NOT PRESENT);
* records carry native meal flags (0 none / 1 before / 2 after) — the
  Reflect's Test Tracker tags tests automatically by time of day;
* USB record count excludes meter error slots (BLE shows them as
  0-value E-records);
* record timestamps are the raw meter clock (local, can be minutes
  off), unlike the drift-corrected BLE timestamps.

## Beurer GL 49 over USB — CP2110 HID-UART, FORA-family frames

The GL 49's mini-USB port is a Silicon Labs **CP2110 HID USB-to-UART
bridge** (`10c4:ea80`, serial `013FF8E4`); the meter's MCU hangs off
the bridge's UART pins. The wire protocol (extracted from the vendor
PC software, live verification pending) is the same FORA-family frame
set as the GlucoCheck Advance, at a different speed:

* transport: CP2110 opened via the HID-UART API, **19200 baud 8N1**,
  read timeout ~1 s, ~50 ms between frames;
* framing: 8-byte `51 CMD p2 p3 p4 p5 A3 CK`, `CK = sum(first 7) & 0xFF`,
  meter echoes the CMD byte;
* `0x24` device ID, `0x29` device info (date/mask/firmware), `0x2B`
  record count (`b3<<8|b2`), `0x27`/`0x28` lot info;
* **`0x85 idxL idxH` record fetch**: 16-byte response
  `51 85 <12 data bytes> A5 CK` carrying TWO 6-byte records —
  idx N returns records `2N` and `2N+1` (zero-padded past the count;
  fetch idx 0..ceil(count/2)-1, trailer `0xA5` at offset 14);
* record format — the PC first byte-swaps every 16-bit word of the wire
  bytes (`0x418e12`), then the parser (`0x418fa0`) reads, on the swapped
  bytes `r0..r5`: `value = r5 | ((r4 & 3) << 8)`, `year = 2000 + (r0 >> 1)`,
  `month = (r1 >> 5) | ((r0 & 1) << 3)` (1 = January), `day = r1 & 0x1F`,
  `hour = r2 & 0x1F`, `minute = r3 & 0x3F`, `meal mark = r2 >> 5`
  (0 none, 1 preprandial/before meal, 2 postprandial/after meal,
  3 general marking). Manual §5.3: the mark key CYCLES — once = before
  meal, twice = after, thrice = general, 4th press clears; marking is
  only possible while the result is on screen, stored on power-off.
  Ordering equals the SIG glucose-context meal table (1=preprandial,
  2=postprandial), confirmed live via the GL49's own BLE context for
  the same record (single apple press -> USB mark 1 -> BLE context
  meal code 1). An earlier revision mapped 1=after on a misremembered
  button meaning — wrong, reverted.)
  `r3>>6` and `r4>>2` are ignored by the vendor app (`r4>>2` drifts with
  ambient warmth over the day — possibly temp+10 degC, unconfirmed);
* enumeration quirks: only enumerates on some ports (marginal
  connector); needs the NEW USB enumeration scheme
  (`old_scheme_first=0`); meter must show the USB symbol (connect
  while switched OFF).

`tools/gl49_probe.py` speaks this protocol (24/29/2B/85/27/28 frames at
19200, then 9600).

## GL49 BLE (live-verified 2026-10-26)
* Advertises as `GL49` (no space); real Glucose Service `0x1808`
  (0x2A18/0x2A34/0x2A51/0x2A52 + CCCDs), battery, device info, plus a
  Nordic UART decoy `0x1523` (same as GlucoCheck). Standard RACP
  `report stored records` download works — all records match the USB
  transport to the minute.
* Pairing = passkey entry, code generated per attempt: use the
  interactive prompt (a pre-passed `--passkey` is always stale).
  MUST be a clean single-agent session: `pkill bluetoothctl` first —
  a competing/stale default agent steals `RequestPasskey` and the
  attempt dies as misleading `Authentication Canceled`/
  `AuthenticationFailed`. One `pair` attempt at a time (overlap ->
  `InProgress` + confirm failure). Meter shows `PA` in pairing mode,
  `nOK` on reject. After bonding, all tools connect agent-free.
* Marked readings carry a context record (measurement flags `0x12`
  vs `0x02` unmarked); context meal code order equals the SIG table —
  the single apple press tags `preprandial` on both transports.

## Live test round 2 (2026-10-26, Raspberry Pi host `bt-test`)

- **Library pairing works end-to-end on GL49**: `glucobridge pair` (our own
  BlueZ agent, KeyboardDisplay) → `RequestPasskey` → passkey typed →
  bonded + trusted in ~11 s. Two earlier attempts died `Page Timeout`
  because the meter was not in/connectable from its pairing window —
  that error simply means "wake the meter, retry immediately".
- **Accu-Chek Guide seen live for the first time** (`meter+49080084`,
  `A0:D9:1A:60:5D:EE`): paired via library passkey agent fine, but the
  first `dump` hit `Multiple Characteristics with this UUID` — the Guide
  exposes duplicate characteristic UUIDs across services. Fixed by
  resolving every lookup through `GlucoseMeter._char()`, which prefers
  the standard Glucose Service (0x1808) and passes the characteristic
  object (handle-pinned) to bleak instead of a bare UUID.
  **Fix confirmed live**: bare `dump` afterwards returned readings with
  meal tags (`97 mg/dL [preprandial]`).
- **GlucoCheck Advance answers standard GLS/RACP**: paired Just Works
  (no agent interaction) and `dump --backend gls` returned 6 readings —
  the proprietary `fora` backend is a fallback, not the only path.
  (Its RTC was at 2026-01 defaults; values match earlier live runs.)
- Gotcha on fresh hosts: Raspberry Pi Bluetooth ships soft-blocked —
  `rfkill list` / `rfkill unblock bluetooth` before anything else.
- Transient `Device with address ... was not found` immediately after
  pairing = meter stepped out of range of BlueZ's cache between calls;
  a plain retry works.

## Open items
* Live-test the Accu-Chek Guide radio
  (GlucoCheck Advance + Ultra Plus Reflect BLE + USB: done 2026-10-21;
  GL49 USB: done 2026-10-22 — `gl49_usb` backend, live-verified;
  GL49 BLE: done 2026-10-26 — pairing + `gls` backend, live-verified;
  Accu-Chek Guide BLE: done 2026-10-26 — pairing + download after the
  duplicate-characteristic fix).
* Optional: Home Assistant / MQTT bridge on top of `watch()`.
