# pcie-axil-uvm

Verification of `pcie_axil_master` from Alex Forencich's
[verilog-pcie](https://github.com/alexforencich/verilog-pcie) (commit `25156a9`),
using pyuvm + cocotb on Icarus Verilog.

The block takes PCIe request TLPs, turns memory reads/writes into AXI4-Lite
transactions, and sends back completion TLPs.

I found two bugs in it and fixed both. With the fixes, all tests pass at
64, 128 and 256-bit TLP widths.

## Testbench

- **Sequences:** random memory reads/writes (unaligned, near 4 KB pages, 0-256 bytes),
  error injection (IO requests, messages, stray completions), and a sweep of reads
  starting just before 128-byte boundaries
- **Driver:** sends requests through the cocotbext-pcie root complex model; messages
  and stray completions are injected as raw TLPs
- **Monitor:** watches the completion output and checks that strobe and EOP match
  the length in each header
- **Scoreboard:** keeps a memory model and checks every read and write; also checks
  that posted requests never get a completion and IO requests get UR
- **Coverage:** request type, length, alignment, 4 KB crossing, back-pressure,
  read split type (43 bins)

Random back-pressure is applied on both the TLP and AXI-Lite sides.

## Bug 1: completions sent for posted messages

The DUT sends an Unsupported Request completion for every Message TLP.
Messages are posted, so they should never get a completion.

Cause, in `rtl/pcie_axil_master.v` line 550:

```verilog
if (rx_req_tlp_hdr_fmt[0] && rx_req_tlp_hdr_type & 5'b11000 == 5'b10000)
```

`==` has higher precedence than `&`, so this is always false. Fix:

```verilog
if (rx_req_tlp_hdr_fmt[0] && (rx_req_tlp_hdr_type & 5'b11000) == 5'b10000)
```

This is already fixed in the author's newer repo (taxi).

## Bug 2: bad completion for some long reads

A read longer than 32 DW that starts one DW before a 128-byte boundary
(e.g. address `0x0DFC`) gives a completion whose header says 1 DW, but the
rest of the read data keeps coming in the same packet.

Reads over 32 DW are split at 128-byte boundaries. When the first piece is
only 1 DW, the "last" flag never gets set for it, so that completion never
ends. Fix: set "last" when the piece is 1 DW and start a new completion after
it (`docs/fixes.patch`).

The random test hit this first; the boundary sweep then showed it on every
matching read. The same code looks unchanged in taxi (I haven't simulated it there).

## Results

| TLP width | Original RTL | Fixed RTL |
|---|---|---|
| 64-bit | fails | passes |
| 128-bit | - | passes (3 seeds) |
| 256-bit | fails | passes |

About 1,200 transactions checked per run, 0 errors on the fixed RTL,
100% functional coverage.

## Running

```bash
pip install cocotb==1.9.2 pyuvm==3.0.0 cocotbext-pcie cocotbext-axi cocotb-bus
sudo apt install iverilog

cd tb
make                                  # original RTL (fails)
make RTL=pcie_axil_master_fixed       # fixed RTL
make RTL=pcie_axil_master_fixed TLP_DATA_WIDTH=256
python3 ../scripts/merge_coverage.py ../reports/pcie_axil_master_fixed_w64
```

## Files

- `rtl/pcie_axil_master.v` - original DUT (MIT licence, from verilog-pcie)
- `rtl/pcie_axil_master_fixed.v` - DUT with both fixes
- `tb/pcie_uvm_env.py` - testbench components
- `tb/test_pcie_axil_uvm.py` - tests
- `tb/pcie_if.py` - TLP interface model from verilog-pcie
- `docs/fixes.patch` - the fixes
- `docs/upstream_issues.md` - bug reports for upstream
- `reports/` - test and coverage reports

## To do

- Port the testbench to SystemVerilog UVM
- Prove the completion length property formally in JasperGold
