# UVM Verification of an Open-Source PCIe Completer (`pcie_axil_master`)

A UVM-style verification environment (written in **pyuvm**, the Python
implementation of UVM, running on **cocotb + Icarus Verilog**) for the
`pcie_axil_master` block from Alex Forencich's open-source
[`verilog-pcie`](https://github.com/alexforencich/verilog-pcie) library
(commit `25156a9`).

The DUT is a PCIe **completer**: it receives request TLPs (Memory Read/Write,
IO, Messages, ...) on a TLP streaming interface, turns memory requests into
AXI4-Lite transactions, and returns completion TLPs.

**Result:** the environment found **two PCIe protocol violations** in the
RTL, root-caused both, and verified RTL fixes. With the fixes applied the full
regression passes at 64/128/256-bit TLP datapath widths with **100% functional
coverage**.

---

## Environment

```
            +-------------------------- PcieEnv --------------------------+
 sequences  |  uvm_sequencer --> PcieDriver ---------------------------+   |
 (random /  |                     |  MRd/MWr/IO via PCIe root-complex   |   |
  error /   |                     |  model; Msg / stray Cpl injected as |   |
  sweep)    |                     |  raw TLP frames                     v   |
            |                     |                         +------------+  |
            |                     |                         |    DUT     |  |
            |                     |                         | pcie_axil_ |--+--> AXI4-Lite RAM model
            |                     |                         |  master    |  |
            |                     |                         +------------+  |
            |                     |                               | Cpl TLPs|
            |                     |                         CplMonitor      |
            |                     |                  (passive; strobe/length|
            |                     v                   protocol checker)     |
            |      PcieScoreboard (byte-accurate memory reference model +   |
            |                      PCIe rule checks)                        |
            |      PcieCoverage   (8 coverpoints/crosses, 43 bins)          |
            +---------------------------------------------------------------+
```

| Component | What it does |
|---|---|
| `PcieReqItem` | Sequence item: request kind, address, length, data + observed results |
| `RandomMemSeq` | Constrained-random MRd/MWr: 0..256 bytes, unaligned starts, starts just below 4 KB pages |
| `ErrorInjectSeq` | IO read/write, Vendor-Defined Msg / MsgD, unexpected completions |
| `BoundaryReadSeq` | Directed sweep: 1..40 DW reads starting just below 128-byte / 4 KB boundaries |
| `MixedSeq` | Random interleaving of legal and illegal traffic |
| `PcieDriver` | Drives memory/IO requests through the cocotbext-pcie root-complex model; injects raw TLP frames for messages and stray completions |
| `CplMonitor` | Passive monitor on `tx_cpl_tlp_*`: decodes every completion header and checks that the data strobe/length match the header, beat by beat |
| `PcieScoreboard` | Reference memory model (checks every read, and AXI memory incl. guard bytes after every write); checks *posted requests are never completed*, IO gets UR, stray completions flag an error, legal traffic raises no error |
| `PcieCoverage` | Request kind, length bins, alignment, 4 KB-page crossing, back-pressure, kind×length, kind×back-pressure, read RCB-split type |

Random **back-pressure** is applied on every valid/ready handshake on both the
TLP and AXI-Lite sides in most tests.

## Bugs found

### Bug 1 — Posted Message TLPs receive an Unsupported Request completion

* **Symptom:** every injected Vendor-Defined Type 1 `Msg`/`MsgD` (posted
  requests) was answered with a **UR completion**. PCIe never allows a
  completion for a posted request.
* **Root cause:** Verilog operator precedence (`rtl/pcie_axil_master.v:550`):

  ```verilog
  if (rx_req_tlp_hdr_fmt[0] && rx_req_tlp_hdr_type & 5'b11000 == 5'b10000)
  ```

  `==` binds tighter than `&`, so this is `type & (5'b11000 == 5'b10000)` =
  `type & 0` — **always false**. Messages fall into the "other non-posted
  request" branch and get a UR completion.
* **Fix:** `(rx_req_tlp_hdr_type & 5'b11000) == 5'b10000`
* **Status:** the same pattern exists in `pcie_axi_master_rd.v` and
  `pcie_axil_master_minimal.v` (by inspection). The author's newer `taxi`
  rewrite already uses the parenthesised form, i.e. this is fixed there.

### Bug 2 — Malformed completion when a >32 DW read starts one DW before a 128-byte boundary

* **Symptom:** a read of more than 32 DW whose start address has
  `addr[6:2] == 31` (e.g. `0x0DFC`) produces a completion whose header says
  **Length = 1 DW**, but the DUT keeps streaming the rest of the read data in
  the same packet (strobe asserted for DWs beyond the header length; EOP
  arrives many beats late). Found first by constrained-random stimulus (seed
  1793393833), then characterised with the directed `BoundaryReadSeq` sweep:
  66 protocol-checker violations on the original RTL.
* **Root cause:** reads > 32 DW are split into completions aligned to the
  128-byte RCB, first chunk = `32 - addr[6:2]` DW. The "last DW of this
  completion" flag for the **first** DW of a chunk only checks whether the
  whole *operation* is ending (`req_op_dword_count == 1`), never whether the
  *chunk* is one DW long — so a 1-DW chunk is never terminated, and the state
  machine continues in `READ_2` instead of starting a new completion.
* **Fix** (`docs/fixes.patch`): set `last` when the chunk is 1 DW, and go to
  `READ_1` (start a new completion) instead of `READ_2` when that chunk is
  already done — in both the `IDLE` and `READ_1` states.
* **Status:** the same logic appears to be present in the current `taxi`
  rewrite (`taxi_pcie_axil_master.sv`, commit `cc70b27`, by inspection; not
  simulated there).

## Results

| Configuration | Original RTL | Fixed RTL |
|---|---|---|
| 64-bit TLP | 4/4 tests fail | 4/4 pass |
| 128-bit TLP (3 seeds, random tests) | — | 6/6 pass |
| 256-bit TLP | 4/4 tests fail | 4/4 pass |

Per configuration: ~1,200 transactions checked by the scoreboard
(500 random memory, 200 mixed, 30 error-injection, 480 boundary sweep), 0
errors on the fixed RTL. Merged functional coverage: **100% (43/43 bins)**.

## Running

```bash
pip install cocotb==1.9.2 pyuvm==3.0.0 cocotbext-pcie cocotbext-axi cocotb-bus
sudo apt install iverilog

cd tb
make                                   # original RTL, all tests (fails: bugs)
make RTL=pcie_axil_master_fixed        # fixed RTL
make RTL=pcie_axil_master_fixed TLP_DATA_WIDTH=256
make TESTCASE=boundary_sweep_test WAVES=1
python3 ../scripts/merge_coverage.py ../reports/pcie_axil_master_fixed_w64
```

## Files

```
rtl/pcie_axil_master.v          DUT, unmodified from verilog-pcie @ 25156a9 (MIT)
rtl/pcie_axil_master_fixed.v    DUT with both fixes
docs/fixes.patch                diff of the fixes
docs/upstream_issues.md         draft bug reports for verilog-pcie / taxi
.github/workflows/regression.yml  CI regression (3 TLP widths)
tb/pcie_uvm_env.py              pyuvm environment (items, sequences, driver,
                                monitor/checker, scoreboard, coverage)
tb/test_pcie_axil_uvm.py        harness + tests
tb/pcie_if.py                   TLP interface model from verilog-pcie (MIT)
scripts/merge_coverage.py       merges per-test coverage
```

## Continuous integration

`.github/workflows/regression.yml` runs the full regression on the fixed RTL
for 64/128/256-bit TLP widths on every push and prints merged coverage.

## Upstream reports

Draft issue reports with reproduction commands: `docs/upstream_issues.md`.

## Next steps

* Port the environment to SystemVerilog UVM (Cadence Xcelium) with
  SV covergroups and SVA protocol assertions on the TLP interface
* Formal proof of the completion-length/strobe property with JasperGold
