# Draft upstream issue reports

## 1. pcie_axil_master: posted Message TLPs get a UR completion (operator precedence)

`rtl/pcie_axil_master.v` (and `pcie_axil_master_minimal.v`, `pcie_axi_master_rd.v`):

```verilog
if (rx_req_tlp_hdr_fmt[0] && rx_req_tlp_hdr_type & 5'b11000 == 5'b10000)
```

`==` binds tighter than `&`, so the message check is always false and a posted
Msg/MsgD falls through to the "other non-posted request" branch, which sends a
UR completion. Completions must never be generated for posted requests.
Fix: `(rx_req_tlp_hdr_type & 5'b11000) == 5'b10000`. (Already fixed in taxi.)

Reproduce: `cd tb && make TESTCASE=error_inject_test`

## 2. pcie_axil_master: malformed completion for >32 DW reads starting at addr[6:2] == 31

A Memory Read of more than 32 DW starting one DW below a 128-byte boundary
(e.g. 0x0DFC, 33 DW) produces a completion whose header Length is 1 DW, but
the remaining read data keeps streaming in the same TLP (strobe set beyond the
header length, EOP many beats late).

Cause: for the first DW of each RCB-aligned chunk, `resp_fifo_wr_last` only
checks `req_op_dword_count == 1`, not whether the chunk itself is one DW, and
the FSM moves to READ_2 instead of starting a new completion (READ_1).
Fix: see `docs/fixes.patch`. The same logic appears in
`taxi_pcie_axil_master.sv` (by inspection).

Reproduce: `cd tb && make TLP_DATA_WIDTH=64 TESTCASE=boundary_sweep_test`
